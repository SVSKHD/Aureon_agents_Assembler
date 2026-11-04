"""User-scoped image job + asset operations used by the API. Postgres is the source of
truth for job state; the Redis list only wakes the worker."""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.imaging import GeneratedAsset, ImageJob

ENQUEUE_LOCK = "SELECT pg_advisory_xact_lock(hashtext('aureon.image_enqueue'))"


class QueueFull(Exception):
    pass


def images_root(settings: Settings) -> Path:
    return settings.DATA_ROOT / "images"


def output_path(settings: Settings, user_id: uuid.UUID, job_id: uuid.UUID) -> Path:
    # Built only from UUIDs: no user-controlled text ever reaches the filesystem path.
    return images_root(settings) / str(user_id) / f"{job_id}.png"


def safe_asset_file(settings: Settings, relative: str) -> Path | None:
    """Resolve a stored asset path, refusing anything outside DATA_ROOT/images."""
    root = images_root(settings).resolve()
    candidate = (settings.DATA_ROOT / relative).resolve()
    if not candidate.is_relative_to(root) or candidate.suffix != ".png":
        return None
    return candidate if candidate.is_file() else None


class ImageService:
    def __init__(self, db: AsyncSession, user_id: uuid.UUID, settings: Settings) -> None:
        self.db = db
        self.user_id = user_id
        self.settings = settings

    async def enqueue(
        self,
        prompt: str,
        negative_prompt: str | None,
        seed: int | None,
        steps: int,
        width: int,
        height: int,
        *,
        model: str | None = None,
    ) -> ImageJob:
        # Serialize enqueues so two requests can't both slip under the cap.
        await self.db.execute(text(ENQUEUE_LOCK))
        queued = await self.db.scalar(
            select(func.count()).select_from(ImageJob).where(ImageJob.status == "queued")
        )
        if (queued or 0) >= self.settings.IMAGE_QUEUE_MAX:
            raise QueueFull
        job = ImageJob(
            user_id=self.user_id,
            prompt=prompt,
            negative_prompt=negative_prompt,
            seed=seed if seed is not None else secrets.randbelow(2**32),
            steps=steps,
            width=width,
            height=height,
            model=model or self.settings.IMAGE_MODEL_NAME,
        )
        self.db.add(job)
        await self.db.flush()
        await self.db.refresh(job)
        return job

    async def job(self, job_id: uuid.UUID) -> ImageJob | None:
        found: ImageJob | None = await self.db.scalar(
            select(ImageJob).where(ImageJob.id == job_id, ImageJob.user_id == self.user_id)
        )
        return found

    async def queue_position(self, job: ImageJob) -> int | None:
        if job.status != "queued":
            return None
        ahead = await self.db.scalar(
            select(func.count())
            .select_from(ImageJob)
            .where(ImageJob.status == "queued", ImageJob.created_at < job.created_at)
        )
        return int(ahead or 0) + 1

    async def asset_for_job(self, job_id: uuid.UUID) -> GeneratedAsset | None:
        found: GeneratedAsset | None = await self.db.scalar(
            select(GeneratedAsset).where(
                GeneratedAsset.job_id == job_id, GeneratedAsset.user_id == self.user_id
            )
        )
        return found

    async def cancel(self, job_id: uuid.UUID) -> ImageJob | None:
        """Queued → cancelled at once. Running → cancel_requested (the worker stops the
        process within IMAGE_POLL_SECONDS). Finished jobs are returned unchanged."""
        job = await self.job(job_id)
        if job is None:
            return None
        await self.db.execute(
            update(ImageJob)
            .where(ImageJob.id == job_id, ImageJob.status == "queued")
            .values(
                status="cancelled",
                cancel_requested=True,
                finished_at=func.now(),
                updated_at=func.now(),
            )
        )
        await self.db.execute(
            update(ImageJob)
            .where(ImageJob.id == job_id, ImageJob.status == "running")
            .values(cancel_requested=True, updated_at=func.now())
        )
        await self.db.refresh(job)
        return job

    async def gallery(
        self, limit: int, offset: int
    ) -> tuple[list[tuple[GeneratedAsset, ImageJob]], int]:
        cond = GeneratedAsset.user_id == self.user_id
        total = await self.db.scalar(select(func.count()).select_from(GeneratedAsset).where(cond))
        rows = await self.db.execute(
            select(GeneratedAsset, ImageJob)
            .join(
                ImageJob,
                and_(ImageJob.id == GeneratedAsset.job_id, ImageJob.user_id == self.user_id),
            )
            .where(cond)
            .order_by(GeneratedAsset.created_at.desc(), GeneratedAsset.id)
            .limit(limit)
            .offset(offset)
        )
        return [(a, j) for a, j in rows.all()], int(total or 0)

    async def asset(self, asset_id: uuid.UUID) -> GeneratedAsset | None:
        found: GeneratedAsset | None = await self.db.scalar(
            select(GeneratedAsset).where(
                GeneratedAsset.id == asset_id, GeneratedAsset.user_id == self.user_id
            )
        )
        return found

    async def delete_asset(self, asset_id: uuid.UUID) -> bool:
        asset = await self.asset(asset_id)
        if asset is None:
            return False
        path = safe_asset_file(self.settings, asset.path)
        await self.db.delete(asset)
        await self.db.flush()
        if path is not None:
            path.unlink(missing_ok=True)
        return True
