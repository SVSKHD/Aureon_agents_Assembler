"""DevOps dashboard for ASTRA-FRONTEND (docs/32-devops-monitoring.md). Admin only, like the
rest of the GitHub data; GETs need no CSRF header, writes do.

    GET  /api/v1/devops/overview                    what merged, is building, failed, deployed,
                                                    is running; unread count
    GET  /api/v1/devops/repositories                per repo: last merged PR, CI on the default
                                                    branch, latest and last healthy deployment
    GET  /api/v1/devops/events                      normalized GitHub activity (PRs, deploys)
    GET  /api/v1/devops/builds                      GitHub Actions runs (CI status)
    GET  /api/v1/devops/deployments                 server + GitHub deployments
    GET  /api/v1/devops/services                    compose services from the host collector
    GET  /api/v1/devops/feed?after_id=              live-feed events after a cursor (polling)
    GET  /api/v1/devops/stream                      the same feed as Server-Sent Events
    GET  /api/v1/devops/notifications               this user's notifications + unread count
    POST /api/v1/devops/notifications/read          mark ids / everything up to an id as read
    GET  /api/v1/devops/notifications/preferences
    PUT  /api/v1/devops/notifications/preferences   enabled, failures_only, repos, types

Read-only towards GitHub and Docker: everything is served from the database, so no request
here spends GitHub rate limit or touches the Docker socket, and no token or secret is ever
returned. Statuses: PR / CI / deployment / runtime are separate vocabularies with UNKNOWN.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.api.ui import ui_meta, ui_module
from app.auth.deps import AdminRequired, DbSession, SettingsDep
from app.schemas.common import Page
from app.schemas.devops import (
    BuildOut,
    CIStatus,
    DeploymentOut,
    DeploymentStatus,
    EventType,
    FeedOut,
    MarkReadIn,
    NotificationsOut,
    OverviewOut,
    PreferencesIn,
    PreferencesOut,
    RepoEventKind,
    RepoEventOut,
    RepositoryStatusOut,
    RuntimeStatus,
    ServiceOut,
    UnreadOut,
)
from app.services import devops_read as read

UI_MODULE = ui_module("devops", label="DevOps", group="dev", icon="activity", order=53)
router = APIRouter(prefix="/v1/devops", tags=["devops"])

Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]
Repo = Annotated[str | None, Query(max_length=201, pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")]
Branch = Annotated[str | None, Query(max_length=255)]
Since = Annotated[datetime | None, Query(description="Inclusive lower bound (ISO 8601)")]
Until = Annotated[datetime | None, Query(description="Exclusive upper bound (ISO 8601)")]
Env = Annotated[str | None, Query(max_length=100)]


@router.get("/overview", response_model=OverviewOut)
async def overview(db: DbSession, admin: AdminRequired, settings: SettingsDep) -> OverviewOut:
    return await read.overview(db, settings, admin.user.id)


@router.get("/repositories", response_model=list[RepositoryStatusOut])
async def repositories(
    db: DbSession, _: AdminRequired, settings: SettingsDep
) -> list[RepositoryStatusOut]:
    return await read.repositories(db, settings)


@router.get("/events", response_model=Page[RepoEventOut])
async def events(
    db: DbSession,
    _: AdminRequired,
    repo: Repo = None,
    branch: Branch = None,
    type: Annotated[RepoEventKind | None, Query(description="Event kind")] = None,
    status: Annotated[str | None, Query(max_length=32, description="Provider state")] = None,
    since: Since = None,
    until: Until = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[RepoEventOut]:
    items, total = await read.repo_events(
        db, repo=repo, branch=branch, kind=type, state=status, since=since, until=until,
        limit=limit, offset=offset,
    )  # fmt: skip
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/builds", response_model=Page[BuildOut])
async def builds(
    db: DbSession,
    _: AdminRequired,
    repo: Repo = None,
    branch: Branch = None,
    status: CIStatus | None = None,
    since: Since = None,
    until: Until = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[BuildOut]:
    items, total = await read.builds(
        db, repo=repo, branch=branch, status=status, since=since, until=until, limit=limit,
        offset=offset,
    )  # fmt: skip
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/deployments", response_model=Page[DeploymentOut])
async def deployments(
    db: DbSession,
    _: AdminRequired,
    repo: Repo = None,
    environment: Env = None,
    status: DeploymentStatus | None = None,
    source: Annotated[str | None, Query(pattern="^(server|github)$")] = None,
    target: Annotated[str | None, Query(max_length=32)] = None,
    since: Since = None,
    until: Until = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[DeploymentOut]:
    items, total = await read.deployments(
        db, repo=repo, environment=environment, status=status, source=source, target=target,
        since=since, until=until, limit=limit, offset=offset,
    )  # fmt: skip
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/services", response_model=list[ServiceOut])
async def services(
    db: DbSession,
    _: AdminRequired,
    settings: SettingsDep,
    status: RuntimeStatus | None = None,
    host: Annotated[str | None, Query(max_length=100)] = None,
) -> list[ServiceOut]:
    return await read.services(db, settings, status=status, host=host)


@router.get("/feed", response_model=FeedOut)
async def feed(
    db: DbSession,
    _: AdminRequired,
    after_id: Annotated[int, Query(ge=0, description="Return events with a larger id")] = 0,
    type: Annotated[list[EventType] | None, Query(max_length=10)] = None,
    repo: Repo = None,
    limit: Limit = 100,
) -> FeedOut:
    """Polling fallback for /stream: every feed event after `after_id`, oldest first. Keep
    `last_id` and pass it back as `after_id`; `has_more` means call again at once."""
    return await read.feed(db, after_id=after_id, limit=limit, types=type or [], repo=repo)


@router.get(
    "/stream",
    response_class=StreamingResponse,
    status_code=200,
    responses={200: {"content": {"text/event-stream": {}}}},
    openapi_extra=ui_meta(kind="stream"),
)
async def stream(
    request: Request,
    admin: AdminRequired,
    settings: SettingsDep,
    type: Annotated[list[EventType] | None, Query(max_length=10)] = None,
    repo: Repo = None,
    last_event_id: Annotated[str | None, Header(max_length=32)] = None,
    cursor: Annotated[str | None, Query(alias="last_event_id", max_length=32)] = None,
) -> StreamingResponse:
    """Server-Sent Events. Each event's name is its type (build.failed, deployment.completed,
    service.unhealthy, ...), its `id:` the feed id and its data an EventOut. Reconnecting with
    `Last-Event-ID` (or ?last_event_id=) replays everything missed, in order; `hello` carries
    the starting cursor, `bye` ends the stream (session ended / max duration)."""
    gen = read.stream(
        request.app.state.sessionmaker,
        admin,
        last_event_id=last_event_id or cursor,
        types=type or [],
        repo=repo,
        poll_seconds=settings.DEVOPS_STREAM_POLL_SECONDS,
        heartbeat_seconds=settings.DEVOPS_STREAM_HEARTBEAT_SECONDS,
        max_seconds=settings.DEVOPS_STREAM_MAX_SECONDS,
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/notifications", response_model=NotificationsOut)
async def notifications(
    db: DbSession,
    admin: AdminRequired,
    unread_only: bool = False,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Limit = 50,
) -> NotificationsOut:
    out = await read.notifications(
        db, admin.user.id, unread_only=unread_only, limit=limit, before_id=before_id
    )
    await db.commit()  # first read creates the default preferences row
    return out


@router.post("/notifications/read", response_model=UnreadOut)
async def mark_read(body: MarkReadIn, db: DbSession, admin: AdminRequired) -> UnreadOut:
    """Mark `event_ids`, and/or everything up to `up_to_id`, as read for this user."""
    n = await read.mark_read(db, admin.user.id, body.event_ids, body.up_to_id)
    await db.commit()
    return UnreadOut(unread_count=n)


@router.get("/notifications/preferences", response_model=PreferencesOut)
async def get_preferences(db: DbSession, admin: AdminRequired) -> PreferencesOut:
    out = read.prefs_out(await read.get_prefs(db, admin.user.id))
    await db.commit()
    return out


@router.put("/notifications/preferences", response_model=PreferencesOut)
async def put_preferences(
    body: PreferencesIn, db: DbSession, admin: AdminRequired
) -> PreferencesOut:
    out = await read.set_prefs(db, admin.user.id, body)
    await db.commit()
    return out
