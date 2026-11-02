"""Incident detection (docs/21-self-repair.md).

Sources, each fingerprinted so repeats aggregate into one open incident (`count`,
`last_seen`):

- exception: unhandled exceptions captured in-process by app/core/exception_sink.py and
  written here by a background flusher (API and every worker), rate-limited per fingerprint.
- heartbeat: an agent (not paused, not cleanly stopped) whose last heartbeat is older than
  3x its interval (or its own `stale_after_s`, whichever is longer).
- docker: a container that is not running or reports unhealthy, read through the existing
  read-only docker-proxy (only the API can reach it); no proxy = "unknown", no incident.
- actions: a failed GitHub Actions run, from the Phase 17 `on_workflow_failed` hook and from
  github.workflow_runs (the sync fills gaps).
- db: `SELECT 1` failing or slower than INCIDENT_DB_SLOW_MS.
- api: the API's own /api/health/ready not answering 200.

Probe incidents (heartbeat, docker, db, api) resolve themselves once the probe is healthy
again. Nothing here acts on an incident: recovery and escalation live in
app/services/repair_recovery.py, repairs in app/services/repairs.py. `details` is always
passed through the log scrubber.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.exception_sink import SINK, Captured
from app.core.logging import get_logger, scrub
from app.models.agents import Agent
from app.models.github import GitHubWorkflowRun
from app.models.repairs import INCIDENT_ACTIVE, Incident
from app.services.github_webhooks import WorkflowFailure, on_workflow_failed

log = get_logger("services.incidents")

AUTO_RESOLVABLE = ("open", "recovering")
FAILED_CONCLUSIONS = ("failure", "timed_out", "startup_failure")
MAX_RUN_IDS = 10
HEARTBEAT_FACTOR = 3
REPAIR_BRANCH_PREFIX = "repair/"


def fingerprint(source: str, *parts: object) -> str:
    raw = "|".join([source, *(str(p) for p in parts)])
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def _now() -> datetime:
    return datetime.now(UTC)


# --- writing ------------------------------------------------------------------------------


async def record(
    db: AsyncSession,
    *,
    source: str,
    fp: str,
    severity: str,
    title: str,
    details: dict[str, Any],
    count: int = 1,
) -> Incident:
    """Open a new incident, or bump the open one with this fingerprint (in the caller's
    transaction). Serialised per fingerprint with an advisory lock."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "incident:" + fp})
    safe: dict[str, Any] = scrub(details)
    inc = await db.scalar(
        select(Incident)
        .where(Incident.fingerprint == fp, Incident.status.in_(INCIDENT_ACTIVE))
        .with_for_update()
    )
    if inc is None:
        inc = Incident(
            fingerprint=fp,
            source=source,
            severity=severity,
            title=title[:300],
            details=safe,
            count=count,
            status="open",
        )
        db.add(inc)
        await db.flush()
        log.warning("incident_opened", incident=str(inc.id), source=source, title=title[:120])
        return inc
    # Keep the system's own bookkeeping keys (recovery, escalation) from the stored row.
    keep = {k: v for k, v in inc.details.items() if k.startswith("_")}
    inc.details = safe | keep
    inc.count += count
    inc.last_seen = _now()
    inc.title = title[:300]
    if _rank(severity) > _rank(inc.severity):
        inc.severity = severity
    return inc


_SEVERITY = {"info": 0, "warning": 1, "error": 2, "critical": 3}


def _rank(sev: str) -> int:
    return _SEVERITY.get(sev, 0)


async def resolve_fp(db: AsyncSession, fp: str, note: str) -> int:
    """Auto-resolve an open/recovering incident whose probe is healthy again."""
    res = await db.execute(
        update(Incident)
        .where(Incident.fingerprint == fp, Incident.status.in_(AUTO_RESOLVABLE))
        .values(
            status="resolved",
            resolved_at=func.now(),
            details=Incident.details.op("||")({"_resolved_by": note}),
        )
    )
    n: int = res.rowcount  # type: ignore[attr-defined]
    return n


# --- exceptions ---------------------------------------------------------------------------


async def flush_exceptions(
    maker: async_sessionmaker[AsyncSession], settings: Settings, process: str
) -> int:
    """Write due captured exceptions (rate-limited per fingerprint). Returns how many."""
    due = SINK.due(settings.INCIDENT_EXCEPTION_RATE_SECONDS)
    if not due:
        return 0
    try:
        async with maker() as db, db.begin():
            for cap in due:
                await _record_exception(db, cap, process)
    except Exception as exc:
        SINK.requeue(due)
        # No exc_info: a failing flush must not feed itself back into the sink.
        log.warning("incident_flush_failed", error=type(exc).__name__)
        return 0
    return len(due)


async def _record_exception(db: AsyncSession, cap: Captured, process: str) -> Incident:
    details = dict(cap.details) | {"process": process}
    return await record(
        db,
        source="exception",
        fp=cap.fingerprint,
        severity="error",
        title=cap.title,
        details=details,
        count=cap.pending,
    )


async def exception_flusher(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    process: str,
    stop: asyncio.Event,
    every: float = 5.0,
) -> None:
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every)
        await flush_exceptions(maker, settings, process)


def start_exception_flusher(
    maker: async_sessionmaker[AsyncSession], settings: Settings, process: str
) -> tuple[asyncio.Task[None], asyncio.Event]:
    stop = asyncio.Event()
    task = asyncio.create_task(
        exception_flusher(maker, settings, process, stop), name="incident-exception-flusher"
    )
    return task, stop


async def stop_exception_flusher(handle: tuple[asyncio.Task[None], asyncio.Event]) -> None:
    task, stop = handle
    stop.set()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await asyncio.wait_for(task, 10)


# --- heartbeats ---------------------------------------------------------------------------


def stale_threshold(agent: Agent, settings: Settings) -> float:
    d = agent.details or {}
    interval = d.get("heartbeat_interval_s")
    if not isinstance(interval, int | float) or isinstance(interval, bool) or interval <= 0:
        interval = settings.AGENT_HEARTBEAT_SECONDS
    threshold = HEARTBEAT_FACTOR * float(interval)
    own = d.get("stale_after_s")
    if isinstance(own, int | float) and not isinstance(own, bool) and own > 0:
        threshold = max(threshold, float(own))
    return threshold


async def scan_heartbeats(
    maker: async_sessionmaker[AsyncSession], settings: Settings, now: datetime | None = None
) -> dict[str, Any]:
    now = now or _now()
    stale: list[str] = []
    async with maker() as db, db.begin():
        agents = (await db.scalars(select(Agent))).all()
        for a in agents:
            fp = fingerprint("heartbeat", a.id)
            if a.paused or a.state == "stopped":
                await resolve_fp(db, fp, "paused or stopped")
                continue
            limit = stale_threshold(a, settings)
            last = a.last_heartbeat_at
            if last is not None and (now - last).total_seconds() <= limit:
                await resolve_fp(db, fp, "heartbeat fresh")
                continue
            age = None if last is None else round((now - last).total_seconds())
            stale.append(a.id)
            await record(
                db,
                source="heartbeat",
                fp=fp,
                severity="warning",
                title=f"agent {a.id} heartbeat is stale",
                details={
                    "agent_id": a.id,
                    "kind": a.kind,
                    "state": a.state,
                    "age_seconds": age,
                    "threshold_seconds": limit,
                    "host": a.host,
                },
            )
    return {"stale_agents": stale}


# --- docker -------------------------------------------------------------------------------

# None = unknown (no proxy reachable); otherwise [{"name", "state", "status"}].
DockerProbe = Callable[[], Awaitable[list[dict[str, Any]] | None]]


def proxy_docker_probe(http: httpx.AsyncClient, settings: Settings) -> DockerProbe:
    """GET /containers/json through the existing read-only docker-proxy."""

    async def probe() -> list[dict[str, Any]] | None:
        if not settings.DOCKER_API_URL:
            return None
        try:
            resp = await http.get(
                f"{settings.DOCKER_API_URL.rstrip('/')}/containers/json", params={"all": "1"}
            )
            resp.raise_for_status()
            rows = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
        return [
            {
                "name": (c.get("Names") or ["?"])[0].lstrip("/"),
                "state": c.get("State"),
                "status": c.get("Status"),
            }
            for c in rows
            if isinstance(c, dict)
        ]

    return probe


async def unknown_docker_probe() -> list[dict[str, Any]] | None:
    return None


def unhealthy(c: dict[str, Any]) -> bool:
    return c.get("state") != "running" or "unhealthy" in str(c.get("status") or "")


async def scan_docker(
    maker: async_sessionmaker[AsyncSession], probe: DockerProbe
) -> dict[str, Any]:
    containers = await probe()
    if containers is None:
        return {"docker": "unknown"}
    bad: list[str] = []
    async with maker() as db, db.begin():
        for c in containers:
            name = str(c.get("name") or "?")[:120]
            fp = fingerprint("docker", name)
            if not unhealthy(c):
                await resolve_fp(db, fp, "container healthy")
                continue
            bad.append(name)
            await record(
                db,
                source="docker",
                fp=fp,
                severity="error",
                title=f"container {name} is {c.get('state')}",
                details={"container": name, "state": c.get("state"), "status": c.get("status")},
            )
    return {"docker": "ok" if not bad else "degraded", "unhealthy": bad}


async def docker_monitor(
    maker: async_sessionmaker[AsyncSession],
    probe: DockerProbe,
    every: float,
    stop: asyncio.Event,
) -> None:
    """The API's loop (only the API can reach the docker-proxy)."""
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every)
        if stop.is_set():
            break
        try:
            await scan_docker(maker, probe)
        except Exception as exc:
            log.warning("incident_docker_scan_failed", error=type(exc).__name__)


# --- GitHub Actions -----------------------------------------------------------------------


def actions_fp(repo: str, path: str | None, name: str | None, branch: str | None) -> str:
    return fingerprint("actions", repo.lower(), path or name or "?", branch or "?")


async def record_workflow_failure(db: AsyncSession, f: WorkflowFailure) -> Incident | None:
    fp = actions_fp(f.repo, f.path, f.name, f.branch)
    existing = await db.scalar(
        select(Incident).where(Incident.fingerprint == fp, Incident.status.in_(INCIDENT_ACTIVE))
    )
    seen = list((existing.details if existing else {}).get("run_ids") or [])
    if f.run_id in seen:
        return existing  # the webhook and the table scan saw the same run
    branch = f.branch or ""
    details = {
        "repo": f.repo,
        "run_id": f.run_id,
        "run_ids": [*seen, f.run_id][-MAX_RUN_IDS:],
        "workflow_id": f.workflow_id,
        "workflow": f.name,
        "path": f.path,
        "branch": f.branch,
        "event": f.event,
        "head_sha": f.head_sha,
        "conclusion": f.conclusion,
        "run_attempt": f.run_attempt,
        "html_url": f.html_url,
        # Recursion guard: a repair PR's own CI failure never starts another repair.
        "self_origin": branch.startswith(REPAIR_BRANCH_PREFIX),
    }
    return await record(
        db,
        source="actions",
        fp=fp,
        severity="warning",
        title=f"{f.name or f.path or 'workflow'} failed on {f.repo}@{branch or '?'}",
        details=details,
    )


def subscribe_actions(maker: async_sessionmaker[AsyncSession]) -> Callable[..., Awaitable[None]]:
    """Subscribe to Phase 17's on_workflow_failed (in the webhook worker)."""

    async def on_failed(f: WorkflowFailure) -> None:
        async with maker() as db, db.begin():
            await record_workflow_failure(db, f)

    on_failed.__name__ = "incidents_on_workflow_failed"
    on_workflow_failed(on_failed)
    return on_failed


async def scan_actions(
    maker: async_sessionmaker[AsyncSession], window_minutes: int = 30
) -> dict[str, Any]:
    """Failed runs from github.workflow_runs (fills gaps when the webhook worker is off)."""
    since = _now() - timedelta(minutes=window_minutes)
    n = 0
    async with maker() as db, db.begin():
        runs = (
            await db.scalars(
                select(GitHubWorkflowRun).where(
                    GitHubWorkflowRun.status == "completed",
                    GitHubWorkflowRun.conclusion.in_(FAILED_CONCLUSIONS),
                    GitHubWorkflowRun.updated_at >= since,
                )
            )
        ).all()
        for r in runs:
            failure = WorkflowFailure(
                delivery_id="scan",
                repo=r.repo,
                run_id=r.id,
                workflow_id=r.workflow_id,
                name=r.name,
                path=r.path,
                branch=r.branch,
                head_sha=r.head_sha,
                event=r.event,
                conclusion=str(r.conclusion),
                run_attempt=r.run_attempt,
                html_url=r.html_url,
            )
            if await record_workflow_failure(db, failure) is not None:
                n += 1
    return {"failed_runs": n}


# --- DB / API -----------------------------------------------------------------------------


async def scan_db(maker: async_sessionmaker[AsyncSession], settings: Settings) -> dict[str, Any]:
    fp = fingerprint("db", "select1")
    start = time.perf_counter()
    error: str | None = None
    try:
        async with maker() as db, asyncio.timeout(max(1.0, settings.HEALTH_CHECK_TIMEOUT * 5)):
            await db.execute(text("SELECT 1"))
    except Exception as exc:
        error = type(exc).__name__
    ms = round((time.perf_counter() - start) * 1000, 1)
    slow = ms > settings.INCIDENT_DB_SLOW_MS
    try:
        async with maker() as db, db.begin():
            if error is None and not slow:
                await resolve_fp(db, fp, "SELECT 1 healthy")
            else:
                await record(
                    db,
                    source="db",
                    fp=fp,
                    severity="critical" if error else "warning",
                    title="database unreachable" if error else f"database slow ({ms} ms)",
                    details={
                        "latency_ms": ms,
                        "error": error,
                        "slow_ms": settings.INCIDENT_DB_SLOW_MS,
                    },
                )
    except Exception as exc:  # the DB is down: nothing to write to; the scheduler run fails
        raise RuntimeError(f"database unreachable ({error or type(exc).__name__})") from None
    return {"db_latency_ms": ms, "db_error": error}


async def scan_api(
    maker: async_sessionmaker[AsyncSession], settings: Settings, http: httpx.AsyncClient
) -> dict[str, Any]:
    url = settings.INCIDENT_API_READY_URL
    if not url:
        return {"api": "not probed"}
    fp = fingerprint("api", "ready")
    status: int | None = None
    error: str | None = None
    checks: Any = None
    try:
        resp = await http.get(url, timeout=max(2.0, settings.HEALTH_CHECK_TIMEOUT * 3))
        status = resp.status_code
        with contextlib.suppress(ValueError):
            checks = resp.json().get("checks")
    except httpx.HTTPError as exc:
        error = type(exc).__name__
    ok = status == 200
    async with maker() as db, db.begin():
        if ok:
            await resolve_fp(db, fp, "API ready")
        else:
            await record(
                db,
                source="api",
                fp=fp,
                severity="error",
                title=f"API not ready ({status or error})",
                details={"status": status, "error": error, "checks": checks},
            )
    return {"api": "ready" if ok else "not_ready"}


# --- reads / admin actions ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IncidentFilter:
    status: str | None = None
    source: str | None = None


async def list_incidents(
    db: AsyncSession, f: IncidentFilter, limit: int, offset: int
) -> tuple[Sequence[Incident], int]:
    q = select(Incident)
    c = select(func.count()).select_from(Incident)
    if f.status:
        q, c = q.where(Incident.status == f.status), c.where(Incident.status == f.status)
    if f.source:
        q, c = q.where(Incident.source == f.source), c.where(Incident.source == f.source)
    total = int(await db.scalar(c) or 0)
    rows = (
        await db.scalars(q.order_by(Incident.last_seen.desc()).limit(limit).offset(offset))
    ).all()
    return rows, total


class IncidentStateError(ValueError):
    pass


async def close(db: AsyncSession, incident_id: uuid.UUID, status: str, actor: str) -> Incident:
    inc = await db.get(Incident, incident_id, with_for_update=True)
    if inc is None:
        raise LookupError("incident not found")
    if inc.status in ("resolved", "ignored"):
        raise IncidentStateError(f"incident is already {inc.status}")
    inc.status = status
    inc.resolved_at = _now()
    inc.details = inc.details | {"_closed_by": actor}
    return inc
