"""Coding worker: runs the coding agent's tasks, one at a time.

    python -m app.workers.coding      (compose service `coding-worker`, profile `coding`)

- Claims the oldest queued agents.code_tasks row (FOR UPDATE SKIP LOCKED).
- Registers as agent `coding` (kind "coding"), heartbeats, and stops taking work while an
  admin has it paused. Each task is recorded as an agent run.
- At startup, tasks left `running` by a crash are marked failed (their worktree is removed).
- Never pushes, never merges into a protected branch, never deploys: see app/services/coding.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from app.agents.coding.git import Git
from app.agents.ollama.client import OllamaClient
from app.agents.registry import AgentRegistry, AgentSpec, Heartbeat
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.procsec import make_non_dumpable
from app.db.session import build_engine, build_sessionmaker
from app.models.agents import CodeTask
from app.services import coding

log = get_logger("worker.coding")
CODING = AgentSpec("coding", "Coding agent", "coding")


async def recover(maker: Any, cfg: coding.CodingConfig) -> int:
    """Tasks a crash left `running` are failed, and their worktrees cleaned."""
    async with maker() as db, db.begin():
        stuck = (await db.scalars(select(CodeTask).where(CodeTask.status == "running"))).all()
        for t in stuck:
            repo = cfg.repos.get(t.repo)
            if repo and t.worktree:
                with contextlib.suppress(Exception):
                    await coding._cleanup(
                        Git(repo.path), Path(t.worktree), t.branch, delete_branch=True
                    )
        if stuck:
            await db.execute(
                update(CodeTask)
                .where(CodeTask.id.in_([t.id for t in stuck]))
                .values(status="failed", error="worker restarted during the task", worktree=None)
            )
    return len(stuck)


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    # Test commands run model-patched code as our children: keep our environ/memory unreadable.
    if not make_non_dumpable():
        log.warning("coding_worker_dumpable", hint="prctl(PR_SET_DUMPABLE, 0) unavailable")
    cfg = coding.CodingConfig.from_settings(settings)
    engine = build_engine(settings)
    maker = build_sessionmaker(engine)
    registry = AgentRegistry(maker)
    ollama = OllamaClient(
        settings.OLLAMA_BASE_URL,
        connect_timeout=settings.OLLAMA_CONNECT_TIMEOUT,
        read_timeout=settings.OLLAMA_READ_TIMEOUT,
        cache_ttl=0,
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    state: dict[str, Any] = {"paused": False, "running": None}

    def report() -> tuple[str, dict[str, Any]]:
        return ("busy" if state["running"] else "idle"), {
            "running": state["running"],
            "repos": sorted(cfg.repos),
            "model": cfg.model,
        }

    def on_paused(p: bool) -> None:
        state["paused"] = p

    hb = Heartbeat(registry, CODING, settings.AGENT_HEARTBEAT_SECONDS, report, on_paused)
    await hb.beat()
    hb.start()
    recovered = await recover(maker, cfg)
    log.info("coding_worker_started", repos=sorted(cfg.repos), recovered=recovered)
    try:
        while not stop.is_set():
            tid = None if state["paused"] else await coding.claim(maker)
            if tid is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), settings.CODE_POLL_SECONDS)
                continue
            state["running"] = str(tid)
            started = time.monotonic()
            status = await coding.run_task(maker, cfg, ollama, tid)
            with contextlib.suppress(Exception):
                await registry.record_run(
                    CODING.id,
                    "code_task",
                    "succeeded" if status == "ready" else "failed",
                    int((time.monotonic() - started) * 1000),
                    details={"task_id": str(tid)},
                )
            state["running"] = None
    finally:
        await hb.stop()
        await ollama.aclose()
        await engine.dispose()
        log.info("coding_worker_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
