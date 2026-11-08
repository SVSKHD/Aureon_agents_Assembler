"""Scheduler worker: runs code-registered tasks on cron schedules with retry + backoff.

    python -m app.workers.scheduler

- Jobs live in agents.scheduled_jobs. Postgres holds next_run_at, the retry attempt,
  failure counts and a lease (`locked_until`), so restarts lose nothing and a crashed run
  becomes due again once its lease expires.
- A job names a task in TASKS below. It is never a command line, so nothing arbitrary can be
  scheduled through the DB.
- On failure a job retries after retry_backoff_seconds * 2^(attempt-1) (capped at 1 h), up to
  max_retries times, then waits for its next cron slot. Every execution writes an agent_run
  and agent_log rows.
- Pausing the job, or the `scheduler` agent itself, stops new executions.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from croniter import croniter
from redis.asyncio import Redis
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.ollama.client import OllamaClient
from app.agents.registry import GITHUB, SCHEDULER, AgentRegistry, Heartbeat
from app.agents.trading.base import BrokerAdapter
from app.agents.trading.dhan import build_dhan_adapter
from app.agents.trading.dhan.adapter import DhanAdapter
from app.agents.trading.mt5 import build_mt5_adapter
from app.agents.trading.mt5.adapter import MT5Adapter
from app.agents.trading.types import Timeframe
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.db.session import build_engine, build_sessionmaker
from app.integrations.github import build_github_client
from app.integrations.github.client import GitHubClient, GitHubError
from app.models.agents import Agent, AgentLog, AgentRun, ScheduledJob
from app.models.auth import UserSession
from app.models.finance import FinanceTombstone
from app.models.trading import BrokerAccount
from app.services import (
    alerts,
    app_settings,
    audit,
    devops,
    dhan_connection,
    dhan_monitor,
    dhan_sync,
    github,
    github_actions,
    incidents,
    mt5_ingest,
    mt5_sync,
    news,
    paper,
    repair_recovery,
    repairs,
)
from app.services.approvals import ApprovalGate

log = get_logger("worker.scheduler")
MAX_BACKOFF_SECONDS = 3600


@dataclass
class TaskContext:
    settings: Settings
    maker: async_sessionmaker[AsyncSession]
    ollama: OllamaClient | None
    dhan: DhanAdapter | None = None
    mt5: MT5Adapter | None = None
    redis: Redis | None = None
    github: GitHubClient | None = None
    http: httpx.AsyncClient | None = None
    docker_probe: incidents.DockerProbe | None = None


TaskFn = Callable[[TaskContext], Awaitable[dict[str, Any]]]


# --- built-in tasks -----------------------------------------------------------------------


async def purge_agent_history(ctx: TaskContext) -> dict[str, Any]:
    """Old agent logs/runs, and finance delete tombstones past their retention."""
    logs_cutoff = func.now() - timedelta(days=ctx.settings.AGENT_LOG_RETENTION_DAYS)
    runs_cutoff = func.now() - timedelta(days=ctx.settings.AGENT_RUN_RETENTION_DAYS)
    tomb_cutoff = func.now() - timedelta(days=ctx.settings.FINANCE_TOMBSTONE_RETENTION_DAYS)
    async with ctx.maker() as db, db.begin():
        logs = await db.execute(delete(AgentLog).where(AgentLog.created_at < logs_cutoff))
        runs = await db.execute(
            delete(AgentRun).where(AgentRun.started_at < runs_cutoff, AgentRun.status != "running")
        )
        tombs = await db.execute(
            delete(FinanceTombstone).where(FinanceTombstone.deleted_at < tomb_cutoff)
        )
    return {
        "logs_deleted": logs.rowcount,  # type: ignore[attr-defined]
        "runs_deleted": runs.rowcount,  # type: ignore[attr-defined]
        "tombstones_deleted": tombs.rowcount,  # type: ignore[attr-defined]
    }


async def news_pull(ctx: TaskContext) -> dict[str, Any]:
    """Pull NEWS_FEED_URLS into astra_documents namespace forex (no-op when empty)."""
    async with ctx.maker() as db, db.begin():
        return await news.pull(db, ctx.settings)


async def purge_expired_sessions(ctx: TaskContext) -> dict[str, Any]:
    cutoff = func.now() - timedelta(days=7)
    async with ctx.maker() as db, db.begin():
        res = await db.execute(
            delete(UserSession).where(
                (UserSession.expires_at < cutoff) | (UserSession.revoked_at < cutoff)
            )
        )
    return {"sessions_deleted": res.rowcount}  # type: ignore[attr-defined]


async def refresh_model_inventory(ctx: TaskContext) -> dict[str, Any]:
    from app.services.chat import sync_inventory

    if ctx.ollama is None:
        raise RuntimeError("Ollama client not configured")
    models = await ctx.ollama.list_models(force=True)
    async with ctx.maker() as db, db.begin():
        await sync_inventory(db, models)
    return {"models": len(models)}


def _dhan(ctx: TaskContext) -> DhanAdapter:
    if ctx.dhan is None:
        raise RuntimeError(
            "Dhan is not configured (DHAN_CLIENT_ID + a Dhan login or DHAN_ACCESS_TOKEN)"
        )
    return ctx.dhan


async def dhan_refresh_instruments(ctx: TaskContext) -> dict[str, Any]:
    return await dhan_sync.sync_instruments(ctx.maker, _dhan(ctx))


async def dhan_reconcile(ctx: TaskContext) -> dict[str, Any]:
    """Reconcile every active Dhan account for the configured client; warn on token expiry."""
    adapter = _dhan(ctx)
    health = await adapter.health()
    out: dict[str, Any] = {"token_ok": health.ok}
    if health.credentials_expire_at is not None:
        left = health.credentials_expire_at - datetime.now(UTC)
        out["token_hours_left"] = round(left.total_seconds() / 3600, 2)
    if health.detail:
        # The admin notification: visible in GET /api/agents/scheduler/logs and account health.
        await AgentRegistry(ctx.maker).log(
            SCHEDULER.id, "warning" if health.ok else "error", f"Dhan: {health.detail}"
        )
    if not health.ok:
        raise RuntimeError(f"Dhan credentials unusable: {health.detail}")
    accounts = await dhan_sync.dhan_accounts(ctx.maker, adapter.client.client_id)
    out["accounts"] = [await dhan_sync.reconcile_account(ctx.maker, adapter, a) for a in accounts]
    return out


async def dhan_session_check(ctx: TaskContext) -> dict[str, Any]:
    """Verify the Dhan credential in use with GET /profile and store the result in
    dhan.connections (docs/28). An outage never downgrades the stored verdict."""
    v = await dhan_connection.verify_current(ctx.settings)
    async with ctx.maker() as db, db.begin():
        rec = await dhan_connection.record(db, ctx.settings, v)
        if rec is not None and (rec.changed or rec.replaced):
            audit.record(
                db,
                "dhan_connection_changed",
                repairs.SYSTEM_CLIENT,
                details={
                    "from": rec.previous_status,
                    "to": rec.row.status,
                    "outcome": v.outcome,
                    "source": v.source,
                    "credential_replaced": rec.replaced,
                },
            )
    return {
        "outcome": v.outcome,
        "status": rec.row.status if rec else None,
        "error_code": v.error_code,
    }


async def dhan_monitor_job(ctx: TaskContext) -> dict[str, Any]:
    """Auth, Redis, feed, subscriptions, staleness -> incidents (source `dhan`)."""
    return await dhan_monitor.run(ctx.maker, ctx.settings, ctx.redis)


async def dhan_backfill_candles(ctx: TaskContext) -> dict[str, Any]:
    s = ctx.settings
    tfs = [Timeframe(t) for t in s.DHAN_BACKFILL_TIMEFRAMES]
    return await dhan_sync.backfill_candles(
        ctx.maker, _dhan(ctx), s.DHAN_BACKFILL_INSTRUMENTS, tfs, s.DHAN_BACKFILL_DAYS
    )


async def mt5_sync_status(ctx: TaskContext) -> dict[str, Any]:
    """Read the MT5 bot's status export once and store heartbeat, logs, signals, positions."""
    if ctx.mt5 is None:
        raise RuntimeError("MT5 bot export is not configured (MT5_STATUS_FILE / MT5_STATUS_URL)")
    return await mt5_sync.sync_status(ctx.maker, ctx.mt5, ctx.settings)


async def mt5_ingest_watch(ctx: TaskContext) -> dict[str, Any]:
    """Push mode: open a stale_heartbeat failure when the bot stopped pushing."""
    return await mt5_ingest.watch(ctx.maker, ctx.settings)


async def mt5_ingest_purge(ctx: TaskContext) -> dict[str, Any]:
    """Push mode retention: candles, events, push ids, resolved failures."""
    return await mt5_ingest.purge(ctx.maker, ctx.settings)


def _adapters(ctx: TaskContext) -> dict[str, BrokerAdapter]:
    out: dict[str, BrokerAdapter] = {}
    if ctx.dhan is not None:
        out["dhan"] = ctx.dhan
    if ctx.mt5 is not None:
        out["mt5"] = ctx.mt5
    return out


async def paper_process(ctx: TaskContext) -> dict[str, Any]:
    """Paper trading: fill resting approved intents once marketable; expire past-day ones.
    Simulated only; never contacts a broker's order API."""
    async with ctx.maker() as db, db.begin():
        return await paper.process_open(db, ctx.redis, _adapters(ctx), ctx.settings)


async def paper_reconcile(ctx: TaskContext) -> dict[str, Any]:
    """Compare every PAPER account's simulated positions with its replayed ledger."""
    out: dict[str, Any] = {"accounts": 0, "drift": []}
    async with ctx.maker() as db, db.begin():
        accounts = (
            await db.scalars(
                select(BrokerAccount).where(
                    BrokerAccount.mode == "PAPER", BrokerAccount.is_active.is_(True)
                )
            )
        ).all()
        for account in accounts:
            row = await paper.reconcile(db, account)
            out["accounts"] += 1
            if not row.ok:
                out["drift"].append({"account": str(account.id), "items": row.drift})
    if out["drift"]:
        await AgentRegistry(ctx.maker).log(
            SCHEDULER.id,
            "error",
            f"paper reconciliation drift on {len(out['drift'])} account(s)",
            details=out,
        )
    return out


async def github_sync(ctx: TaskContext) -> dict[str, Any]:
    """GitHub connection health + repository sync; heartbeats the `github` agent."""
    client = ctx.github
    if client is None:
        raise RuntimeError("GitHub is not configured (GITHUB_TOKEN)")
    registry = AgentRegistry(ctx.maker)
    if await registry.register(GITHUB, {"fingerprint": client.fingerprint}):
        await registry.heartbeat(GITHUB.id, "idle")
        return {"skipped": "github agent is paused"}
    conn = await github.check_connection(ctx.maker, client, ctx.settings)
    out: dict[str, Any] = {"ok": conn.ok, "login": conn.login}
    error = None if conn.ok else conn.detail or "connection check failed"
    if error is None:
        try:
            run = await github.sync_repositories(
                ctx.maker, client, ctx.settings, trigger="scheduler"
            )
            out |= {
                "sync_run_id": run.id,
                "repos_upserted": run.repos_upserted,
                "repos_removed": run.repos_removed,
            }
        except GitHubError as exc:
            error = f"{exc.code}: {exc.message}"
    details = {
        "fingerprint": client.fingerprint,
        "login": conn.login,
        "ok": error is None,
        "detail": error,
        "rate_limit": client.rate_limit.as_dict() if client.rate_limit else None,
        "stale_after_s": ctx.settings.GITHUB_AGENT_STALE_SECONDS,
        **{k: v for k, v in out.items() if k.startswith("repos_")},
    }
    await registry.heartbeat(GITHUB.id, "error" if error else "idle", details)
    if error:
        await registry.log(GITHUB.id, "error", f"GitHub: {error}")
        raise RuntimeError(f"GitHub sync failed: {error}")
    return out


async def github_actions_sync(ctx: TaskContext) -> dict[str, Any]:
    """Recent workflow runs of the allowlisted repos -> github.workflow_runs
    (docs/20-github-actions-webhooks.md). Webhooks keep the table current; this fills gaps."""
    client = ctx.github
    if client is None:
        raise RuntimeError("GitHub is not configured (GITHUB_TOKEN)")
    # Read the pause flag only: github_sync owns the `github` agent's heartbeat details.
    if await AgentRegistry(ctx.maker).is_paused(GITHUB.id):
        return {"skipped": "github agent is paused"}
    out = await github_actions.sync_actions(ctx.maker, client, ctx.settings)
    if out["errors"] and not out["runs_upserted"]:
        raise RuntimeError(f"GitHub Actions sync failed: {'; '.join(out['errors'][:5])}")
    return out


async def devops_backfill(ctx: TaskContext) -> dict[str, Any]:
    """Recent PRs and GitHub deployments of the allowlisted repos -> devops.* (fills what the
    webhooks missed during an outage; idempotent). Actions runs: github_actions_sync."""
    client = ctx.github
    if client is None:
        raise RuntimeError("GitHub is not configured (GITHUB_TOKEN)")
    if await AgentRegistry(ctx.maker).is_paused(GITHUB.id):
        return {"skipped": "github agent is paused"}
    out = await devops.backfill(ctx.maker, client, ctx.settings)
    if out["errors"] and not out["pr_events"] and not out["deployments"]:
        raise RuntimeError(f"DevOps backfill failed: {'; '.join(out['errors'][:5])}")
    return out


async def devops_purge(ctx: TaskContext) -> dict[str, Any]:
    """DevOps retention: feed + repo events, collector snapshots, old deployment records."""
    async with ctx.maker() as db, db.begin():
        return await devops.purge(db, ctx.settings)


async def incident_scan(ctx: TaskContext) -> dict[str, Any]:
    """Incident detection + safe recovery / escalation (docs/21-self-repair.md). Each probe
    runs even when another fails; a failing probe fails the run after the rest are done."""
    out: dict[str, Any] = {}
    errors: list[str] = []
    probes: list[tuple[str, Callable[[], Awaitable[dict[str, Any]]]]] = [
        ("db", lambda: incidents.scan_db(ctx.maker, ctx.settings)),
        ("heartbeats", lambda: incidents.scan_heartbeats(ctx.maker, ctx.settings)),
        ("actions", lambda: incidents.scan_actions(ctx.maker)),
        (
            "docker",
            lambda: incidents.scan_docker(
                ctx.maker, ctx.docker_probe or incidents.unknown_docker_probe
            ),
        ),
    ]
    if ctx.http is not None:
        http = ctx.http
        probes.append(("api", lambda: incidents.scan_api(ctx.maker, ctx.settings, http)))
    for name, probe in probes:
        try:
            out |= await probe()
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}"[:300])
    deps = repair_recovery.Deps(ctx.maker, ctx.settings, ctx.redis, ctx.github, ctx.http)
    try:
        out["recovery"] = await repair_recovery.process_incidents(deps)
    except Exception as exc:
        errors.append(f"recovery: {type(exc).__name__}"[:300])
    out["exceptions_flushed"] = await incidents.flush_exceptions(
        ctx.maker, ctx.settings, "scheduler"
    )
    if errors:
        raise RuntimeError("; ".join(errors))
    return out


async def approval_expire(ctx: TaskContext) -> dict[str, Any]:
    """Mark due pending approval requests `expired` (an event + audit row + notification
    each). Expiry is also lazy on every read, so this only makes it timely
    (docs/23-approval-engine.md)."""
    gate = ApprovalGate(ctx.maker, ctx.settings)
    total = 0
    while True:
        async with ctx.maker() as db, db.begin():
            n = await gate.expire_due(db, limit=200)
        total += n
        if n < 200:
            return {"expired": total}


async def alerts_dispatch(ctx: TaskContext) -> dict[str, Any]:
    """Open incidents, stale heartbeats, backup problems and expiring approvals → Discord,
    deduplicated per fingerprint (docs/24-production-integration.md). No-op without
    DISCORD_WEBHOOK_URL."""
    return await alerts.dispatch(ctx.maker, ctx.settings, ctx.redis)


TASKS: dict[str, TaskFn] = {
    "purge_agent_history": purge_agent_history,
    "purge_expired_sessions": purge_expired_sessions,
    "refresh_model_inventory": refresh_model_inventory,
    "dhan_refresh_instruments": dhan_refresh_instruments,
    "dhan_reconcile": dhan_reconcile,
    "dhan_backfill_candles": dhan_backfill_candles,
    "dhan_session_check": dhan_session_check,
    "dhan_monitor": dhan_monitor_job,
    "mt5_sync_status": mt5_sync_status,
    "mt5_ingest_watch": mt5_ingest_watch,
    "mt5_ingest_purge": mt5_ingest_purge,
    "paper_process": paper_process,
    "paper_reconcile": paper_reconcile,
    "news_pull": news_pull,
    "github_sync": github_sync,
    "github_actions_sync": github_actions_sync,
    "incident_scan": incident_scan,
    "approval_expire": approval_expire,
    "alerts_dispatch": alerts_dispatch,
    "devops_backfill": devops_backfill,
    "devops_purge": devops_purge,
}

# (id, task, cron) — created if missing; an admin's later edits (pause etc.) are kept.
DEFAULT_JOBS: list[tuple[str, str, str]] = [
    ("purge-agent-history", "purge_agent_history", "17 3 * * *"),
    ("purge-expired-sessions", "purge_expired_sessions", "29 3 * * *"),
    ("refresh-model-inventory", "refresh_model_inventory", "*/30 * * * *"),
    ("paper-process", "paper_process", "* * * * *"),
    ("paper-reconcile", "paper_reconcile", "*/15 * * * *"),
]


def default_jobs(settings: Settings) -> list[tuple[str, str, str]]:
    """Built-in jobs, plus the Dhan / MT5 jobs when those are configured."""
    jobs = list(DEFAULT_JOBS)
    jobs.append(("approval-expire", "approval_expire", settings.APPROVALS_EXPIRE_CRON))
    if settings.INCIDENT_SCAN_CRON:
        jobs.append(("incident-scan", "incident_scan", settings.INCIDENT_SCAN_CRON))
    if settings.ALERTS_CRON and settings.discord_webhook_url:
        jobs.append(("alerts-dispatch", "alerts_dispatch", settings.ALERTS_CRON))
    if settings.DHAN_CLIENT_ID:
        # Even without a usable token: an expired/rejected credential must stay visible.
        jobs.append(("dhan-session-check", "dhan_session_check", settings.DHAN_SESSION_CHECK_CRON))
        jobs.append(("dhan-monitor", "dhan_monitor", settings.DHAN_MONITOR_CRON))
    if settings.dhan_configured:
        jobs += [
            (
                "dhan-refresh-instruments",
                "dhan_refresh_instruments",
                settings.DHAN_INSTRUMENTS_CRON,
            ),
            ("dhan-reconcile", "dhan_reconcile", settings.DHAN_RECONCILE_CRON),
        ]
        if settings.DHAN_BACKFILL_INSTRUMENTS:
            jobs.append(
                ("dhan-backfill-candles", "dhan_backfill_candles", settings.DHAN_BACKFILL_CRON)
            )
    if settings.mt5_configured:
        jobs.append(("mt5-sync-status", "mt5_sync_status", settings.MT5_SYNC_CRON))
    if settings.mt5_ingest_enabled:
        jobs.append(("mt5-ingest-watch", "mt5_ingest_watch", settings.MT5_INGEST_WATCH_CRON))
        jobs.append(("mt5-ingest-purge", "mt5_ingest_purge", settings.MT5_INGEST_PURGE_CRON))
    if settings.NEWS_FEED_URLS:
        jobs.append(("news-pull", "news_pull", settings.NEWS_PULL_CRON))
    if settings.DEVOPS_PURGE_CRON:
        jobs.append(("devops-purge", "devops_purge", settings.DEVOPS_PURGE_CRON))
    if settings.github_configured:
        jobs.append(("github-sync", "github_sync", settings.GITHUB_SYNC_CRON))
        jobs.append(
            ("github-actions-sync", "github_actions_sync", settings.GITHUB_ACTIONS_SYNC_CRON)
        )
        if settings.DEVOPS_BACKFILL_CRON:
            jobs.append(("devops-backfill", "devops_backfill", settings.DEVOPS_BACKFILL_CRON))
    return jobs


# --- scheduling -----------------------------------------------------------------------------


def next_fire(cron: str, after: datetime, tz: str) -> datetime:
    local = after.astimezone(ZoneInfo(tz))
    nxt: datetime = croniter(cron, local).get_next(datetime)
    return nxt.astimezone(UTC)


def backoff(base_seconds: int, attempt: int) -> timedelta:
    return timedelta(seconds=min(MAX_BACKOFF_SECONDS, base_seconds * 2 ** (attempt - 1)))


class Scheduler:
    def __init__(
        self,
        settings: Settings,
        maker: async_sessionmaker[AsyncSession],
        tasks: dict[str, TaskFn] | None = None,
        ollama: OllamaClient | None = None,
        dhan: DhanAdapter | None = None,
        mt5: MT5Adapter | None = None,
        redis: Redis | None = None,
        github: GitHubClient | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.maker = maker
        self.tasks = TASKS if tasks is None else tasks
        self.registry = AgentRegistry(maker)
        self.ctx = TaskContext(settings, maker, ollama, dhan, mt5, redis, github, http)
        self.paused = False
        self.running_job: str | None = None

    async def ensure_default_jobs(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        async with self.maker() as db, db.begin():
            for job_id, task, cron in default_jobs(self.settings):
                await db.execute(
                    insert(ScheduledJob)
                    .values(
                        id=job_id,
                        agent_id=SCHEDULER.id,
                        task=task,
                        cron=cron,
                        next_run_at=next_fire(cron, now, self.settings.SCHEDULER_TIMEZONE),
                    )
                    .on_conflict_do_nothing(index_elements=[ScheduledJob.id])
                )

    async def _claim(self) -> ScheduledJob | None:
        async with self.maker() as db, db.begin():
            due = (
                select(ScheduledJob.id)
                .where(
                    ScheduledJob.paused.is_(False),
                    ScheduledJob.next_run_at <= func.now(),
                    (ScheduledJob.locked_until.is_(None))
                    | (ScheduledJob.locked_until < func.now()),
                    ~exists().where(Agent.id == ScheduledJob.agent_id, Agent.paused.is_(True)),
                )
                .order_by(ScheduledJob.next_run_at)
                .limit(1)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            job: ScheduledJob | None = await db.scalar(
                update(ScheduledJob)
                .where(ScheduledJob.id == due)
                .values(
                    locked_until=func.now()
                    + func.make_interval(0, 0, 0, 0, 0, 0, ScheduledJob.timeout_seconds + 30),
                    updated_at=func.now(),
                )
                .returning(ScheduledJob)
            )
            return job

    async def tick(self) -> bool:
        """Run one due job. Returns False when nothing was due."""
        job = await self._claim()
        if job is None:
            return False
        await self._execute(job)
        return True

    async def _execute(self, job: ScheduledJob) -> None:
        attempt = job.attempt + 1
        run_id = await self.registry.start_run(
            job.agent_id, job.task, attempt=attempt, scheduled_job_id=job.id
        )
        fn = self.tasks.get(job.task)
        self.running_job = job.id
        error: str | None = None
        result: dict[str, Any] = {}
        try:
            if fn is None:
                raise LookupError(f"unknown task {job.task!r}")
            async with asyncio.timeout(job.timeout_seconds):
                result = await fn(self.ctx)
        except TimeoutError:
            error = f"timed out after {job.timeout_seconds}s"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:2000]
        finally:
            self.running_job = None

        now = datetime.now(UTC)
        tz = self.settings.SCHEDULER_TIMEZONE
        values: dict[str, Any] = {
            "last_run_at": func.now(),
            "locked_until": None,
            "updated_at": func.now(),
        }
        if error is None:
            values |= {
                "attempt": 0,
                "consecutive_failures": 0,
                "last_status": "succeeded",
                "last_error": None,
                "next_run_at": next_fire(job.cron, now, tz),
            }
            status, level, msg = "succeeded", "info", f"{job.task} succeeded"
        elif fn is not None and attempt <= job.max_retries:
            values |= {
                "attempt": attempt,
                "last_status": "retrying",
                "last_error": error,
                "next_run_at": now + backoff(job.retry_backoff_seconds, attempt),
            }
            status, level, msg = "failed", "warning", f"{job.task} failed, retry {attempt}"
        else:
            values |= {
                "attempt": 0,
                "consecutive_failures": job.consecutive_failures + 1,
                "last_status": "failed",
                "last_error": error,
                "next_run_at": next_fire(job.cron, now, tz),
            }
            status, level, msg = "failed", "error", f"{job.task} failed, retries exhausted"

        async with self.maker() as db, db.begin():
            await db.execute(update(ScheduledJob).where(ScheduledJob.id == job.id).values(**values))
        await self.registry.finish_run(run_id, status, error=error, details=result)
        await self.registry.log(
            job.agent_id, level, msg, run_id=run_id, job_id=job.id, error=error, **result
        )
        log.info("scheduled_job", job_id=job.id, status=status, attempt=attempt, error=error)

    async def serve(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            if not self.paused:
                while not stopping.is_set() and not self.paused and await self.tick():
                    pass
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), self.settings.SCHEDULER_TICK_SECONDS)


async def main() -> int:
    settings = await app_settings.worker_settings()
    configure_logging(settings.LOG_LEVEL)
    engine = build_engine(settings)
    maker = build_sessionmaker(engine)
    ollama = OllamaClient(
        settings.OLLAMA_BASE_URL,
        connect_timeout=settings.OLLAMA_CONNECT_TIMEOUT,
        read_timeout=60,
        cache_ttl=0,
    )
    redis = Redis.from_url(settings.REDIS_URL)
    dhan = build_dhan_adapter(settings, redis, maker)
    mt5 = build_mt5_adapter(settings)
    gh_client = build_github_client(settings)
    http = httpx.AsyncClient(timeout=10.0)
    scheduler = Scheduler(
        settings, maker, ollama=ollama, dhan=dhan, mt5=mt5, redis=redis, github=gh_client,
        http=http,
    )  # fmt: skip
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    # A change to its keys from Spasta re-execs this worker (docs/settings.md).
    app_settings.start_worker_watch(
        settings, maker, stopping, ("DHAN_", "GITHUB_", "DISCORD_", "MT5_"), redis
    )

    def state() -> tuple[str, dict[str, Any]]:
        return (
            "busy" if scheduler.running_job else "idle",
            {"running_job": scheduler.running_job, "tasks": sorted(scheduler.tasks)},
        )

    def on_paused(p: bool) -> None:
        scheduler.paused = p

    hb = Heartbeat(
        scheduler.registry, SCHEDULER, settings.AGENT_HEARTBEAT_SECONDS, state, on_paused
    )
    await hb.beat()  # register before seeding jobs (FK to agents.agents)
    hb.start()
    try:
        await scheduler.ensure_default_jobs()
        log.info("scheduler_started", jobs=[j[0] for j in default_jobs(settings)])
        await scheduler.serve(stopping)
    finally:
        await hb.stop()
        await ollama.aclose()
        if dhan is not None:
            await dhan.aclose()
        if mt5 is not None:
            await mt5.aclose()
        if gh_client is not None:
            await gh_client.aclose()
        await http.aclose()
        await redis.aclose()
        await engine.dispose()
        log.info("scheduler_stopped")
    return 0


if __name__ == "__main__":
    app_settings.run_worker(main, "app.workers.scheduler")
