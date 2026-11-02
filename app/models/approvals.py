"""audit.approval_requests and audit.approval_events: the minimal approval gate
(docs/19-github-issues-prs.md). Phase 20 adds policies and artifacts tables beside these.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

APPROVAL_STATUSES = (
    "pending",
    "approved",
    "denied",
    "expired",
    "invalidated",
    "executed",
    "failed",
)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','denied','expired','invalidated','executed',"
            "'failed')",
            name="status",
        ),
        Index("ix_audit_approval_requests_status", "status", "created_at"),
        {"schema": "audit"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    # e.g. "github.pr.merge"; maps to an executor in the registry.
    kind: Mapped[str] = mapped_column(String(64))
    # Human-readable target, e.g. "owner/repo#12".
    target: Mapped[str] = mapped_column(String(300))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # sha256 of what the approver saw; re-computed before execution (drift -> invalidated).
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalEvent(Base):
    __tablename__ = "approval_events"
    __table_args__ = (
        Index("ix_audit_approval_events_request", "request_id", "at"),
        {"schema": "audit"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("audit.approval_requests.id", ondelete="CASCADE")
    )
    # requested | approved | denied | expired | invalidated | executed | failed |
    # reauth_failed | self_approval_refused
    event: Mapped[str] = mapped_column(String(32))
    actor: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
