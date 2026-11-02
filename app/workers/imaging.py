"""Image worker: a separate process that runs exactly one generation at a time.

    python -m app.workers.imaging

- Refuses to start unless the HDD is mounted (marker file) and the generator is configured.
- Holds a Redis lock so only one worker instance runs.
- Claims the oldest queued job from Postgres (FOR UPDATE SKIP LOCKED). The Redis list only
  wakes it up; a lost wake-up is covered by re-checking the table every few seconds.
- At startup, jobs left `running` by a crash are retried until they have started
  IMAGE_MAX_ATTEMPTS times, then marked failed. On SIGTERM a running job is stopped and put
  back in the queue.
- Cancel: the worker checks `cancel_requested` every IMAGE_POLL_SECONDS and kills the process.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.imaging.generator import (
    GenerationFailed,
    GenerationRequest,
    ImageGenerator,
    build_generator,
    png_dimensions,
)
from app.agents.registry import IMAGING, AgentRegistry, Heartbeat
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import build_engine, build_sessionmaker
from app.models.imaging import GeneratedAsset, ImageJob
from app.services.health import check_data_root
from app.services.imaging import output_path

log = get_logger("worker.imaging")

LOCK_KEY = "aureon:imaging:worker-lock"
RUN_STATUS = {
    "done": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "requeue": "cancelled",
}
LOCK_TTL = 30


class Shutdown(Exception):
    pass


class ImagingWorker:
    def __init__(
        self,
        settings: Settings,
        maker: async_sessionmaker[AsyncSession],
        generator: ImageGenerator,
        registry: AgentRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.maker = maker
        self.generator = generator
        self.registry = registry
        self.stopping = asyncio.Event()
        self.paused = False
        self.current_job: uuid.UUID | None = None

    # --- job state ------------------------------------------------------------------

    async def recover(self) -> None:
        """Handle jobs a previous worker left behind."""
        async with self.maker() as db, db.begin():
            await db.execute(
                update(ImageJob)
                .where(
                    ImageJob.status == "running",
                    ImageJob.attempts >= self.settings.IMAGE_MAX_ATTEMPTS,
                )
                .values(
                    status="failed",
                    error="worker stopped during generation",
                    finished_at=func.now(),
                    updated_at=func.now(),
                )
            )
            requeued = await db.execute(
                update(ImageJob)
                .where(ImageJob.status == "running")
                .values(status="queued", progress=0, started_at=None, updated_at=func.now())
            )
        log.info("recovered", requeued=requeued.rowcount)  # type: ignore[attr-defined]

    async def _claim(self) -> ImageJob | None:
        async with self.maker() as db, db.begin():
            next_id = (
                select(ImageJob.id)
                .where(ImageJob.status == "queued", ImageJob.cancel_requested.is_(False))
                .order_by(ImageJob.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            job: ImageJob | None = await db.scalar(
                update(ImageJob)
                .where(ImageJob.id == next_id)
                .values(
                    status="running",
                    attempts=ImageJob.attempts + 1,
                    progress=0,
                    started_at=func.now(),
                    updated_at=func.now(),
                )
                .returning(ImageJob)
            )
            return job

    async def _set(self, job_id: uuid.UUID, **values: Any) -> None:
        async with self.maker() as db, db.begin():
            await db.execute(
                update(ImageJob)
                .where(ImageJob.id == job_id)
                .values(updated_at=func.now(), **values)
            )

    async def _cancel_requested(self, job_id: uuid.UUID) -> bool:
        async with self.maker() as db:
            flag = await db.scalar(select(ImageJob.cancel_requested).where(ImageJob.id == job_id))
        return bool(flag)

    # --- processing -----------------------------------------------------------------

    async def run_once(self) -> bool:
        """Process the next queued job. Returns False if there was none."""
        job = await self._claim()
        if job is None:
            return False
        await self._process(job)
        return True

    async def _process(self, job: ImageJob) -> None:
        final = output_path(self.settings, job.user_id, job.id)
        tmp = final.with_name(f".{job.id}.{secrets.token_hex(4)}.png")
        await asyncio.to_thread(final.parent.mkdir, parents=True, exist_ok=True)
        req = GenerationRequest(
            prompt=job.prompt,
            negative_prompt=job.negative_prompt or "",
            seed=job.seed,
            steps=job.steps,
            width=job.width,
            height=job.height,
        )
        progress = {"now": 0, "saved": 0}
        started = time.monotonic()
        log.info("job_started", job_id=str(job.id), attempt=job.attempts)
        self.current_job = job.id
        run_id = await self._registry_call(
            "start_run",
            IMAGING.id,
            "image_job",
            attempt=job.attempts,
            details={
                "job_id": str(job.id),
                "steps": job.steps,
                "size": f"{job.width}x{job.height}",
            },
        )

        def on_progress(pct: int) -> None:
            progress["now"] = max(progress["now"], min(99, pct))

        task = asyncio.create_task(self.generator.generate(req, tmp, on_progress))
        outcome, error = "done", None
        try:
            async with asyncio.timeout(self.settings.IMAGE_JOB_TIMEOUT_SECONDS):
                while True:
                    done, _ = await asyncio.wait({task}, timeout=self.settings.IMAGE_POLL_SECONDS)
                    if done:
                        task.result()
                        break
                    if self.stopping.is_set():
                        raise Shutdown
                    if await self._cancel_requested(job.id):
                        outcome = "cancelled"
                        break
                    if progress["now"] != progress["saved"]:
                        progress["saved"] = progress["now"]
                        await self._set(job.id, progress=progress["now"])
        except TimeoutError:
            outcome = "failed"
            error = f"timed out after {self.settings.IMAGE_JOB_TIMEOUT_SECONDS:g}s"
        except Shutdown:
            outcome = "requeue"
        except GenerationFailed as exc:
            outcome, error = "failed", str(exc)[:2000]
        except Exception as exc:
            outcome, error = "failed", f"{type(exc).__name__}: {exc}"[:2000]
            log.exception("job_crashed", job_id=str(job.id))
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

        if outcome == "done":
            try:
                await self._store(job, tmp, final, time.monotonic() - started)
            except GenerationFailed as exc:
                outcome, error = "failed", str(exc)
        if outcome != "done":
            tmp.unlink(missing_ok=True)
        if outcome == "requeue":
            await self._set(job.id, status="queued", progress=0, started_at=None)
        elif outcome != "done":
            await self._set(job.id, status=outcome, error=error, finished_at=func.now())
        self.current_job = None
        log.info(
            "job_finished",
            job_id=str(job.id),
            outcome=outcome,
            seconds=round(time.monotonic() - started, 1),
        )
        if isinstance(run_id, int):
            status = RUN_STATUS[outcome]
            await self._registry_call(
                "finish_run",
                run_id,
                status,
                error=error or ("re-queued on shutdown" if outcome == "requeue" else None),
            )
            await self._registry_call(
                "log",
                IMAGING.id,
                "error" if outcome == "failed" else "info",
                f"image job {outcome}",
                run_id=run_id,
                job_id=str(job.id),
                error=error,
            )

    async def _registry_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        if self.registry is None:
            return None
        try:
            return await getattr(self.registry, method)(*args, **kwargs)
        except Exception as exc:  # the registry must never break image generation
            log.warning("registry_call_failed", method=method, error=type(exc).__name__)
            return None

    async def _store(self, job: ImageJob, tmp: Path, final: Path, seconds: float) -> None:
        if not await asyncio.to_thread(tmp.is_file):
            raise GenerationFailed("generator wrote no image")
        width, height = await asyncio.to_thread(png_dimensions, tmp)
        data = await asyncio.to_thread(tmp.read_bytes)
        await asyncio.to_thread(tmp.replace, final)  # atomic on the same filesystem
        async with self.maker() as db, db.begin():
            db.add(
                GeneratedAsset(
                    job_id=job.id,
                    user_id=job.user_id,
                    path=str(final.relative_to(self.settings.DATA_ROOT)),
                    width=width,
                    height=height,
                    size_bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    metadata_={
                        "prompt": job.prompt,
                        "negative_prompt": job.negative_prompt,
                        "seed": job.seed,
                        "steps": job.steps,
                        "model": job.model,
                        "seconds": round(seconds, 1),
                    },
                )
            )
            await db.execute(
                update(ImageJob)
                .where(ImageJob.id == job.id)
                .values(status="done", progress=100, finished_at=func.now(), updated_at=func.now())
            )

    # --- main loop ------------------------------------------------------------------

    async def serve(self, redis: Redis) -> None:
        while not self.stopping.is_set():
            while not self.stopping.is_set() and not self.paused and await self.run_once():
                pass
            # redis-py >= 8 raises its own TimeoutError when a blocking pop of >= 5 s finds
            # nothing; that is an idle wake-up, not a failure (it used to end this loop).
            with contextlib.suppress(asyncio.TimeoutError, RedisTimeoutError):
                await redis.blpop([self.settings.IMAGE_QUEUE_KEY], timeout=5)


async def _hold_lock(redis: Redis, token: str, stopping: asyncio.Event) -> None:
    while not stopping.is_set():
        await asyncio.sleep(LOCK_TTL / 3)
        ok = await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end",
            1,
            LOCK_KEY,
            token,
            LOCK_TTL,
        )
        if not ok:
            log.error("worker_lock_lost")
            stopping.set()


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    check_data_root(settings)  # refuse to run without the HDD
    generator = build_generator(settings)
    engine = build_engine(settings)
    redis = Redis.from_url(settings.REDIS_URL)
    token = secrets.token_hex(16)
    if not await redis.set(LOCK_KEY, token, nx=True, ex=LOCK_TTL):
        log.error("another_worker_running")
        await redis.aclose()
        await engine.dispose()
        return 1
    maker = build_sessionmaker(engine)
    worker = ImagingWorker(settings, maker, generator, AgentRegistry(maker))

    def state() -> tuple[str, dict[str, Any]]:
        busy = worker.current_job is not None
        return (
            "busy" if busy else "idle",
            {
                "current_job": str(worker.current_job) if busy else None,
                "generator": settings.IMAGE_GENERATOR,
            },
        )

    def on_paused(p: bool) -> None:
        worker.paused = p

    assert worker.registry is not None
    heartbeat = Heartbeat(
        worker.registry, IMAGING, settings.AGENT_HEARTBEAT_SECONDS, state, on_paused
    )
    heartbeat.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stopping.set)
    keeper = asyncio.create_task(_hold_lock(redis, token, worker.stopping))
    log.info("worker_started", generator=settings.IMAGE_GENERATOR)
    try:
        await worker.recover()
        serve = asyncio.create_task(worker.serve(redis))
        await worker.stopping.wait()
        # Give a running job a moment to notice `stopping` and requeue itself.
        await asyncio.wait({serve}, timeout=settings.IMAGE_POLL_SECONDS * 3)
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve
    finally:
        keeper.cancel()
        await heartbeat.stop()
        await redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
            "else return 0 end",
            1,
            LOCK_KEY,
            token,
        )
        await redis.aclose()
        await engine.dispose()
        log.info("worker_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
