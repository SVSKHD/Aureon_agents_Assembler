"""DevOps dashboard reads, notifications and the live feed (docs/32-devops-monitoring.md).

DB-only: nothing here calls GitHub or Docker, so polling the dashboard costs no rate limit
and needs no privileged access. Statuses are mapped into four separate vocabularies
(app/schemas/devops.py) and fall back to UNKNOWN whenever the data cannot prove a state.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, exists, func, not_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.deps import CurrentSession
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.agents import AgentRun
from app.models.devops import (
    Deployment,
    DevopsEvent,
    NotificationPrefs,
    NotificationRead,
    RepoEvent,
    Service,
)
from app.models.github import (
    GitHubConnection,
    GitHubRepository,
    GitHubWorkflowJob,
    GitHubWorkflowRun,
)
from app.schemas.devops import (
    BuildOut,
    CollectorStatusOut,
    DeploymentOut,
    EventOut,
    FeedOut,
    GitHubStatusOut,
    JobSummary,
    NotificationOut,
    NotificationsOut,
    OverviewOut,
    PreferencesIn,
    PreferencesOut,
    PullRequestOut,
    RepoEventOut,
    RepositoryStatusOut,
    ServiceOut,
)
from app.services import github as gh
from app.services.devops import build_state
from app.services.trading_stream import PING, _session_ok, sse

log = get_logger("services.devops_read")

CI_STATES = {
    "QUEUED": GitHubWorkflowRun.status.in_(("queued", "requested", "waiting", "pending")),
    "RUNNING": GitHubWorkflowRun.status == "in_progress",
    "SUCCEEDED": and_(
        GitHubWorkflowRun.status == "completed", GitHubWorkflowRun.conclusion == "success"
    ),
    "FAILED": and_(
        GitHubWorkflowRun.status == "completed",
        GitHubWorkflowRun.conclusion.in_(
            ("failure", "timed_out", "startup_failure", "action_required")
        ),
    ),
    "CANCELLED": and_(
        GitHubWorkflowRun.status == "completed",
        GitHubWorkflowRun.conclusion.in_(("cancelled", "skipped", "neutral", "stale")),
    ),
}


def _now() -> datetime:
    return datetime.now(UTC)


# --- mapping --------------------------------------------------------------------------------


def ci_out(run: GitHubWorkflowRun, failed_jobs: Sequence[GitHubWorkflowJob] = ()) -> BuildOut:
    return BuildOut(
        id=run.id, repo=run.repo, name=run.name, workflow_path=run.path, event=run.event,
        branch=run.branch, sha=run.head_sha,
        status=build_state(run.status, run.conclusion).upper(),
        conclusion=run.conclusion if run.status == "completed" else None,
        run_attempt=run.run_attempt, url=run.html_url, created_at=run.created_at,
        updated_at=run.updated_at,
        failed_jobs=[
            JobSummary(name=j.name, status=build_state(j.status, j.conclusion).upper(),
                       url=j.html_url)
            for j in failed_jobs
        ],
    )  # fmt: skip


def deployment_out(d: Deployment) -> DeploymentOut:
    if d.health_verified_at is not None:
        health = "VERIFIED"
    elif d.source == "server" and d.status in ("failed", "rolled_back"):
        health = "FAILED"
    elif d.source == "server" and d.status == "unverified":
        health = "NOT_VERIFIED"
    else:  # running, or a GitHub deployment (this service does not verify those)
        health = "UNKNOWN"
    return DeploymentOut(
        id=d.id, source=d.source, repo=d.repo, target=d.target,
        environment=d.environment, branch=d.branch, sha=d.sha, previous_sha=d.previous_sha,
        status=d.status.upper(),
        health=health,
        health_verified_at=d.health_verified_at, rolled_back=d.rolled_back,
        rollback_status=d.rollback_status,
        failure_reason=d.failure_reason, started_at=d.started_at, finished_at=d.finished_at,
        build_started_at=d.build_started_at, build_finished_at=d.build_finished_at,
        deploy_started_at=d.deploy_started_at, deploy_finished_at=d.deploy_finished_at,
        url=d.url,
    )  # fmt: skip


def pr_out(e: RepoEvent) -> PullRequestOut:
    status = {"pr.merged": "MERGED", "pr.closed": "CLOSED", "pr.opened": "OPEN",
              "pr.reopened": "OPEN"}.get(e.kind, "UNKNOWN")  # fmt: skip
    return PullRequestOut(
        repo=e.repo, number=e.number or 0, title=e.title, status=status,
        branch=e.branch, sha=e.sha, actor=e.actor, url=e.url, occurred_at=e.occurred_at,
    )  # fmt: skip


def service_out(s: Service, settings: Settings, now: datetime | None = None) -> ServiceOut:
    now = now or _now()
    stale = s.last_seen_at < now - timedelta(seconds=settings.DEVOPS_RUNTIME_STALE_SECONDS)
    return ServiceOut(
        id=s.id, host=s.host, service=s.service,
        status="UNKNOWN" if stale else s.status.upper(),
        stale=stale, state=s.state, health=s.health, exit_code=s.exit_code,
        container_id=s.container_id, container_name=s.container_name, image=s.image,
        revision=s.revision, started_at=s.started_at, status_since=s.status_since,
        last_seen_at=s.last_seen_at, last_healthy_at=s.last_healthy_at,
        failure_reason=s.failure_reason,
    )  # fmt: skip


# --- filters --------------------------------------------------------------------------------


def _range(col: Any, since: datetime | None, until: datetime | None) -> list[Any]:
    out = []
    if since is not None:
        out.append(col >= since)
    if until is not None:
        out.append(col < until)
    return out


def _ieq(col: Any, value: str | None) -> list[Any]:
    return [func.lower(col) == value.lower()] if value else []


async def _page(db: AsyncSession, q: Any, limit: int, offset: int) -> tuple[list[Any], int]:
    total = int(await db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0)
    rows = list((await db.scalars(q.limit(limit).offset(offset))).all())
    return rows, total


# --- lists ----------------------------------------------------------------------------------


async def repo_events(
    db: AsyncSession, *, repo: str | None, branch: str | None, kind: str | None,
    state: str | None, since: datetime | None, until: datetime | None, limit: int, offset: int,
) -> tuple[list[RepoEventOut], int]:  # fmt: skip
    q = select(RepoEvent).where(
        *_ieq(RepoEvent.repo, repo), *_range(RepoEvent.occurred_at, since, until),
        *([RepoEvent.branch == branch] if branch else []),
        *([RepoEvent.kind == kind] if kind else []),
        *([RepoEvent.state == state.lower()] if state else []),
    ).order_by(RepoEvent.occurred_at.desc(), RepoEvent.id.desc())  # fmt: skip
    rows, total = await _page(db, q, limit, offset)
    return [RepoEventOut.model_validate(r) for r in rows], total


async def _failed_jobs(db: AsyncSession, runs: Sequence[GitHubWorkflowRun]) -> dict[int, list[Any]]:
    ids = [r.id for r in runs if build_state(r.status, r.conclusion) == "failed"]
    out: dict[int, list[Any]] = {}
    if not ids:
        return out
    for j in (
        await db.scalars(
            select(GitHubWorkflowJob)
            .where(
                GitHubWorkflowJob.run_id.in_(ids),
                GitHubWorkflowJob.conclusion.in_(("failure", "timed_out", "startup_failure")),
            )
            .order_by(GitHubWorkflowJob.id)
        )
    ).all():
        out.setdefault(j.run_id, []).append(j)
    return out


async def builds(
    db: AsyncSession, *, repo: str | None, branch: str | None, status: str | None,
    since: datetime | None, until: datetime | None, limit: int, offset: int,
) -> tuple[list[BuildOut], int]:  # fmt: skip
    conds = [
        *_ieq(GitHubWorkflowRun.repo, repo),
        *_range(GitHubWorkflowRun.created_at, since, until),
    ]
    if branch:
        conds.append(GitHubWorkflowRun.branch == branch)
    if status == "UNKNOWN":
        conds.append(not_(or_(*CI_STATES.values())))
    elif status:
        conds.append(CI_STATES[status])
    q = (
        select(GitHubWorkflowRun)
        .where(*conds)
        .order_by(GitHubWorkflowRun.created_at.desc().nulls_last(), GitHubWorkflowRun.id.desc())
    )
    rows, total = await _page(db, q, limit, offset)
    jobs = await _failed_jobs(db, rows)
    return [ci_out(r, jobs.get(r.id, ())) for r in rows], total


async def deployments(
    db: AsyncSession, *, repo: str | None, environment: str | None, status: str | None,
    source: str | None, target: str | None, since: datetime | None, until: datetime | None,
    limit: int, offset: int,
) -> tuple[list[DeploymentOut], int]:  # fmt: skip
    q = select(Deployment).where(
        *_ieq(Deployment.repo, repo), *_range(Deployment.started_at, since, until),
        *([Deployment.environment == environment] if environment else []),
        *([Deployment.status == status.lower()] if status else []),
        *([Deployment.source == source] if source else []),
        *([Deployment.target == target] if target else []),
    ).order_by(Deployment.started_at.desc(), Deployment.id.desc())  # fmt: skip
    rows, total = await _page(db, q, limit, offset)
    return [deployment_out(r) for r in rows], total


async def services(
    db: AsyncSession, settings: Settings, *, status: str | None, host: str | None
) -> list[ServiceOut]:
    q = select(Service).order_by(Service.host, Service.service)
    if host:
        q = q.where(Service.host == host)
    now = _now()
    out = [service_out(s, settings, now) for s in (await db.scalars(q)).all()]
    return [s for s in out if s.status == status] if status else out


# --- overview / repositories ----------------------------------------------------------------


async def _github_status(db: AsyncSession, settings: Settings) -> GitHubStatusOut:
    run = await db.scalar(
        select(AgentRun)
        .where(AgentRun.task == "devops_backfill", AgentRun.status != "running")
        .order_by(AgentRun.started_at.desc())
        .limit(1)
    )
    return GitHubStatusOut(
        configured=settings.github_configured,
        webhook_configured=bool(
            settings.GITHUB_WEBHOOK_SECRET and settings.GITHUB_WEBHOOK_SECRET.get_secret_value()
        ),
        last_backfill_at=run.finished_at if run else None,
        last_backfill_ok=(run.status == "succeeded") if run else None,
    )


async def _latest_deployments(db: AsyncSession) -> list[Deployment]:
    """The newest deployment per (source, repo, target, environment)."""
    rn = (
        func.row_number()
        .over(
            partition_by=(
                Deployment.source,
                Deployment.repo,
                Deployment.target,
                Deployment.environment,
            ),
            order_by=(Deployment.started_at.desc(), Deployment.id.desc()),
        )
        .label("rn")
    )
    sub = select(Deployment.id, rn).subquery()
    return list(
        (
            await db.scalars(
                select(Deployment)
                .join(sub, sub.c.id == Deployment.id)
                .where(sub.c.rn == 1)
                .order_by(Deployment.started_at.desc())
            )
        ).all()
    )


async def overview(db: AsyncSession, settings: Settings, user_id: uuid.UUID) -> OverviewOut:
    now = _now()
    merged = (
        await db.scalars(
            select(RepoEvent).where(RepoEvent.kind == "pr.merged")
            .order_by(RepoEvent.occurred_at.desc()).limit(10)
        )
    ).all()  # fmt: skip
    running = (
        await db.scalars(
            select(GitHubWorkflowRun)
            .where(or_(CI_STATES["RUNNING"], CI_STATES["QUEUED"]))
            .order_by(GitHubWorkflowRun.created_at.desc().nulls_last()).limit(20)
        )
    ).all()  # fmt: skip
    failed = (
        await db.scalars(
            select(GitHubWorkflowRun)
            .where(CI_STATES["FAILED"], GitHubWorkflowRun.updated_at >= now - timedelta(days=2))
            .order_by(GitHubWorkflowRun.updated_at.desc()).limit(10)
        )
    ).all()  # fmt: skip
    jobs = await _failed_jobs(db, failed)
    svc = [service_out(s, settings, now) for s in (await db.scalars(select(Service))).all()]
    by_status: dict[str, int] = {}
    for s in svc:
        by_status[s.status] = by_status.get(s.status, 0) + 1
    last_seen = max((s.last_seen_at for s in svc), default=None)
    stale_cut = now - timedelta(seconds=settings.DEVOPS_RUNTIME_STALE_SECONDS)
    return OverviewOut(
        generated_at=now,
        github=await _github_status(db, settings),
        collector=CollectorStatusOut(
            last_seen_at=last_seen, stale=last_seen is None or last_seen < stale_cut
        ),
        recently_merged=[pr_out(e) for e in merged],
        building=[ci_out(r) for r in running],
        recent_failures=[ci_out(r, jobs.get(r.id, ())) for r in failed],
        deployments=[deployment_out(d) for d in await _latest_deployments(db)],
        services=sorted(svc, key=lambda s: (s.status == "HEALTHY", s.host, s.service)),
        services_by_status=by_status,
        unread_notifications=await unread_count(db, user_id),
    )


async def repositories(db: AsyncSession, settings: Settings) -> list[RepositoryStatusOut]:
    """Every monitored repository: the allowlisted synced repos plus any repo that has
    deployments or events recorded (e.g. a deploy target before the first GitHub sync)."""
    login = await db.scalar(
        select(GitHubConnection.login)
        .where(GitHubConnection.ok.is_(True), GitHubConnection.login.is_not(None))
        .order_by(GitHubConnection.last_checked_at.desc().nulls_last())
        .limit(1)
    )
    synced = {
        r.full_name.lower(): r
        for r in (await db.scalars(gh.visible_repos_query(settings, login))).all()
    }
    names: dict[str, str] = {k: r.full_name for k, r in synced.items()}
    for q in (select(Deployment.repo).distinct(), select(RepoEvent.repo).distinct()):
        for name in (await db.scalars(q)).all():
            if name:
                names.setdefault(name.lower(), name)
    out: list[RepositoryStatusOut] = []
    for key in sorted(names):
        full = names[key]
        repo_row: GitHubRepository | None = synced.get(key)
        default = repo_row.default_branch if repo_row else None
        pr = await db.scalar(
            select(RepoEvent)
            .where(func.lower(RepoEvent.repo) == key, RepoEvent.kind == "pr.merged")
            .order_by(RepoEvent.occurred_at.desc()).limit(1)
        )  # fmt: skip
        ci_q = select(GitHubWorkflowRun).where(func.lower(GitHubWorkflowRun.repo) == key)
        if default:
            ci_q = ci_q.where(GitHubWorkflowRun.branch == default)
        ci = await db.scalar(
            ci_q.order_by(GitHubWorkflowRun.created_at.desc().nulls_last()).limit(1)
        )
        dep_q = select(Deployment).where(func.lower(Deployment.repo) == key)
        latest = await db.scalar(dep_q.order_by(Deployment.started_at.desc()).limit(1))
        deployed = await db.scalar(
            dep_q.where(Deployment.status == "succeeded")
            .order_by(Deployment.started_at.desc()).limit(1)
        )  # fmt: skip
        out.append(
            RepositoryStatusOut(
                repo=full,
                default_branch=default,
                pr=pr_out(pr) if pr else None,
                ci=ci_out(ci) if ci else None,
                deployment=deployment_out(latest) if latest else None,
                deployed=deployment_out(deployed) if deployed else None,
            )
        )
    return out


# --- feed + notifications -------------------------------------------------------------------


def _event_filters(
    *, after_id: int | None = None, types: Sequence[str] = (), repo: str | None = None
) -> list[Any]:
    out: list[Any] = []
    if after_id is not None:
        out.append(DevopsEvent.id > after_id)
    if types:
        out.append(DevopsEvent.type.in_(list(types)))
    if repo:
        out.append(func.lower(DevopsEvent.repo) == repo.lower())
    return out


async def feed(
    db: AsyncSession, *, after_id: int, limit: int, types: Sequence[str], repo: str | None
) -> FeedOut:
    """Polling fallback for the stream: events after `after_id`, oldest first."""
    rows = list(
        (
            await db.scalars(
                select(DevopsEvent)
                .where(*_event_filters(after_id=after_id, types=types, repo=repo))
                .order_by(DevopsEvent.id)
                .limit(limit + 1)
            )
        ).all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1].id if rows else after_id
    return FeedOut(items=[EventOut.model_validate(r) for r in rows], last_id=last, has_more=more)


async def get_prefs(db: AsyncSession, user_id: uuid.UUID) -> NotificationPrefs:
    row = await db.get(NotificationPrefs, user_id)
    if row is None:
        await db.execute(insert(NotificationPrefs).values(user_id=user_id).on_conflict_do_nothing())
        row = await db.get(NotificationPrefs, user_id)
        assert row is not None
    return row


def prefs_out(p: NotificationPrefs) -> PreferencesOut:
    return PreferencesOut(
        enabled=p.enabled, failures_only=p.failures_only, repos=list(p.repos or []),
        types=list(p.types or []),
    )  # fmt: skip


async def set_prefs(db: AsyncSession, user_id: uuid.UUID, body: PreferencesIn) -> PreferencesOut:
    p = await get_prefs(db, user_id)
    p.enabled, p.failures_only = body.enabled, body.failures_only
    p.repos, p.types = list(body.repos), list(body.types)
    p.updated_at = _now()
    await db.flush()
    return prefs_out(p)


def _notify_filters(p: NotificationPrefs) -> list[Any]:
    """Which feed events are notifications for this user."""
    if not p.enabled:
        return [DevopsEvent.id < 0]  # nothing
    out: list[Any] = []
    if p.failures_only:
        out.append(DevopsEvent.severity == "error")
    if p.types:
        out.append(DevopsEvent.type.in_(list(p.types)))
    if p.repos:
        repos = [r.lower() for r in p.repos]
        # Runtime events have no repo: they are host-wide and always included.
        out.append(or_(DevopsEvent.repo.is_(None), func.lower(DevopsEvent.repo).in_(repos)))
    return out


def _is_read(user_id: uuid.UUID, p: NotificationPrefs) -> Any:
    return or_(
        DevopsEvent.id <= p.read_up_to,
        exists().where(
            NotificationRead.user_id == user_id, NotificationRead.event_id == DevopsEvent.id
        ),
    )


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    p = await db.get(NotificationPrefs, user_id)
    if p is None:  # never configured: defaults, nothing read yet
        p = NotificationPrefs(user_id=user_id, enabled=True, failures_only=False, repos=[],
                              types=[], read_up_to=0)  # fmt: skip
    q = (
        select(func.count())
        .select_from(DevopsEvent)
        .where(*_notify_filters(p), not_(_is_read(user_id, p)))
    )
    return int(await db.scalar(q) or 0)


async def notifications(
    db: AsyncSession, user_id: uuid.UUID, *, unread_only: bool, limit: int,
    before_id: int | None,
) -> NotificationsOut:  # fmt: skip
    p = await get_prefs(db, user_id)
    read = _is_read(user_id, p)
    q = select(DevopsEvent, read.label("read")).where(*_notify_filters(p))
    if unread_only:
        q = q.where(not_(read))
    if before_id is not None:
        q = q.where(DevopsEvent.id < before_id)
    rows = (await db.execute(q.order_by(DevopsEvent.id.desc()).limit(limit + 1))).all()
    more = len(rows) > limit
    rows = rows[:limit]
    items = [
        NotificationOut(**EventOut.model_validate(ev).model_dump(), read=bool(is_read))
        for ev, is_read in rows
    ]
    return NotificationsOut(
        items=items,
        unread_count=await unread_count(db, user_id),
        next_before_id=items[-1].id if more and items else None,
    )


async def mark_read(
    db: AsyncSession, user_id: uuid.UUID, event_ids: Sequence[int], up_to_id: int | None
) -> int:
    p = await get_prefs(db, user_id)
    if up_to_id is not None and up_to_id > p.read_up_to:
        p.read_up_to = up_to_id
        p.updated_at = _now()
    ids = sorted({i for i in event_ids if i > p.read_up_to})
    if ids:
        existing = set(
            (await db.scalars(select(DevopsEvent.id).where(DevopsEvent.id.in_(ids)))).all()
        )
        if existing:
            await db.execute(
                insert(NotificationRead)
                .values([{"user_id": user_id, "event_id": i} for i in sorted(existing)])
                .on_conflict_do_nothing()
            )
    await db.flush()
    return await unread_count(db, user_id)


# --- live stream ----------------------------------------------------------------------------


def parse_cursor(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    return int(value) if value.isdigit() and len(value) <= 19 else None


async def stream(
    maker: async_sessionmaker[AsyncSession],
    current: CurrentSession,
    *,
    last_event_id: str | None,
    types: Sequence[str],
    repo: str | None,
    poll_seconds: float,
    heartbeat_seconds: float,
    max_seconds: float,
    batch: int = 100,
) -> AsyncIterator[str]:
    """Server-Sent Events: every feed event after the cursor, oldest first, each with its id.
    With a cursor (Last-Event-ID / ?last_event_id) the stream replays what was missed;
    without one it starts at the newest event (load history via /feed or /notifications).
    A database error (e.g. a Postgres restart) skips that poll instead of ending the stream."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    cursor = parse_cursor(last_event_id)
    async with maker() as db:
        if cursor is None:
            cursor = int(await db.scalar(select(func.coalesce(func.max(DevopsEvent.id), 0))) or 0)
    yield "retry: 3000\n\n"
    yield sse("hello", {"server_time": _now(), "poll_seconds": poll_seconds, "cursor": cursor},
              str(cursor))  # fmt: skip
    last_sent = loop.time()
    while True:
        rows: list[DevopsEvent] = []
        try:
            async with maker() as db:
                if not await _session_ok(db, current.session.id):
                    yield sse("bye", {"reason": "session_ended"})
                    return
                rows = list(
                    (
                        await db.scalars(
                            select(DevopsEvent)
                            .where(*_event_filters(after_id=cursor, types=types, repo=repo))
                            .order_by(DevopsEvent.id)
                            .limit(batch)
                        )
                    ).all()
                )
        except Exception as exc:  # the DB is restarting: try again next poll
            log.warning("devops_stream_poll_failed", error=type(exc).__name__)
        for ev in rows:
            cursor = ev.id
            yield sse(ev.type, EventOut.model_validate(ev).model_dump(mode="json"), str(ev.id))
        if rows:
            last_sent = loop.time()
        elif loop.time() - last_sent >= heartbeat_seconds:
            yield PING
            last_sent = loop.time()
        if loop.time() - started >= max_seconds:
            yield sse("bye", {"reason": "max_duration"}, str(cursor))
            return
        if len(rows) < batch:
            await asyncio.sleep(poll_seconds)
