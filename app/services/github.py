"""GitHub foundation: allowlist, input validation, connection health, repository sync and
read-only repo browsing (docs/18-github-foundation.md).

Every repo-level call checks the allowlist first; a repo outside it is reported exactly like
a missing one (404), so the API never confirms that it exists.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote, unquote

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.github.client import (
    GitHubClient,
    GitHubError,
    GitHubNotFound,
    GitHubRateLimited,
)
from app.models.github import GitHubConnection, GitHubRepository, GitHubSyncRun
from app.schemas.github import (
    BranchOut,
    CommitOut,
    ContentEntry,
    ContentOut,
    RepositoryDetail,
    TreeEntry,
    TreeOut,
)

log = get_logger("services.github")

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/@{}^~-]{1,255}$")
MAX_PATH_LEN = 1024
MAX_EXPLICIT_REPOS = 50
MAX_COMMIT_MESSAGE = 4000


class InvalidInput(ValueError):
    """Rejected before anything is sent to GitHub (400)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --- validation ---------------------------------------------------------------------------


def check_name(value: str, what: str) -> str:
    if not NAME_RE.fullmatch(value) or value in (".", ".."):
        raise InvalidInput(f"invalid_{what}", f"invalid {what} name")
    return value


def check_ref(ref: str | None, what: str = "ref") -> str | None:
    if ref is None or ref == "":
        return None
    if (
        not REF_RE.fullmatch(ref)
        or ".." in ref
        or ref.startswith(("/", "-"))
        or ref.endswith(("/", ".lock"))
        or "//" in ref
        or "@{" in ref
    ):
        raise InvalidInput(f"invalid_{what}", f"invalid {what}")
    return ref


def check_path(path: str) -> str:
    """A repo-relative path: no traversal, no absolute paths, no odd characters.

    Checked both as given and once more URL-decoded, so `%2e%2e` cannot sneak a `..` past.
    """
    if path == "":
        return ""
    for candidate in (path, unquote(path)):
        if len(candidate) > MAX_PATH_LEN:
            raise InvalidInput("invalid_path", "path too long")
        if any(ord(c) < 32 or ord(c) == 127 for c in candidate) or "\\" in candidate:
            raise InvalidInput("invalid_path", "invalid characters in path")
        if candidate.startswith(("/", "~")):
            raise InvalidInput("invalid_path", "path must be relative to the repository root")
        parts = candidate.rstrip("/").split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise InvalidInput("invalid_path", "path traversal is not allowed")
    return path.rstrip("/")


def _quote_path(path: str) -> str:
    return "/".join(quote(p, safe="") for p in path.split("/"))


# --- allowlist ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Allowlist:
    owners: frozenset[str]
    repos: frozenset[str]

    @classmethod
    def from_settings(cls, settings: Settings) -> Allowlist:
        return cls(
            frozenset(o.strip().lower() for o in settings.GITHUB_ALLOWED_OWNERS if o.strip()),
            frozenset(r.strip().lower() for r in settings.GITHUB_ALLOWED_REPOS if r.strip()),
        )

    def allows(self, full_name: str, login: str | None) -> bool:
        """GITHUB_ALLOWED_REPOS (if set) is the exact list, narrowed by owners if both are
        set. Otherwise GITHUB_ALLOWED_OWNERS; with neither set, only the token user's own."""
        full = full_name.lower()
        owner = full.split("/", 1)[0]
        if self.repos:
            return full in self.repos and (not self.owners or owner in self.owners)
        if self.owners:
            return owner in self.owners
        return login is not None and owner == login.lower()


async def known_login(db: AsyncSession, client: GitHubClient) -> str | None:
    """The token user's login: from the last connection check, else one live /user call."""
    login = await db.scalar(
        select(GitHubConnection.login).where(GitHubConnection.fingerprint == client.fingerprint)
    )
    if login:
        return login
    return await client.login()


async def require_allowed(
    db: AsyncSession, client: GitHubClient, settings: Settings, owner: str, repo: str
) -> str:
    check_name(owner, "owner")
    check_name(repo, "repo")
    allow = Allowlist.from_settings(settings)
    login = None
    if not allow.repos and not allow.owners:
        login = await known_login(db, client)
    full = f"{owner}/{repo}"
    if not allow.allows(full, login):
        raise GitHubNotFound("repository not found")
    return full


# --- connection health --------------------------------------------------------------------


async def check_connection(
    maker: async_sessionmaker[AsyncSession], client: GitHubClient, settings: Settings
) -> GitHubConnection:
    """GET /user once; upsert github.connections for this token's fingerprint."""
    values: dict[str, Any] = {
        "token_kind": client.token_kind,
        "api_url": client.base_url,
    }
    try:
        user, scopes = await client.get_user()
        values |= {
            "login": str(user.get("login") or "")[:100] or None,
            "github_user_id": user.get("id") if isinstance(user.get("id"), int) else None,
            "scopes": scopes,
            "ok": True,
            "detail": None,
        }
    except GitHubError as exc:
        values |= {"ok": False, "detail": f"{exc.code}: {exc.message}"[:2000]}
    if client.rate_limit is not None:
        values["rate_limit"] = client.rate_limit.as_dict()
    ins = insert(GitHubConnection).values(
        fingerprint=client.fingerprint, last_checked_at=func.now(), **values
    )
    stmt = ins.on_conflict_do_update(
        index_elements=[GitHubConnection.fingerprint],
        set_={k: ins.excluded[k] for k in values}
        | {"last_checked_at": func.now(), "updated_at": func.now()},
    ).returning(GitHubConnection)
    async with maker() as db, db.begin():
        row: GitHubConnection = (await db.scalars(stmt)).one()
    log.info("github_connection_checked", ok=row.ok, login=row.login, detail=row.detail)
    return row


async def save_rate_limit(db: AsyncSession, client: GitHubClient) -> None:
    if client.rate_limit is None:
        return
    await db.execute(
        update(GitHubConnection)
        .where(GitHubConnection.fingerprint == client.fingerprint)
        .values(rate_limit=client.rate_limit.as_dict(), updated_at=func.now())
    )


# --- repository sync ----------------------------------------------------------------------


def _dt(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _repo_values(r: dict[str, Any]) -> dict[str, Any] | None:
    try:
        owner = str(r["owner"]["login"])
        return {
            "id": int(r["id"]),
            "full_name": str(r["full_name"])[:201],
            "owner": owner[:100],
            "name": str(r["name"])[:100],
            "description": r.get("description"),
            "private": bool(r.get("private")),
            "default_branch": r.get("default_branch"),
            "archived": bool(r.get("archived")),
            "pushed_at": _dt(r.get("pushed_at")),
            "html_url": str(r.get("html_url") or ""),
        }
    except (KeyError, TypeError, ValueError):
        return None


async def sync_repositories(
    maker: async_sessionmaker[AsyncSession],
    client: GitHubClient,
    settings: Settings,
    *,
    trigger: str,
    actor_user_id: uuid.UUID | None = None,
) -> GitHubSyncRun:
    """List the token's repos, keep the allowlisted ones, upsert github.repositories.
    Raises the GitHubError after recording it on the sync run."""
    async with maker() as db, db.begin():
        run = GitHubSyncRun(
            trigger=trigger,
            connection_fingerprint=client.fingerprint,
            actor_user_id=actor_user_id,
        )
        db.add(run)
        await db.flush()
        run_id = run.id
    try:
        allow = Allowlist.from_settings(settings)
        login = await client.login()
        listed, complete = await client.paginate_all(
            "/user/repos",
            {"affiliation": "owner,collaborator,organization_member", "sort": "pushed"},
            cache=False,
        )
        rows = {
            v["id"]: v
            for r in listed
            if isinstance(r, dict)
            and (v := _repo_values(r))
            and allow.allows(v["full_name"], login)
        }
        # Explicitly allowlisted repos the token can read but /user/repos does not list
        # (e.g. public repos of another owner).
        seen = {v["full_name"].lower() for v in rows.values()}
        for full in sorted(allow.repos - seen)[:MAX_EXPLICIT_REPOS]:
            owner, _, name = full.partition("/")
            try:
                check_name(owner, "owner")
                check_name(name, "repo")
                data = await client.get_json(f"/repos/{owner}/{name}", cache=False)
            except (GitHubNotFound, InvalidInput):
                continue
            if isinstance(data, dict) and (v := _repo_values(data)):
                if allow.allows(v["full_name"], login):
                    rows[v["id"]] = v
        removed = 0
        async with maker() as db, db.begin():
            for v in rows.values():
                ins = insert(GitHubRepository).values(**v, synced_at=func.now(), removed_at=None)
                await db.execute(
                    ins.on_conflict_do_update(
                        index_elements=[GitHubRepository.id],
                        set_={k: ins.excluded[k] for k in v if k != "id"}
                        | {"synced_at": func.now(), "removed_at": None},
                    )
                )
            if complete:  # a capped listing must not mark the unseen remainder as removed
                res = await db.execute(
                    update(GitHubRepository)
                    .where(
                        GitHubRepository.removed_at.is_(None),
                        GitHubRepository.id.not_in(list(rows) or [-1]),
                    )
                    .values(removed_at=func.now())
                )
                removed = res.rowcount  # type: ignore[attr-defined]
            await save_rate_limit(db, client)
            run_row = await db.get(GitHubSyncRun, run_id)
            assert run_row is not None
            run_row.status = "succeeded"
            run_row.repos_seen = len(listed)
            run_row.repos_upserted = len(rows)
            run_row.repos_removed = removed
            run_row.finished_at = func.now()
        log.info("github_sync", seen=len(listed), upserted=len(rows), removed=removed)
    except GitHubError as exc:
        async with maker() as db, db.begin():
            await db.execute(
                update(GitHubSyncRun)
                .where(GitHubSyncRun.id == run_id)
                .values(
                    status="failed",
                    error=f"{exc.code}: {exc.message}"[:2000],
                    finished_at=func.now(),
                )
            )
        raise
    async with maker() as db:
        final = await db.get(GitHubSyncRun, run_id)
        assert final is not None
        return final


async def last_sync(db: AsyncSession) -> GitHubSyncRun | None:
    row: GitHubSyncRun | None = await db.scalar(
        select(GitHubSyncRun).order_by(GitHubSyncRun.started_at.desc()).limit(1)
    )
    return row


def visible_repos_query(settings: Settings, login: str | None) -> Any:
    """github.repositories rows visible under the current allowlist (not removed)."""
    allow = Allowlist.from_settings(settings)
    q = select(GitHubRepository).where(GitHubRepository.removed_at.is_(None))
    if allow.repos:
        q = q.where(func.lower(GitHubRepository.full_name).in_(allow.repos))
        if allow.owners:
            q = q.where(func.lower(GitHubRepository.owner).in_(allow.owners))
    elif allow.owners:
        q = q.where(func.lower(GitHubRepository.owner).in_(allow.owners))
    else:
        q = q.where(func.lower(GitHubRepository.owner) == (login or "").lower())
    return q


# --- repo browsing ------------------------------------------------------------------------


async def repository(client: GitHubClient, full: str) -> RepositoryDetail:
    r = await client.get_json(f"/repos/{full}")
    if not isinstance(r, dict):
        raise GitHubError("unexpected response from GitHub")
    perms = r.get("permissions") or {}
    return RepositoryDetail(
        id=int(r["id"]),
        full_name=r["full_name"],
        owner=r["owner"]["login"],
        name=r["name"],
        description=r.get("description"),
        private=bool(r.get("private")),
        visibility=r.get("visibility"),
        default_branch=r.get("default_branch"),
        archived=bool(r.get("archived")),
        disabled=bool(r.get("disabled")),
        fork=bool(r.get("fork")),
        language=r.get("language"),
        size_kb=r.get("size"),
        open_issues_count=r.get("open_issues_count"),
        pushed_at=_dt(r.get("pushed_at")),
        updated_at=_dt(r.get("updated_at")),
        html_url=r.get("html_url") or "",
        permissions={k: bool(v) for k, v in perms.items() if isinstance(v, bool)},
    )


async def branches(client: GitHubClient, full: str) -> list[BranchOut]:
    items = await client.paginate(f"/repos/{full}/branches")
    return [
        BranchOut(
            name=b["name"],
            sha=(b.get("commit") or {}).get("sha"),
            protected=bool(b.get("protected")),
        )
        for b in items
        if isinstance(b, dict) and "name" in b
    ]


async def commits(
    client: GitHubClient,
    full: str,
    *,
    sha: str | None,
    path: str | None,
    per_page: int,
    page: int,
) -> list[CommitOut]:
    params: dict[str, Any] = {"per_page": per_page, "page": page}
    if sha:
        params["sha"] = sha
    if path:
        params["path"] = path
    data = await client.get_json(f"/repos/{full}/commits", params)
    return (
        [commit_out(client, c) for c in data if isinstance(c, dict)]
        if isinstance(data, list)
        else []
    )


def commit_out(client: GitHubClient, c: dict[str, Any]) -> CommitOut:
    """A GitHub commit object (repo or PR commit listing) -> CommitOut, message redacted."""
    commit = c.get("commit") or {}
    author = commit.get("author") or {}
    committer = commit.get("committer") or {}
    return CommitOut(
        sha=c["sha"],
        message=client.redact(str(commit.get("message") or ""))[:MAX_COMMIT_MESSAGE],
        author_name=author.get("name"),
        author_login=(c.get("author") or {}).get("login"),
        authored_at=_dt(author.get("date")),
        committer_name=committer.get("name"),
        committed_at=_dt(committer.get("date")),
        html_url=c.get("html_url"),
    )


async def _default_branch(client: GitHubClient, full: str) -> str:
    r = await client.get_json(f"/repos/{full}")
    branch = r.get("default_branch") if isinstance(r, dict) else None
    if not branch:
        raise GitHubNotFound("repository has no default branch (empty repository?)")
    return str(branch)


async def tree(
    client: GitHubClient,
    full: str,
    settings: Settings,
    *,
    ref: str | None,
    path: str,
    recursive: bool,
) -> TreeOut:
    ref = ref or await _default_branch(client, full)
    tree_ish = f"{ref}:{path}" if path else ref
    params = {"recursive": "1"} if recursive else None
    data = await client.get_json(f"/repos/{full}/git/trees/{quote(tree_ish, safe='/:')}", params)
    if not isinstance(data, dict):
        raise GitHubError("unexpected response from GitHub")
    raw = [e for e in data.get("tree") or [] if isinstance(e, dict) and "path" in e]
    cap = settings.GITHUB_MAX_TREE_ENTRIES
    return TreeOut(
        ref=ref,
        path=path,
        sha=data.get("sha"),
        recursive=recursive,
        truncated=bool(data.get("truncated")) or len(raw) > cap,
        entries=[
            TreeEntry(
                path=e["path"],
                type=str(e.get("type") or ""),
                mode=e.get("mode"),
                sha=e.get("sha"),
                size=e.get("size"),
            )
            for e in raw[:cap]
        ],
    )


def _decode_text(
    content: str, max_bytes: int
) -> tuple[Literal["text", "binary", "too_large"], str | None]:
    """-> (content_kind, text). Binary = not UTF-8, or has NUL bytes."""
    try:
        raw = base64.b64decode(content, validate=False)
    except (binascii.Error, ValueError):
        return "binary", None
    if len(raw) > max_bytes:
        return "too_large", None
    if b"\x00" in raw:
        return "binary", None
    try:
        return "text", raw.decode("utf-8")
    except UnicodeDecodeError:
        return "binary", None


async def contents(
    client: GitHubClient, full: str, settings: Settings, *, path: str, ref: str | None
) -> ContentOut:
    params = {"ref": ref} if ref else None
    url = f"/repos/{full}/contents/{_quote_path(path)}" if path else f"/repos/{full}/contents"
    data = await client.get_json(url, params)
    if isinstance(data, list):
        return ContentOut(
            path=path,
            ref=ref,
            type="dir",
            entries=[
                ContentEntry(
                    name=e.get("name") or "",
                    path=e.get("path") or "",
                    type=e.get("type") or "",
                    size=e.get("size"),
                    sha=e.get("sha"),
                )
                for e in data
                if isinstance(e, dict)
            ],
        )
    if not isinstance(data, dict):
        raise GitHubError("unexpected response from GitHub")
    kind = data.get("type")
    base = {
        "path": data.get("path") or path,
        "ref": ref,
        "name": data.get("name"),
        "size": data.get("size"),
        "sha": data.get("sha"),
        "html_url": data.get("html_url"),
    }
    if kind == "symlink":
        return ContentOut(**base, type="symlink", target=data.get("target"))
    if kind == "submodule":
        return ContentOut(**base, type="submodule", target=data.get("submodule_git_url"))
    if kind != "file":
        raise GitHubError(f"unsupported content type {kind!r}")
    size = data.get("size") if isinstance(data.get("size"), int) else None
    content = data.get("content")
    if (size is not None and size > settings.GITHUB_MAX_FILE_BYTES) or (
        data.get("encoding") != "base64" or not isinstance(content, str)
    ):
        # GitHub sends no content for files over 1 MB ("encoding": "none").
        return ContentOut(**base, type="file", content_kind="too_large")
    content_kind, text = _decode_text(content, settings.GITHUB_MAX_FILE_BYTES)
    return ContentOut(
        **base,
        type="file",
        content_kind=content_kind,
        encoding="utf-8" if text is not None else None,
        text=text,
    )


__all__ = [
    "Allowlist",
    "GitHubRateLimited",
    "InvalidInput",
    "branches",
    "check_connection",
    "check_name",
    "check_path",
    "check_ref",
    "commit_out",
    "commits",
    "contents",
    "known_login",
    "repository",
    "require_allowed",
    "sync_repositories",
    "tree",
    "visible_repos_query",
]
