"""Agent registry: each process registers its agents, heartbeats, and records runs + logs.

Every write here is best-effort from the caller's point of view: a registry failure (for
example the DB is briefly down) is logged and must never break the agent's real work.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.heartbeat import touch_heartbeat_file
from app.core.logging import get_logger
from app.models.agents import Agent, AgentLog, AgentRun

log = get_logger(__name__)
VERSION = "0.1.0"


@dataclass(frozen=True, slots=True)
class AgentSpec:
    id: str
    name: str
    kind: str


OLLAMA = AgentSpec("ollama", "Ollama chat", "llm")
IMAGING = AgentSpec("imaging", "Stable Diffusion worker", "imaging")
SCHEDULER = AgentSpec("scheduler", "Scheduler", "scheduler")
# Heartbeat comes from the scheduler's `github_sync` task (docs/18-github-foundation.md).
GITHUB = AgentSpec("github", "GitHub integration", "integration")


def effective_state(agent: Agent, stale_after: float, now: datetime | None = None) -> str:
    """paused > stale > reported state. An agent may carry its own window in
    `details.stale_after_s` (e.g. the MT5 bot, whose heartbeat is its export time)."""
    if agent.paused:
        return "paused"
    own = (agent.details or {}).get("stale_after_s")
    if isinstance(own, int | float) and not isinstance(own, bool) and own > 0:
        stale_after = float(own)
    now = now or datetime.now(UTC)
    if agent.last_heartbeat_at is None or (
        now - agent.last_heartbeat_at > timedelta(seconds=stale_after)
    ):
        return "stale"
    return agent.state


class AgentRegistry:
    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self.maker = maker

    async def register(self, spec: AgentSpec, details: dict[str, Any] | None = None) -> bool:
        """Upsert the agent row (keeps `paused`). Returns the current paused flag."""
        values = {
            "id": spec.id,
            "name": spec.name,
            "kind": spec.kind,
            "state": "idle",
            "version": VERSION,
            "host": socket.gethostname()[:255],
            "pid": os.getpid(),
            "details": details or {},
        }
        ins = insert(Agent).values(**values, last_heartbeat_at=func.now())
        upsert = ins.on_conflict_do_update(
            index_elements=[Agent.id],
            set_={k: ins.excluded[k] for k in values if k != "id"}
            | {"last_heartbeat_at": func.now()},
        ).returning(Agent.paused)
        async with self.maker() as db, db.begin():
            return bool(await db.scalar(upsert))

    async def heartbeat(
        self, agent_id: str, state: str, details: dict[str, Any] | None = None
    ) -> bool:
        """Record liveness; returns whether an admin has paused this agent."""
        values: dict[str, Any] = {"state": state, "last_heartbeat_at": func.now()}
        if details is not None:
            values["details"] = details
        async with self.maker() as db, db.begin():
            paused = await db.scalar(
                update(Agent).where(Agent.id == agent_id).values(**values).returning(Agent.paused)
            )
        return bool(paused)

    async def mark_stopped(self, agent_id: str) -> None:
        async with self.maker() as db, db.begin():
            await db.execute(
                update(Agent)
                .where(Agent.id == agent_id)
                .values(state="stopped", last_heartbeat_at=func.now())
            )

    async def is_paused(self, agent_id: str) -> bool:
        async with self.maker() as db:
            return bool(await db.scalar(select(Agent.paused).where(Agent.id == agent_id)))

    async def start_run(
        self,
        agent_id: str,
        task: str,
        *,
        attempt: int = 1,
        scheduled_job_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> int:
        async with self.maker() as db, db.begin():
            run = AgentRun(
                agent_id=agent_id,
                task=task,
                attempt=attempt,
                scheduled_job_id=scheduled_job_id,
                details=details or {},
            )
            db.add(run)
            await db.execute(
                update(Agent).where(Agent.id == agent_id).values(last_activity_at=func.now())
            )
            await db.flush()
            return run.id

    async def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "finished_at": func.now(),
            "error": error,
            "duration_ms": (func.extract("epoch", func.now() - AgentRun.started_at) * 1000).cast(
                AgentRun.duration_ms.type
            ),
        }
        if details is not None:
            values["details"] = AgentRun.details.op("||")(details)
        async with self.maker() as db, db.begin():
            await db.execute(update(AgentRun).where(AgentRun.id == run_id).values(**values))

    async def record_run(
        self,
        agent_id: str,
        task: str,
        status: str,
        duration_ms: int,
        *,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """One-shot run row for work that is timed elsewhere (e.g. a chat turn)."""
        async with self.maker() as db, db.begin():
            db.add(
                AgentRun(
                    agent_id=agent_id,
                    task=task,
                    status=status,
                    duration_ms=duration_ms,
                    error=error,
                    details=details or {},
                    finished_at=func.now(),
                )
            )
            await db.execute(
                update(Agent).where(Agent.id == agent_id).values(last_activity_at=func.now())
            )

    async def log(
        self, agent_id: str, level: str, message: str, *, run_id: int | None = None, **context: Any
    ) -> None:
        async with self.maker() as db, db.begin():
            db.add(
                AgentLog(
                    agent_id=agent_id,
                    run_id=run_id,
                    level=level,
                    message=message[:4000],
                    context=context,
                )
            )


@dataclass
class Heartbeat:
    """Background task: register once, then heartbeat every `interval` seconds.

    `state_fn` reports the current state + details; `on_paused` receives the admin's flag.
    """

    registry: AgentRegistry
    spec: AgentSpec
    interval: float
    state_fn: Callable[[], tuple[str, dict[str, Any]]]
    on_paused: Callable[[bool], None] = lambda _p: None
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    registered: bool = field(default=False, init=False)

    async def beat(self) -> None:
        state, details = self.state_fn()
        try:
            if not self.registered:
                paused = await self.registry.register(self.spec, details)
                self.registered = True
                await self.registry.heartbeat(self.spec.id, state)
            else:
                paused = await self.registry.heartbeat(self.spec.id, state, details)
            self.on_paused(paused)
        except Exception as exc:  # DB hiccup: keep the agent running, try again next beat
            log.warning("heartbeat_failed", agent=self.spec.id, error=type(exc).__name__)
            return
        # The container healthcheck (python -m app.workers.healthcheck) reads this file's age.
        await asyncio.to_thread(touch_heartbeat_file)

    async def _loop(self) -> None:
        while True:
            await self.beat()
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name=f"heartbeat:{self.spec.id}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self.registered:
            with contextlib.suppress(Exception):
                await self.registry.mark_stopped(self.spec.id)
