"""The coding agent's git ops: clone / pull allowlisted GitHub repos into
CODE_CLONE_ROOT and push feature branches + open a PR (docs/05-agents.md, "Git ops").

- An admin queues an op (POST /api/code/git/...); the coding worker claims and runs it, one
  at a time, under HEAVY_MODEL_LOCK, so no model-written test process ever runs while a git
  child holds the token in its environment.
- Repos: GITHUB_CODING_REPO_ALLOWLIST (empty = nothing), and each must also pass the GitHub
  allowlist (D-15-3), checked at queue time and again at run time.
- Pushes: only feature branches (app/agents/coding/remote.py `check_push_branch`), fast-
  forward only, never a commit touching a secrets path, then a PR to the default branch.
  Merging stays behind the approval engine (`github.pr.merge`); nothing here merges.
- Every op is audited when queued (by the API) and when it finishes (by the worker).
- A clone becomes a coding repo named `gh-<owner>-<repo>` (base = its default branch), so
  change tasks run on it and their approved branches can be pushed.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.coding.git import TASK_BRANCH, GitError
from app.agents.coding.remote import (
    PushRefused,
    RemoteGit,
    check_full_name,
    check_push_branch,
    secret_paths,
)
from app.agents.coding.repos import NAME, RepoConfig
from app.core.config import Environment, Settings
from app.core.logging import get_logger
from app.core.sensitive_paths import sensitive_paths
from app.integrations.github.client import GitHubClient, GitHubError, GitHubNotFound
from app.integrations.github.redact import redact, redact_obj
from app.models.agents import CodeGitOp, CodeTask
from app.services import audit
from app.services import github as gh
from app.services import github_ops as ops
from app.services.audit import ClientInfo

log = get_logger(__name__)

OPS = ("clone", "pull", "push")  # queued through queue(); `task` through queue_task()
WORKER_CLIENT = ClientInfo(user_agent="aureon-coding-git")
HEAD_FILE = re.compile(r"^ref: refs/heads/([A-Za-z0-9._/-]{1,200})\s*$")


class GitOpRefused(ValueError):
    """Refused before anything runs; `code` is the API error code."""

    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class GitOpsConfig:
    allowlist: frozenset[str]
    clone_root: Path
    prefixes: tuple[str, ...]
    protected: frozenset[str]
    git_url: str
    timeout: float
    allow_file: bool

    @classmethod
    def from_settings(cls, s: Settings) -> GitOpsConfig:
        allow = set()
        for r in s.GITHUB_CODING_REPO_ALLOWLIST:
            if str(r).strip():
                allow.add(check_full_name(str(r).strip()))
        return cls(
            allowlist=frozenset(allow),
            clone_root=s.CODE_CLONE_ROOT,
            prefixes=tuple(p.strip() for p in s.CODE_PUSH_BRANCH_PREFIXES if p.strip()),
            protected=frozenset(s.CODE_PROTECTED_BRANCHES),
            git_url=s.GITHUB_GIT_URL,
            timeout=s.CODE_GIT_TIMEOUT_SECONDS,
            # A local bare repo as "GitHub" is for tests only, never prod.
            allow_file=s.ENV is Environment.TEST,
        )

    def require(self, full: str) -> str:
        try:
            name = check_full_name(full)
        except PushRefused as exc:
            raise GitOpRefused("invalid_repo", str(exc), 422) from exc
        if name not in self.allowlist:
            raise GitOpRefused(
                "repo_not_allowlisted", "repository is not in GITHUB_CODING_REPO_ALLOWLIST", 404
            )
        return name

    def clone_path(self, full: str) -> Path:
        owner, name = check_full_name(full).split("/", 1)
        return self.clone_root / owner / name


def clone_repo_name(full: str) -> str:
    """The coding-repo name a clone is registered under (CODE_REPOS naming rules)."""
    raw = "gh-" + check_full_name(full).replace("/", "-")
    name = re.sub(r"[^a-z0-9._-]", "-", raw)[:64]
    if not NAME.match(name):
        raise ValueError(f"cannot name a coding repo for {full}")
    return name


def head_branch(repo: Path) -> str | None:
    """The checked-out branch of a clone, read from .git/HEAD (sync, no git process)."""
    try:
        m = HEAD_FILE.match((repo / ".git" / "HEAD").read_text())
    except OSError:
        return None
    return m.group(1) if m else None


def clone_repos(settings: Settings) -> dict[str, RepoConfig]:
    """Allowlisted clones that exist on disk, as coding repos (no test commands)."""
    cfg = GitOpsConfig.from_settings(settings)
    out: dict[str, RepoConfig] = {}
    for full in sorted(cfg.allowlist):
        path = cfg.clone_path(full)
        base = head_branch(path)
        if base is None:
            continue
        out[clone_repo_name(full)] = RepoConfig(clone_repo_name(full), path, base)
    return out


def clone_full_name(settings: Settings, coding_repo: str) -> str | None:
    """The GitHub "owner/repo" a coding repo is a clone of, or None (a CODE_REPOS checkout).
    CODE_REPOS wins on a name clash, exactly as in CodingConfig."""
    if coding_repo in settings.CODE_REPOS:
        return None
    cfg = GitOpsConfig.from_settings(settings)
    for full in cfg.allowlist:
        if clone_repo_name(full) == coding_repo:
            return full
    return None


# --- queueing (API) ------------------------------------------------------------------------


async def queue(
    db: AsyncSession,
    settings: Settings,
    *,
    op: str,
    repo: str,
    user_id: uuid.UUID | None,
    client: ClientInfo,
    branch: str | None = None,
    title: str | None = None,
    body: str | None = None,
) -> CodeGitOp:
    """Validate and queue one op; the caller commits. Static branch rules are checked here
    too (default branch and GitHub protection are re-checked by the worker)."""
    if op not in OPS:
        raise GitOpRefused("invalid_op", f"unknown op {op}", 422)
    cfg = GitOpsConfig.from_settings(settings)
    full = cfg.require(repo)
    if not settings.github_configured:
        raise GitOpRefused("github_not_configured", "GITHUB_TOKEN is not set", 503)
    path = cfg.clone_path(full)
    exists = (path / ".git").exists()
    if op == "clone" and exists:
        raise GitOpRefused("already_cloned", "already cloned; pull instead")
    if op in ("pull", "push") and not exists:
        raise GitOpRefused("not_cloned", "clone the repository first")
    if op == "push":
        if not branch:
            raise GitOpRefused("branch_required", "a branch is required", 422)
        try:
            check_push_branch(
                branch,
                default_branch=head_branch(path),
                protected=cfg.protected,
                prefixes=cfg.prefixes,
            )
        except PushRefused as exc:
            raise GitOpRefused("push_refused", str(exc)) from exc
        if TASK_BRANCH.match(branch):  # an agent task branch: only once an admin approved it
            task = await db.scalar(
                select(CodeTask).where(
                    CodeTask.repo == clone_repo_name(full), CodeTask.branch == branch
                )
            )
            if task is not None and task.status != "approved":
                raise GitOpRefused(
                    "task_not_approved", f"the task on {branch} is {task.status}, not approved"
                )
    busy = await db.scalar(
        select(func.count())
        .select_from(CodeGitOp)
        .where(CodeGitOp.repo == full, CodeGitOp.status.in_(("queued", "running")))
    )
    if busy:
        raise GitOpRefused("op_in_progress", "another git op on this repository is in progress")
    row = CodeGitOp(
        op=op,
        repo=full,
        branch=branch if op == "push" else None,
        title=(title or (f"{branch}" if branch else None)) if op == "push" else None,
        body=body if op == "push" else None,
        requested_by=user_id,
    )
    db.add(row)
    await db.flush()
    audit.record(
        db,
        f"code_git_{op}_queued",
        client,
        actor_user_id=user_id,
        details={"op_id": str(row.id), "repo": full, "branch": row.branch},
    )
    return row


async def queue_task(
    db: AsyncSession,
    settings: Settings,
    task: CodeTask,
    *,
    branch: str,
    feature: str | None,
    user_id: uuid.UUID | None,
    client: ClientInfo,
) -> CodeGitOp:
    """The git half of approving a change task, for the coding worker (the API process has
    no git). The caller has validated the task and commits. For a GitHub clone the push
    rules are checked here as well and again by the worker, against GitHub's data."""
    full = clone_full_name(settings, task.repo)
    if full is not None:
        cfg = GitOpsConfig.from_settings(settings)
        if not settings.github_configured:
            raise GitOpRefused("github_not_configured", "GITHUB_TOKEN is not set", 503)
        try:
            check_push_branch(
                branch,
                default_branch=head_branch(cfg.clone_path(full)),
                protected=cfg.protected,
                prefixes=cfg.prefixes,
            )
        except PushRefused as exc:
            raise GitOpRefused("push_refused", str(exc)) from exc
    repo = full or task.repo
    busy = await db.scalar(
        select(func.count())
        .select_from(CodeGitOp)
        .where(CodeGitOp.repo == repo, CodeGitOp.status.in_(("queued", "running")))
    )
    if busy:
        raise GitOpRefused("op_in_progress", "another git op on this repository is in progress")
    stat = str((task.result or {}).get("diff_stat") or "")
    row = CodeGitOp(
        op="task",
        repo=repo,
        task_id=task.id,
        branch=branch,
        title=f"aureon: {task.title}"[:200],
        body=(
            f"{task.instruction[:4000]}\n\n```\n{stat[:4000]}\n```"
            if stat
            else task.instruction[:4000]
        ),
        requested_by=user_id,
        result={"feature_branch": feature, "task_branch": task.branch, "push": full is not None},
    )
    db.add(row)
    await db.flush()
    audit.record(
        db,
        "code_git_task_queued",
        client,
        actor_user_id=user_id,
        details={"op_id": str(row.id), "task": str(task.id), "repo": repo, "branch": branch},
    )
    return row


# --- running (coding worker) ---------------------------------------------------------------


async def claim(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID | None:
    async with maker() as db, db.begin():
        oid = await db.scalar(
            select(CodeGitOp.id)
            .where(CodeGitOp.status == "queued")
            .order_by(CodeGitOp.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if oid is None:
            return None
        await db.execute(
            update(CodeGitOp)
            .where(CodeGitOp.id == oid)
            .values(status="running", started_at=func.now())
        )
    return oid


async def recover(maker: async_sessionmaker[AsyncSession]) -> int:
    """Ops a crash left `running` are failed (a half-done clone directory is left for the
    admin to inspect; the next clone refuses until it is removed)."""
    async with maker() as db, db.begin():
        res = await db.execute(
            update(CodeGitOp)
            .where(CodeGitOp.status == "running")
            .values(status="failed", error="worker restarted during the op", finished_at=func.now())
        )
    return int(getattr(res, "rowcount", 0) or 0)


async def push_branch(
    cfg: GitOpsConfig,
    remote: RemoteGit,
    full: str,
    branch: str,
    *,
    default_branch: str | None,
    remote_protected: bool,
) -> dict[str, Any]:
    """Every push rule, then the push itself. No GitHub API calls in here (tested offline)."""
    path = cfg.clone_path(full)
    check_push_branch(
        branch,
        default_branch=default_branch,
        protected=cfg.protected,
        prefixes=cfg.prefixes,
        remote_protected=remote_protected,
    )
    assert default_branch is not None
    await remote.run("fetch", "--prune", "--no-tags", "origin", cwd=path, network=True)
    base = f"refs/remotes/origin/{default_branch}"
    files = await remote.touched_files(path, base, branch)
    if not files:
        raise PushRefused(f"{branch} has no commits beyond {default_branch}")
    secrets = secret_paths(files)
    if secrets:
        raise PushRefused("commits touch secrets paths: " + ", ".join(secrets[:20]))
    sha = await remote.push(full, path, branch)
    return {
        "head_sha": sha,
        "default_branch": default_branch,
        "files": files[:500],
        "sensitive": sensitive_paths(files),  # infra/auth/trading: allowed, flagged for review
    }


async def _open_pr(
    maker: async_sessionmaker[AsyncSession],
    client: GitHubClient,
    row: CodeGitOp,
    default_branch: str,
) -> tuple[int | None, str | None, bool]:
    owner = row.repo.split("/", 1)[0]
    existing = await client.get_json(
        f"/repos/{row.repo}/pulls",
        {"head": f"{owner}:{row.branch}", "state": "open", "per_page": 1},
        cache=False,
    )
    if isinstance(existing, list) and existing and isinstance(existing[0], dict):
        pr = existing[0]
        return pr.get("number"), pr.get("html_url"), False
    body = (row.body or "").strip()
    body = (
        (body + "\n\n" if body else "")
        + "Opened by the Aureon coding agent. Merging goes through the approval gate "
        "(`github.pr.merge`)."
    )
    data = await ops.tracked_write(
        maker, client, ops.Actor(row.requested_by, WORKER_CLIENT),
        op="pr.create", repo=row.repo, target=f"branch:{row.branch}",
        method="POST", path=f"/repos/{row.repo}/pulls",
        json={
            "title": (row.title or str(row.branch))[:200],
            "head": row.branch,
            "base": default_branch,
            "body": body,
            "maintainer_can_modify": False,
        },
    )  # fmt: skip
    pr = data if isinstance(data, dict) else {}
    number = pr.get("number") if isinstance(pr.get("number"), int) else None
    return number, pr.get("html_url"), True


async def _github_push(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: GitHubClient | None,
    cfg: GitOpsConfig,
    remote: RemoteGit,
    row: CodeGitOp,
    full: str,
    branch: str,
) -> dict[str, Any]:
    """Allowlists again, GitHub's default branch + protection, every push rule, the push,
    then the PR. Shared by `push` and `task` ops."""
    if client is None:
        raise GitOpRefused("github_not_configured", "GITHUB_TOKEN is not set", 503)
    async with maker() as db:  # the GitHub allowlist too, again at run time
        owner, name = full.split("/", 1)
        await gh.require_allowed(db, client, settings, owner, name)
    detail = await gh.repository(client, full)
    result = await push_checked(
        cfg, remote, client, full, branch, default_branch=detail.default_branch
    )
    number, url, created = await _open_pr(maker, client, row, str(detail.default_branch))
    return {
        "head_sha": result["head_sha"],
        "pr_number": number,
        "pr_url": url,
        "result": {**result, "pr_created": created},
    }


async def push_checked(
    cfg: GitOpsConfig,
    remote: RemoteGit,
    client: Any,
    full: str,
    branch: str,
    *,
    default_branch: str | None,
) -> dict[str, Any]:
    """GitHub's protection flag for the branch, then push_branch (every rule)."""
    protected = False
    try:
        b = await client.get_json(f"/repos/{full}/branches/{branch}", cache=False)
        protected = bool(isinstance(b, dict) and b.get("protected"))
    except GitHubNotFound:
        protected = False  # a new branch
    return await push_branch(
        cfg, remote, full, branch, default_branch=default_branch, remote_protected=protected
    )


async def finish_task_branch(settings: Settings, task: Any, branch: str) -> dict[str, Any]:
    """The local part of an approval, in the worker: create the feature branch at the task's
    commit (if one was asked for) and remove the task's worktree. Never pushes."""
    from app.agents.coding.git import FEATURE_BRANCH, Git
    from app.agents.coding.repos import get_repo
    from app.services import coding

    ccfg = coding.CodingConfig.from_settings(settings)
    repo = get_repo(ccfg.repos, task.repo)
    git = Git(repo.path)
    if branch != task.branch:
        if not FEATURE_BRANCH.match(branch) or branch in ccfg.protected:
            raise PushRefused(f"{branch} is not an allowed feature branch")
        if branch in await git.branches():
            if (await git.head(branch)) != task.commit_sha:
                raise GitError(f"{branch} already exists at another commit")
        else:
            await git.create_branch(branch, str(task.commit_sha))
    if task.worktree:
        await coding._cleanup(git, Path(task.worktree), None, delete_branch=False)
    return {"branch": branch, "commit": task.commit_sha}


async def _run_task_op(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: GitHubClient | None,
    cfg: GitOpsConfig,
    remote: RemoteGit | None,
    token: str | None,
    row: CodeGitOp,
) -> dict[str, Any]:
    async with maker() as db:
        task = await db.get(CodeTask, row.task_id) if row.task_id else None
    if task is None or task.status != "approved":
        raise GitOpRefused("task_not_approved", "the task is missing or not approved")
    branch = str(row.branch or "")
    local = await finish_task_branch(settings, task, branch)
    async with maker() as db, db.begin():
        await db.execute(update(CodeTask).where(CodeTask.id == task.id).values(worktree=None))
    full = clone_full_name(settings, task.repo)
    if full is None:  # a local CODE_REPOS checkout: keep the branch, nothing to push
        return {"head_sha": task.commit_sha, "result": {**row.result, **local, "pushed": False}}
    cfg.require(full)
    remote = remote or RemoteGit(cfg.git_url, token, timeout=cfg.timeout, allow_file=cfg.allow_file)
    out = await _github_push(maker, settings, client, cfg, remote, row, full, branch)
    return {**out, "result": {**row.result, **local, **out["result"], "pushed": True}}


async def _run_repo_op(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: GitHubClient | None,
    cfg: GitOpsConfig,
    remote: RemoteGit | None,
    token: str | None,
    row: CodeGitOp,
) -> dict[str, Any]:
    full = cfg.require(row.repo)
    if client is None:
        raise GitOpRefused("github_not_configured", "GITHUB_TOKEN is not set", 503)
    async with maker() as db:  # the GitHub allowlist too, again at run time
        owner, name = full.split("/", 1)
        await gh.require_allowed(db, client, settings, owner, name)
    remote = remote or RemoteGit(cfg.git_url, token, timeout=cfg.timeout, allow_file=cfg.allow_file)
    path = cfg.clone_path(full)
    if row.op == "clone":
        await remote.clone(full, path)
        default = await remote.default_branch(path)
        return {"result": {"default_branch": default, "path": str(path)}}
    if row.op == "pull":
        default = await remote.default_branch(path)
        if not default:
            raise GitError("cannot tell the default branch of the clone")
        sha = await remote.pull(full, path, default)
        return {"head_sha": sha, "result": {"default_branch": default}}
    return await _github_push(
        maker, settings, client, cfg, remote, row, full, str(row.branch or "")
    )


async def run_op(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: GitHubClient | None,
    oid: uuid.UUID,
    *,
    remote: RemoteGit | None = None,
) -> str:
    """Execute one claimed op; returns its final status (done / failed)."""
    started = time.monotonic()
    async with maker() as db:
        row = await db.get(CodeGitOp, oid)
        assert row is not None
    token = settings.GITHUB_TOKEN.get_secret_value() if settings.GITHUB_TOKEN else None
    values: dict[str, Any] = {}
    try:
        cfg = GitOpsConfig.from_settings(settings)
        if row.op == "task":
            values = await _run_task_op(maker, settings, client, cfg, remote, token, row)
        else:
            values = await _run_repo_op(maker, settings, client, cfg, remote, token, row)
        status, error = "done", None
    except Exception as exc:  # every failure ends the op; nothing is left `running`
        known = (GitOpRefused, PushRefused, GitError, OSError, ValueError)
        if isinstance(exc, GitHubError):
            msg = exc.message
        elif isinstance(exc, known):
            msg = str(exc)
        else:
            msg = f"{type(exc).__name__}: {exc}"
        status, error = "failed", redact(msg, [token])[:2000]
        log.warning("code_git_op_failed", op=row.op, repo=row.repo, error=error[:200])
    values = redact_obj(values, [token])
    async with maker() as db, db.begin():
        await db.execute(
            update(CodeGitOp)
            .where(CodeGitOp.id == oid)
            .values(status=status, error=error, finished_at=func.now(), **values)
        )
        audit.record(
            db,
            f"code_git_{row.op}_{status}",
            WORKER_CLIENT,
            actor_user_id=row.requested_by,
            details={
                "op_id": str(oid),
                "task": str(row.task_id) if row.task_id else None,
                "repo": row.repo,
                "branch": row.branch,
                "head_sha": values.get("head_sha"),
                "pr_number": values.get("pr_number"),
                "error": error,
                "duration_ms": int((time.monotonic() - started) * 1000),
            },
        )
    log.info("code_git_op_finished", op=row.op, repo=row.repo, status=status)
    return status
