"""GET /api/v1/audit/events: the audit trail for the frontend audit module
(docs/23-approval-engine.md). Admin only, read-only.

Filters: `event_type` (prefix match, e.g. `approval_`), `actor_user_id`, `since` / `until`
(ISO-8601, on occurred_at). Newest first. `details` are returned as stored: every writer
already redacts them (audit.record never receives secrets).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.auth.deps import AdminRequired, DbSession
from app.models.auth import AuditEvent, User
from app.schemas.common import Page

router = APIRouter(prefix="/v1/audit", tags=["audit"])


class AuditEventOut(BaseModel):
    id: int
    event_type: str
    actor_user_id: uuid.UUID | None
    actor_email: str | None
    target_user_id: uuid.UUID | None
    ip: str | None
    user_agent: str | None
    request_id: str | None
    details: dict[str, Any]
    created_at: datetime


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/events", response_model=Page[AuditEventOut])
async def list_audit_events(
    _admin: AdminRequired,
    db: DbSession,
    event_type: Annotated[str | None, Query(max_length=64)] = None,
    actor_user_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[AuditEventOut]:
    conds: list[Any] = []
    if event_type:
        conds.append(AuditEvent.event_type.like(_escape_like(event_type) + "%", escape="\\"))
    if actor_user_id is not None:
        conds.append(AuditEvent.actor_user_id == actor_user_id)
    if since is not None:
        conds.append(AuditEvent.occurred_at >= since)
    if until is not None:
        conds.append(AuditEvent.occurred_at < until)
    total = await db.scalar(select(func.count()).select_from(AuditEvent).where(*conds))
    rows = (
        await db.execute(
            select(AuditEvent, User.email)
            .outerjoin(User, User.id == AuditEvent.actor_user_id)
            .where(*conds)
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[
            AuditEventOut(
                id=e.id,
                event_type=e.event_type,
                actor_user_id=e.actor_user_id,
                actor_email=email,
                target_user_id=e.target_user_id,
                ip=e.ip,
                user_agent=e.user_agent,
                request_id=e.request_id,
                details=e.details or {},
                created_at=e.occurred_at,
            )
            for e, email in rows
        ],
        total=total or 0,
        limit=limit,
        offset=offset,
    )
