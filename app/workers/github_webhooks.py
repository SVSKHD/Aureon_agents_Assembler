"""GitHub webhook worker: processes stored deliveries (docs/20-github-actions-webhooks.md).

    python -m app.workers.github_webhooks   (compose service `github-webhook-worker`,
                                             profile `github`)

- Registers as agent `github-webhooks`, heartbeats, and stops taking work while an admin has
  it paused.
- Waits on the Redis list `github:webhooks` (BRPOP) for new delivery ids, and after every
  wake-up also scans github.webhook_events for due pending events (FOR UPDATE SKIP LOCKED):
  that scan picks up retries, anything the list lost (Redis restart, trimmed list) and
  events stored while Redis was down. Several workers may run; a row is processed once.
- workflow_run / workflow_job upsert github.workflow_runs / workflow_jobs; pull_request,
  issues, push and ping are recorded. Failures retry with exponential backoff, and an event
  is marked `failed` after GITHUB_WEBHOOK_MAX_ATTEMPTS.
- `on_workflow_failed(fn)` (re-exported from app.services.github_webhooks) is the hook Phase
  18 (self-repair) subscribes to: `incidents.subscribe_actions` records each failed run as
  an `actions` incident (docs/21-self-repair.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.registry import AgentRegistry, AgentSpec, Heartbeat
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.db.session import build_engine, build_sessionmaker
from app.services import app_settings, incidents
from app.services import github_webhooks as hooks
from app.services.github_webhooks import WorkflowFailure, on_workflow_failed

log = get_logger("worker.github_webhooks")
GITHUB_WEBHOOKS = AgentSpec("github-webhooks", "GitHub webhook worker", "integration")
SCAN_BATCH = 50


class WebhookWorker:
    def __init__(
        self,
        settings: Settings,
        maker: async_sessionmaker[AsyncSession],
        redis: Redis | None,
    ) -> None:
        self.settings = settings
        self.maker = maker
        self.redis = redis
        self.paused = False
        self.processed = 0
        self.failed = 0
        self.last_error: str | None = None

    def _count(self, results: list[hooks.Processed]) -> None:
        for r in results:
            if r.status == "processed":
                self.processed += 1
            elif r.status == "failed":
                self.failed += 1
            if r.error:
                self.last_error = r.error[:300]

    async def _pop(self) -> str | None:
        """Wait up to GITHUB_WEBHOOK_POLL_SECONDS for a queued delivery id."""
        wait = max(1, int(self.settings.GITHUB_WEBHOOK_POLL_SECONDS))
        if self.redis is None:
            await asyncio.sleep(wait)
            return None
        try:
            item = await self.redis.brpop([hooks.QUEUE_KEY], timeout=wait)
        except RedisTimeoutError:  # redis-py >= 8: an empty blocking pop, not an outage
            return None
        except Exception as exc:  # Redis down: the DB scan still works
            log.warning("github_webhooks_redis_error", error=type(exc).__name__)
            await asyncio.sleep(wait)
            return None
        if not item:
            return None
        raw = item[1]
        value = raw.decode() if isinstance(raw, bytes) else str(raw)
        return value if hooks.DELIVERY_RE.fullmatch(value) else None

    async def step(self) -> int:
        """One wake-up: the queued delivery (if any), then the due events. -> how many."""
        results: list[hooks.Processed] = []
        delivery = await self._pop()
        if self.paused:
            if delivery is not None and self.redis is not None:
                with contextlib.suppress(Exception):
                    await self.redis.rpush(hooks.QUEUE_KEY, delivery)  # put it back
            return 0
        if delivery is not None:
            res = await hooks.process_next(self.maker, self.settings, delivery_id=delivery)
            if res is not None:
                results.append(res)
        results += await hooks.drain(self.maker, self.settings, SCAN_BATCH)
        self._count(results)
        return len(results)

    def state(self) -> tuple[str, dict[str, Any]]:
        return "idle", {
            "processed": self.processed,
            "failed": self.failed,
            "last_error": self.last_error,
            "queue": hooks.QUEUE_KEY,
        }


async def main() -> int:
    settings = await app_settings.worker_settings()
    configure_logging(settings.LOG_LEVEL)
    engine = build_engine(settings)
    maker = build_sessionmaker(engine)
    redis = Redis.from_url(settings.REDIS_URL)
    worker = WebhookWorker(settings, maker, redis)
    registry = AgentRegistry(maker)
    incidents.subscribe_actions(maker)
    flusher = incidents.start_exception_flusher(maker, settings, "github-webhook-worker")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    # A change to its keys from Spasta re-execs this worker (docs/settings.md).
    app_settings.start_worker_watch(settings, maker, stop, ("GITHUB_",), redis)

    def on_paused(p: bool) -> None:
        worker.paused = p

    hb = Heartbeat(
        registry, GITHUB_WEBHOOKS, settings.AGENT_HEARTBEAT_SECONDS, worker.state, on_paused
    )
    await hb.beat()
    hb.start()
    log.info(
        "github_webhooks_started",
        configured=settings.github_webhook_configured,
        max_attempts=settings.GITHUB_WEBHOOK_MAX_ATTEMPTS,
    )
    try:
        while not stop.is_set():
            step = asyncio.ensure_future(worker.step())
            stopping = asyncio.ensure_future(stop.wait())
            await asyncio.wait({step, stopping}, return_when=asyncio.FIRST_COMPLETED)
            if not step.done():  # stop requested while blocked on BRPOP
                step.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await step
            else:
                stopping.cancel()
                try:
                    step.result()
                except Exception as exc:  # a DB hiccup: back off, keep running
                    log.warning("github_webhooks_step_failed", error=type(exc).__name__)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), settings.GITHUB_WEBHOOK_POLL_SECONDS)
    finally:
        await incidents.stop_exception_flusher(flusher)
        await hb.stop()
        await redis.aclose()
        await engine.dispose()
        log.info("github_webhooks_stopped")
    return 0


__all__ = ["GITHUB_WEBHOOKS", "WebhookWorker", "WorkflowFailure", "main", "on_workflow_failed"]


if __name__ == "__main__":
    app_settings.run_worker(main, "app.workers.github_webhooks")
