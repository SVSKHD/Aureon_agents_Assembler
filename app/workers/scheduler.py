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

from croniter import croniter
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.ollama.client import OllamaClient
from app.agents.registry import SCHEDULER, AgentRegistry, Heartbeat
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import build_engine, build_sessionmaker
from app.models.agents import Agent, AgentLog, AgentRun, ScheduledJob
from app.models.auth import UserSession

log = get_logger("worker.scheduler")
MAX_BACKOFF_SECONDS = 3600


@dataclass
class TaskContext:
    settings: Settings
    maker: async_sessionmaker[AsyncSession]
    ollama: OllamaClient | None


TaskFn = Callable[[TaskContext], Awaitable[dict[str, Any]]]


# --- built-in tasks -----------------------------------------------------------------------


async def purge_agent_history(ctx: TaskContext) -> dict[str, Any]:
    logs_cutoff = func.now() - timedelta(days=ctx.settings.AGENT_LOG_RETENTION_DAYS)
    runs_cutoff = func.now() - timedelta(days=ctx.settings.AGENT_RUN_RETENTION_DAYS)
    async with ctx.maker() as db, db.begin():
        logs = await db.execute(delete(AgentLog).where(AgentLog.created_at < logs_cutoff))
        runs = await db.execute(
            delete(AgentRun).where(AgentRun.started_at < runs_cutoff, AgentRun.status != "running")
        )
    return {"logs_deleted": logs.rowcount, "runs_deleted": runs.rowcount}  # type: ignore[attr-defined]


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


TASKS: dict[str, TaskFn] = {
    "purge_agent_history": purge_agent_history,
    "purge_expired_sessions": purge_expired_sessions,
    "refresh_model_inventory": refresh_model_inventory,
}

# (id, task, cron) — created if missing; an admin's later edits (pause etc.) are kept.
DEFAULT_JOBS: list[tuple[str, str, str]] = [
    ("purge-agent-history", "purge_agent_history", "17 3 * * *"),
    ("purge-expired-sessions", "purge_expired_sessions", "29 3 * * *"),
    ("refresh-model-inventory", "refresh_model_inventory", "*/30 * * * *"),
]


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
    ) -> None:
        self.settings = settings
        self.maker = maker
        self.tasks = TASKS if tasks is None else tasks
        self.registry = AgentRegistry(maker)
        self.ctx = TaskContext(settings, maker, ollama)
        self.paused = False
        self.running_job: str | None = None

    async def ensure_default_jobs(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        async with self.maker() as db, db.begin():
            for job_id, task, cron in DEFAULT_JOBS:
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
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    engine = build_engine(settings)
    maker = build_sessionmaker(engine)
    ollama = OllamaClient(
        settings.OLLAMA_BASE_URL,
        connect_timeout=settings.OLLAMA_CONNECT_TIMEOUT,
        read_timeout=60,
        cache_ttl=0,
    )
    scheduler = Scheduler(settings, maker, ollama=ollama)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)

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
        log.info("scheduler_started", jobs=[j[0] for j in DEFAULT_JOBS])
        await scheduler.serve(stopping)
    finally:
        await hb.stop()
        await ollama.aclose()
        await engine.dispose()
        log.info("scheduler_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
