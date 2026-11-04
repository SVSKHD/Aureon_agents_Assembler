"""GitHub foundation (docs/18-github-foundation.md). Admin only, GETs included.

    GET  /api/v1/github/connection                                  health (never the token)
    POST /api/v1/github/connection/check                            GET /user now (CSRF)
    GET  /api/v1/github/repositories                                synced table (?refresh=true)
    GET  /api/v1/github/repositories/{owner}/{repo}
    GET  /api/v1/github/repositories/{owner}/{repo}/branches
    GET  /api/v1/github/repositories/{owner}/{repo}/commits         ?sha=&path=&per_page=&page=
    GET  /api/v1/github/repositories/{owner}/{repo}/tree            ?ref=&path=&recursive=
    GET  /api/v1/github/repositories/{owner}/{repo}/contents/{path} ?ref=

Without GITHUB_TOKEN, /connection reports `configured: false` and everything else is
503 `github_not_configured`. Repos outside the allowlist are 404.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.ui import ui_module
from app.auth.deps import AdminRequired, DbSession, SettingsDep
from app.core.config import Settings
from app.integrations.github.client import (
    GitHubClient,
    GitHubError,
    GitHubNotConfigured,
    GitHubNotFound,
    GitHubRateLimited,
)
from app.models.github import GitHubConnection, GitHubRepository
from app.schemas.common import Page
from app.schemas.github import (
    BranchOut,
    CommitOut,
    ConnectionOut,
    ContentOut,
    RateLimitOut,
    RepositoryDetail,
    RepositoryOut,
    SyncRunOut,
    TreeOut,
)
from app.services import audit
from app.services import github as gh
from app.services.audit import ClientInfo

UI_MODULE = ui_module("github", label="GitHub", group="dev", icon="github", order=52)
router = APIRouter(prefix="/v1/github", tags=["github"])

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


def _error(exc: GitHubError) -> HTTPException:
    headers = None
    if isinstance(exc, GitHubRateLimited):
        headers = {"Retry-After": str(exc.retry_after)}
    # exc.message is redacted by the client; never str(exc.__cause__).
    return HTTPException(exc.status, {"code": exc.code, "detail": exc.message}, headers=headers)


def _invalid(exc: gh.InvalidInput) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, {"code": exc.code, "detail": exc.message})


def _maybe_client(request: Request) -> GitHubClient | None:
    client: GitHubClient | None = getattr(request.app.state, "github", None)
    return client


def _client(_admin: AdminRequired, request: Request) -> GitHubClient:
    client = _maybe_client(request)
    if client is None:
        err = GitHubNotConfigured("GitHub is not configured (set GITHUB_TOKEN on the server)")
        raise _error(err)
    return client


Client = Annotated[GitHubClient, Depends(_client)]


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    return maker


async def _allowed(
    db: AsyncSession, client: GitHubClient, settings: Settings, owner: str, repo: str
) -> str:
    try:
        return await gh.require_allowed(db, client, settings, owner, repo)
    except gh.InvalidInput as exc:
        raise _invalid(exc) from None
    except GitHubError as exc:
        raise _error(exc) from None


async def _connection_out(
    db: AsyncSession, client: GitHubClient | None, settings: Settings
) -> ConnectionOut:
    base = ConnectionOut(
        configured=client is not None,
        api_url=settings.GITHUB_API_URL,
        allowed_owners=list(settings.GITHUB_ALLOWED_OWNERS),
        allowed_repos=list(settings.GITHUB_ALLOWED_REPOS),
    )
    if client is None:
        base.detail = "GITHUB_TOKEN is not set"
        return base
    base.fingerprint = client.fingerprint
    base.token_kind = client.token_kind
    row = await db.get(GitHubConnection, client.fingerprint)
    if row is not None:
        base.login = row.login
        base.scopes = list(row.scopes or [])
        base.ok = row.ok
        base.detail = row.detail
        base.last_checked_at = row.last_checked_at
        if row.rate_limit:
            base.rate_limit = RateLimitOut.model_validate(row.rate_limit)
    if client.rate_limit is not None:  # this process's newer snapshot wins
        base.rate_limit = RateLimitOut.model_validate(client.rate_limit.as_dict())
    run = await gh.last_sync(db)
    base.last_sync = SyncRunOut.model_validate(run) if run else None
    return base


@router.get("/connection", response_model=ConnectionOut)
async def get_connection(
    _admin: AdminRequired, request: Request, db: DbSession, settings: SettingsDep
) -> ConnectionOut:
    return await _connection_out(db, _maybe_client(request), settings)


@router.post("/connection/check", response_model=ConnectionOut)
async def check_connection(
    admin: AdminRequired, client: Client, request: Request, db: DbSession, settings: SettingsDep
) -> ConnectionOut:
    row = await gh.check_connection(_maker(request), client, settings)
    audit.record(
        db,
        "github_connection_checked",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"ok": row.ok, "login": row.login, "fingerprint": client.fingerprint},
    )
    await db.commit()
    return await _connection_out(db, client, settings)


@router.get("/repositories", response_model=Page[RepositoryOut])
async def list_repositories(
    admin: AdminRequired,
    client: Client,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    limit: Limit = 50,
    offset: Offset = 0,
    refresh: bool = False,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> Page[RepositoryOut]:
    # A retried GET (client timeout while a slow sync runs) must not start another one.
    if refresh and not await gh.sync_in_progress(db):
        try:
            run = await gh.sync_repositories(
                _maker(request), client, settings, trigger="api", actor_user_id=admin.user.id
            )
        except GitHubError as exc:
            raise _error(exc) from None
        audit.record(
            db,
            "github_repositories_synced",
            ClientInfo.from_request(request),
            actor_user_id=admin.user.id,
            details={"sync_run_id": run.id, "upserted": run.repos_upserted},
        )
        await db.commit()
    login = None
    allow = gh.Allowlist.from_settings(settings)
    if not allow.repos and not allow.owners:
        try:
            login = await gh.known_login(db, client)
        except GitHubError as exc:
            raise _error(exc) from None
    query = gh.visible_repos_query(settings, login)
    if q:
        query = query.where(GitHubRepository.full_name.ilike(f"%{_like(q)}%", escape="\\"))
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await db.scalars(
            query.order_by(
                GitHubRepository.pushed_at.desc().nulls_last(), GitHubRepository.full_name
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[RepositoryOut.model_validate(r) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/repositories/{owner}/{repo}", response_model=RepositoryDetail)
async def get_repository(
    _admin: AdminRequired, client: Client, db: DbSession, settings: SettingsDep, owner: str,
    repo: str,
) -> RepositoryDetail:  # fmt: skip
    full = await _allowed(db, client, settings, owner, repo)
    try:
        detail = await gh.repository(client, full)
        # A renamed/transferred repo is followed by GitHub's redirect; re-check where it went.
        if detail.full_name.lower() != full.lower():
            new_owner, _, new_name = detail.full_name.partition("/")
            await gh.require_allowed(db, client, settings, new_owner, new_name)
    except GitHubError as exc:
        raise _error(exc) from None
    return detail


@router.get("/repositories/{owner}/{repo}/branches", response_model=list[BranchOut])
async def list_branches(
    _admin: AdminRequired, client: Client, db: DbSession, settings: SettingsDep, owner: str,
    repo: str,
) -> list[BranchOut]:  # fmt: skip
    full = await _allowed(db, client, settings, owner, repo)
    try:
        return await gh.branches(client, full)
    except GitHubError as exc:
        raise _error(exc) from None


@router.get("/repositories/{owner}/{repo}/commits", response_model=list[CommitOut])
async def list_commits(
    _admin: AdminRequired,
    client: Client,
    db: DbSession,
    settings: SettingsDep,
    owner: str,
    repo: str,
    sha: Annotated[str | None, Query(max_length=255)] = None,
    path: Annotated[str | None, Query(max_length=1024)] = None,
    per_page: Annotated[int, Query(ge=1, le=100)] = 30,
    page: Annotated[int, Query(ge=1, le=100)] = 1,
) -> list[CommitOut]:
    try:
        ref = gh.check_ref(sha, "sha")
        p = gh.check_path(path) if path else None
    except gh.InvalidInput as exc:
        raise _invalid(exc) from None
    full = await _allowed(db, client, settings, owner, repo)
    try:
        return await gh.commits(client, full, sha=ref, path=p, per_page=per_page, page=page)
    except GitHubError as exc:
        raise _error(exc) from None


@router.get("/repositories/{owner}/{repo}/tree", response_model=TreeOut)
async def get_tree(
    _admin: AdminRequired,
    client: Client,
    db: DbSession,
    settings: SettingsDep,
    owner: str,
    repo: str,
    ref: Annotated[str | None, Query(max_length=255)] = None,
    path: Annotated[str, Query(max_length=1024)] = "",
    recursive: bool = False,
) -> TreeOut:
    try:
        r = gh.check_ref(ref)
        p = gh.check_path(path)
    except gh.InvalidInput as exc:
        raise _invalid(exc) from None
    full = await _allowed(db, client, settings, owner, repo)
    try:
        return await gh.tree(client, full, settings, ref=r, path=p, recursive=recursive)
    except GitHubError as exc:
        raise _error(exc) from None


@router.get("/repositories/{owner}/{repo}/contents/{path:path}", response_model=ContentOut)
async def get_contents(
    _admin: AdminRequired,
    client: Client,
    db: DbSession,
    settings: SettingsDep,
    owner: str,
    repo: str,
    path: str,
    ref: Annotated[str | None, Query(max_length=255)] = None,
) -> ContentOut:
    try:
        r = gh.check_ref(ref)
        p = gh.check_path(path)
    except gh.InvalidInput as exc:
        raise _invalid(exc) from None
    full = await _allowed(db, client, settings, owner, repo)
    try:
        return await gh.contents(client, full, settings, path=p, ref=r)
    except GitHubNotFound:
        raise _error(GitHubNotFound("path not found")) from None
    except GitHubError as exc:
        raise _error(exc) from None
