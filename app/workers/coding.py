"""Coding worker: runs the coding agent's tasks, one at a time.

    python -m app.workers.coding      (compose service `coding-worker`, profile `coding`)

- Claims the oldest queued agents.code_tasks row (FOR UPDATE SKIP LOCKED).
- Registers as agent `coding` (kind "coding"), heartbeats, and stops taking work while an
  admin has it paused. Each task is recorded as an agent run.
- At startup, tasks left `running` by a crash are marked failed (their worktree is removed).
- Never merges, never deploys: see app/services/coding.py. Its only network git is the
  git-ops loop (app/services/coding_git.py): clone / pull allowlisted repos and push feature
  branches + open a PR, one op at a time under HEAVY_MODEL_LOCK, so no model-written test
  process runs while a git child holds the token in its environment.
- Also runs the self-repair loop (agent `repair`, docs/21-self-repair.md): one repair at a
  time. Both loops hold HEAVY_MODEL_LOCK for a whole task, so a code task and a repair never
  run a model (or a test suite) at the same time. Repairs reach GitHub only through the REST
  client, after their allowlisted tests pass.
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
from app.agents.ollama.heavy import HEAVY_MODEL_LOCK
from app.agents.registry import AgentRegistry, AgentSpec, Heartbeat
from app.core.logging import configure_logging, get_logger
from app.core.procsec import make_non_dumpable
from app.db.session import build_engine, build_sessionmaker
from app.integrations.github import build_github_client
from app.integrations.github.client import GitHubClient
from app.models.agents import CodeTask
from app.services import app_settings, coding, coding_git, incidents, repairs

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


async def repair_loop(
    maker: Any,
    settings: Any,
    cfg: repairs.RepairConfig,
    ollama: OllamaClient,
    github: GitHubClient | None,
    registry: AgentRegistry,
    stop: asyncio.Event,
    state: dict[str, Any],
) -> None:
    while not stop.is_set():
        rid = None
        try:
            rid = await repairs.claim(maker, settings)
        except Exception as exc:  # DB hiccup: back off
            log.warning("repair_claim_failed", error=type(exc).__name__)
        if rid is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.REPAIR_POLL_SECONDS)
            continue
        state["repair"] = str(rid)
        started = time.monotonic()
        async with HEAVY_MODEL_LOCK:
            status = await repairs.run_repair(maker, settings, cfg, ollama, github, rid)
        with contextlib.suppress(Exception):
            await registry.record_run(
                repairs.REPAIR.id,
                "repair_task",
                "succeeded" if status == "pr_opened" else "failed",
                int((time.monotonic() - started) * 1000),
                details={"repair_id": str(rid), "status": status},
            )
        state["repair"] = None


async def git_loop(
    maker: Any,
    settings: Any,
    github: GitHubClient | None,
    registry: AgentRegistry,
    stop: asyncio.Event,
    state: dict[str, Any],
) -> None:
    while not stop.is_set():
        oid = None
        if not state["paused"]:
            try:
                oid = await coding_git.claim(maker)
            except Exception as exc:  # DB hiccup: back off
                log.warning("git_op_claim_failed", error=type(exc).__name__)
        if oid is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.CODE_POLL_SECONDS)
            continue
        state["git"] = str(oid)
        started = time.monotonic()
        status = "failed"
        async with HEAVY_MODEL_LOCK:
            with contextlib.suppress(Exception):  # e.g. the DB went away mid-op: keep looping
                status = await coding_git.run_op(maker, settings, github, oid)
        with contextlib.suppress(Exception):
            await registry.record_run(
                CODING.id,
                "code_git_op",
                "succeeded" if status == "done" else "failed",
                int((time.monotonic() - started) * 1000),
                details={"op_id": str(oid)},
            )
        state["git"] = None


async def main() -> int:
    settings = await app_settings.worker_settings()
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
    # A change to its keys from Spasta re-execs this worker (docs/settings.md).
    app_settings.start_worker_watch(settings, maker, stop, ("GITHUB_",), None)
    state: dict[str, Any] = {"paused": False, "running": None}

    def report() -> tuple[str, dict[str, Any]]:
        return ("busy" if state["running"] else "idle"), {
            "running": state["running"],
            "repos": sorted(cfg.repos),
            "model": cfg.model,
            "git_op": state.get("git"),
        }

    def on_paused(p: bool) -> None:
        state["paused"] = p

    hb = Heartbeat(registry, CODING, settings.AGENT_HEARTBEAT_SECONDS, report, on_paused)
    await hb.beat()
    hb.start()
    recovered = await recover(maker, cfg)
    log.info("coding_worker_started", repos=sorted(cfg.repos), recovered=recovered)

    # --- self-repair loop (same process: shares the heavy-model lock) ---
    rcfg = repairs.RepairConfig.from_settings(settings)
    github = build_github_client(settings)

    def repair_report() -> tuple[str, dict[str, Any]]:
        return ("busy" if state.get("repair") else "idle"), {
            "running": state.get("repair"),
            "model": rcfg.model,
            "repo": rcfg.repo,
            "disabled": repairs.disabled_reason(settings),
        }

    repair_hb = Heartbeat(registry, repairs.REPAIR, settings.AGENT_HEARTBEAT_SECONDS, repair_report)
    await repair_hb.beat()
    repair_hb.start()
    await repairs.recover_stuck(maker, rcfg)
    repair_task = asyncio.create_task(
        repair_loop(maker, settings, rcfg, ollama, github, registry, stop, state),
        name="repair-loop",
    )
    await coding_git.recover(maker)
    git_task = asyncio.create_task(
        git_loop(maker, settings, github, registry, stop, state), name="git-ops-loop"
    )
    flusher = incidents.start_exception_flusher(maker, settings, "coding-worker")
    try:
        while not stop.is_set():
            tid = None if state["paused"] else await coding.claim(maker)
            if tid is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), settings.CODE_POLL_SECONDS)
                continue
            state["running"] = str(tid)
            started = time.monotonic()
            # Rebuilt per task: a clone made since startup is a coding repo too.
            with contextlib.suppress(ValueError):
                cfg = coding.CodingConfig.from_settings(settings)
            async with HEAVY_MODEL_LOCK:
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
        stop.set()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(repair_task, 60)
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.wait_for(git_task, 60)
        await incidents.stop_exception_flusher(flusher)
        await repair_hb.stop()
        await hb.stop()
        if github is not None:
            await github.aclose()
        await ollama.aclose()
        await engine.dispose()
        log.info("coding_worker_stopped")
    return 0


if __name__ == "__main__":
    app_settings.run_worker(main, "app.workers.coding")
