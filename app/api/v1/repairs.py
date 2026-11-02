"""Incidents and self-repair (docs/21-self-repair.md). Admin only; POSTs need CSRF; every
write is audited.

    GET  /api/v1/repairs/status                     enabled?, why not, paused?
    GET  /api/v1/repairs/incidents                  ?status&source&limit&offset
    GET  /api/v1/repairs/incidents/{id}             with its repairs
    POST /api/v1/repairs/incidents/{id}/resolve | /ignore
    GET  /api/v1/repairs                            ?status&incident_id&limit&offset
    GET  /api/v1/repairs/{id}                       with the patch and the test output
    POST /api/v1/repairs                            {incident_id, paths?}: a manual repair
    POST /api/v1/repairs/{id}/cancel | /retry

A repair never merges anything: it ends at `pr_opened`. Merging the PR is
POST /api/v1/github/repositories/{owner}/{repo}/pulls/{n}/merge, which files an
ApprovalGate request of kind `github.pr.merge` (Phase 16).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select

from app.auth.deps import AdminRequired, DbSession, SettingsDep
from app.models.repairs import Incident, RepairTask
from app.schemas.common import Page
from app.schemas.repairs import (
    IncidentDetail,
    IncidentOut,
    RepairCreate,
    RepairDetail,
    RepairOut,
    RepairStatusOut,
)
from app.services import audit, incidents, repairs
from app.services.audit import ClientInfo

router = APIRouter(prefix="/v1/repairs", tags=["repairs"])

IncidentStatus = Literal["open", "recovering", "repair_queued", "resolved", "ignored"]
Source = Literal["exception", "heartbeat", "docker", "actions", "db", "api"]
RepairStatus = Literal[
    "queued",
    "running",
    "patch_ready",
    "tests_passed",
    "tests_failed",
    "pr_opened",
    "blocked",
    "failed",
    "cancelled",
]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]


def _refused(exc: repairs.RepairRefused) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "detail": exc.message})


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "detail": what})


@router.get("/status", response_model=RepairStatusOut)
async def repair_status(
    _admin: AdminRequired, request: Request, settings: SettingsDep
) -> RepairStatusOut:
    reason = repairs.disabled_reason(settings)
    return RepairStatusOut(
        enabled=reason is None,
        reason=reason,
        paused=await repairs.is_paused(request.app.state.sessionmaker),
        model=(settings.REPAIR_MODEL or "").strip() or None,
        repo=settings.REPAIR_REPO,
        github_repo=settings.REPAIR_GITHUB_REPO,
    )


# --- incidents ------------------------------------------------------------------------------


@router.get("/incidents", response_model=Page[IncidentOut])
async def list_incidents(
    _admin: AdminRequired,
    db: DbSession,
    status: IncidentStatus | None = None,
    source: Source | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[IncidentOut]:
    rows, total = await incidents.list_incidents(
        db, incidents.IncidentFilter(status, source), limit, offset
    )
    return Page(
        items=[IncidentOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


async def _detail(db: DbSession, inc: Incident) -> IncidentDetail:
    tasks = (
        await db.scalars(
            select(RepairTask)
            .where(RepairTask.incident_id == inc.id)
            .order_by(RepairTask.created_at.desc())
        )
    ).all()
    out = IncidentDetail.model_validate(inc)
    out.repairs = [RepairOut.model_validate(t) for t in tasks]
    return out


@router.get("/incidents/{incident_id}", response_model=IncidentDetail)
async def get_incident(
    _admin: AdminRequired, db: DbSession, incident_id: uuid.UUID
) -> IncidentDetail:
    inc = await db.get(Incident, incident_id)
    if inc is None:
        raise _not_found("incident not found")
    return await _detail(db, inc)


async def _close(
    admin: AdminRequired, request: Request, db: DbSession, incident_id: uuid.UUID, to: str
) -> IncidentDetail:
    try:
        inc = await incidents.close(db, incident_id, to, str(admin.user.id))
    except LookupError:
        raise _not_found("incident not found") from None
    except incidents.IncidentStateError as exc:
        raise HTTPException(409, {"code": "incident_closed", "detail": str(exc)}) from None
    audit.record(
        db,
        f"incident_{to}",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"incident_id": str(incident_id)},
    )
    await db.commit()
    return await _detail(db, inc)


@router.post("/incidents/{incident_id}/resolve", response_model=IncidentDetail)
async def resolve_incident(
    admin: AdminRequired, request: Request, db: DbSession, incident_id: uuid.UUID
) -> IncidentDetail:
    return await _close(admin, request, db, incident_id, "resolved")


@router.post("/incidents/{incident_id}/ignore", response_model=IncidentDetail)
async def ignore_incident(
    admin: AdminRequired, request: Request, db: DbSession, incident_id: uuid.UUID
) -> IncidentDetail:
    return await _close(admin, request, db, incident_id, "ignored")


# --- repairs --------------------------------------------------------------------------------


@router.get("", response_model=Page[RepairOut])
async def list_repairs(
    _admin: AdminRequired,
    db: DbSession,
    status: RepairStatus | None = None,
    incident_id: uuid.UUID | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[RepairOut]:
    q = select(RepairTask)
    c = select(func.count()).select_from(RepairTask)
    if status:
        q, c = q.where(RepairTask.status == status), c.where(RepairTask.status == status)
    if incident_id:
        q = q.where(RepairTask.incident_id == incident_id)
        c = c.where(RepairTask.incident_id == incident_id)
    total = int(await db.scalar(c) or 0)
    rows = (
        await db.scalars(q.order_by(RepairTask.created_at.desc()).limit(limit).offset(offset))
    ).all()
    return Page(
        items=[RepairOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{repair_id}", response_model=RepairDetail)
async def get_repair(_admin: AdminRequired, db: DbSession, repair_id: uuid.UUID) -> RepairDetail:
    task = await db.get(RepairTask, repair_id)
    if task is None:
        raise _not_found("repair not found")
    return RepairDetail.model_validate(task)


@router.post("", response_model=RepairDetail, status_code=status.HTTP_201_CREATED)
async def create_repair(
    admin: AdminRequired,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    body: RepairCreate,
) -> RepairDetail:
    inc = await db.get(Incident, body.incident_id, with_for_update=True)
    if inc is None:
        raise _not_found("incident not found")
    try:
        task = await repairs.create_repair(
            db, settings, inc, created_by=admin.user.id, paths=body.paths,
            client=ClientInfo.from_request(request),
        )  # fmt: skip
    except repairs.RepairRefused as exc:
        raise _refused(exc) from None
    await db.commit()
    return RepairDetail.model_validate(task)


@router.post("/{repair_id}/cancel", response_model=RepairDetail)
async def cancel_repair(
    admin: AdminRequired,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    repair_id: uuid.UUID,
) -> RepairDetail:
    task = await db.get(RepairTask, repair_id, with_for_update=True)
    if task is None:
        raise _not_found("repair not found")
    try:
        await repairs.cancel(db, repairs.RepairConfig.from_settings(settings), task)
    except repairs.RepairRefused as exc:
        raise _refused(exc) from None
    audit.record(
        db,
        "repair_cancelled",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"repair_id": str(repair_id)},
    )
    await db.commit()
    return RepairDetail.model_validate(task)


@router.post("/{repair_id}/retry", response_model=RepairDetail, status_code=201)
async def retry_repair(
    admin: AdminRequired,
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    repair_id: uuid.UUID,
) -> RepairDetail:
    task = await db.get(RepairTask, repair_id)
    if task is None:
        raise _not_found("repair not found")
    try:
        new = await repairs.retry(
            db, settings, task, admin.user.id, ClientInfo.from_request(request)
        )
    except repairs.RepairRefused as exc:
        raise _refused(exc) from None
    await db.commit()
    return RepairDetail.model_validate(new)
