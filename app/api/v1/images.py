"""Image generation endpoints. The API only enqueues and reads; the worker generates."""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import FileResponse

from app.api.ui import ui_module
from app.auth.deps import CsrfProtected, Current, DbSession, SettingsDep
from app.core.logging import get_logger
from app.models.imaging import GeneratedAsset, ImageJob
from app.schemas.images import AssetOut, AssetPage, GenerateRequest, JobOut
from app.services.health import check_data_root
from app.services.imaging import ImageService, QueueFull, safe_asset_file

UI_MODULE = ui_module("images", label="Images", group="ai", icon="image", order=42)
router = APIRouter(prefix="/images", tags=["images"])
log = get_logger(__name__)


async def _job_out(svc: ImageService, job: ImageJob) -> JobOut:
    asset = await svc.asset_for_job(job.id) if job.status == "done" else None
    return JobOut(
        id=job.id,
        status=job.status,
        progress=job.progress,
        cancel_requested=job.cancel_requested,
        queue_position=await svc.queue_position(job),
        prompt=job.prompt,
        negative_prompt=job.negative_prompt,
        seed=job.seed,
        steps=job.steps,
        width=job.width,
        height=job.height,
        model=job.model,
        error=job.error,
        attempts=job.attempts,
        asset_id=asset.id if asset else None,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _asset_out(a: GeneratedAsset, j: ImageJob) -> AssetOut:
    return AssetOut(
        id=a.id,
        job_id=a.job_id,
        url=f"/api/images/{a.id}/file",
        width=a.width,
        height=a.height,
        size_bytes=a.size_bytes,
        sha256=a.sha256,
        prompt=j.prompt,
        seed=j.seed,
        steps=j.steps,
        model=j.model,
        created_at=a.created_at,
    )


@router.post("/generate", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
async def generate(
    body: GenerateRequest,
    request: Request,
    current: CsrfProtected,
    db: DbSession,
    settings: SettingsDep,
) -> JobOut:
    if body.size not in settings.IMAGE_ALLOWED_SIZES:
        raise HTTPException(422, f"size must be one of {settings.IMAGE_ALLOWED_SIZES}")
    steps = body.steps or settings.IMAGE_DEFAULT_STEPS
    if steps > settings.IMAGE_MAX_STEPS:
        raise HTTPException(422, f"steps may not exceed {settings.IMAGE_MAX_STEPS}")
    try:
        await asyncio.to_thread(check_data_root, settings)
    except RuntimeError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "image storage is not mounted"
        ) from exc
    width, height = (int(n) for n in body.size.split("x"))

    svc = ImageService(db, current.user.id, settings)
    try:
        job = await svc.enqueue(body.prompt, body.negative_prompt, body.seed, steps, width, height)
    except QueueFull as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "image queue is full",
            headers={"Retry-After": str(settings.IMAGE_RETRY_AFTER)},
        ) from exc
    await db.commit()
    try:
        await request.app.state.redis.rpush(settings.IMAGE_QUEUE_KEY, str(job.id))
    except Exception:  # the worker also polls Postgres, so a lost wake-up only adds delay
        log.warning("image_wake_failed", job_id=str(job.id))
    return await _job_out(svc, job)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(
    job_id: uuid.UUID, db: DbSession, current: Current, settings: SettingsDep
) -> JobOut:
    svc = ImageService(db, current.user.id, settings)
    job = await svc.job(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return await _job_out(svc, job)


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(
    job_id: uuid.UUID, db: DbSession, current: CsrfProtected, settings: SettingsDep
) -> JobOut:
    svc = ImageService(db, current.user.id, settings)
    job = await svc.cancel(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    await db.commit()
    return await _job_out(svc, job)


@router.get("", response_model=AssetPage)
async def gallery(
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AssetPage:
    rows, total = await ImageService(db, current.user.id, settings).gallery(limit, offset)
    return AssetPage(
        items=[_asset_out(a, j) for a, j in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{asset_id}/file", response_class=FileResponse)
async def asset_file(
    asset_id: uuid.UUID, db: DbSession, current: Current, settings: SettingsDep
) -> FileResponse:
    asset = await ImageService(db, current.user.id, settings).asset(asset_id)
    path = safe_asset_file(settings, asset.path) if asset else None
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "image not found")
    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{asset_id}.png"',
        },
    )


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_image(
    asset_id: uuid.UUID, db: DbSession, current: CsrfProtected, settings: SettingsDep
) -> None:
    if not await ImageService(db, current.user.id, settings).delete_asset(asset_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "image not found")
    await db.commit()
