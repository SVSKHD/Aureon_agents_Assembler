"""The approval engine's tables (docs/19-github-issues-prs.md, docs/23-approval-engine.md):

- audit.approval_requests: one gated operation (Phase 16, extended additively in Phase 20 with
  the policy snapshot, approval counts, risk level, summary and requester IP)
- audit.approval_events: every state change, hash-chained per request (Phase 20)
- audit.approval_policies: per-kind policy rows; the most specific `kind_pattern` wins
- audit.approval_artifacts: what the approver is shown, one row per named artifact
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
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
_STATUS_CHECK = "status IN (" + ",".join(f"'{s}'" for s in APPROVAL_STATUSES) + ")"


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(_STATUS_CHECK, name="status"),
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
    # --- Phase 20 (additive) ---
    # The policy that applied when the request was filed, and a snapshot of it: later policy
    # edits never change a pending request's rules.
    policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit.approval_policies.id", ondelete="SET NULL")
    )
    policy: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # What the policy was resolved for: the kind, or e.g. "repair.pr_merge" for a repair PR.
    policy_subject: Mapped[str | None] = mapped_column(String(64))
    required_approvals: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    received_approvals: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    risk_level: Mapped[str] = mapped_column(String(16), server_default=text("'medium'"))
    summary: Mapped[str | None] = mapped_column(String(500))
    requester_ip: Mapped[str | None] = mapped_column(String(64))
    # Hash of the newest event: anchors the chain so a deleted tail is detected too.
    event_head_hash: Mapped[str | None] = mapped_column(String(64))


RISK_LEVELS = ("low", "medium", "high", "critical")
_RISK_CHECK = "risk_level IN (" + ",".join(f"'{s}'" for s in RISK_LEVELS) + ")"


class ApprovalPolicy(Base):
    __tablename__ = "approval_policies"
    __table_args__ = (
        UniqueConstraint("kind_pattern", name="uq_approval_policies_kind_pattern"),
        CheckConstraint("required_approvals BETWEEN 1 AND 5", name="required_approvals"),
        CheckConstraint(
            "expiry_seconds IS NULL OR expiry_seconds BETWEEN 60 AND 604800", name="expiry"
        ),
        CheckConstraint("reauth_max_age_seconds BETWEEN 0 AND 3600", name="reauth_max_age"),
        CheckConstraint(_RISK_CHECK, name="risk_level"),
        {"schema": "audit"},
    )

    id: Mapped[int] = mapped_column(Integer, Identity(always=True), primary_key=True)
    # fnmatch-style: "*", "github.*", "github.pr.merge", "repair.*", "trading.paper_intent".
    kind_pattern: Mapped[str] = mapped_column(String(64))
    required_approvals: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    # NULL = APPROVALS_TTL_MINUTES.
    expiry_seconds: Mapped[int | None] = mapped_column(Integer)
    # true = the D-16-3 rule (only the sole active admin, and only with
    # APPROVALS_ALLOW_SELF_APPROVAL); false = the requester can never approve.
    allow_self_approval: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    require_reauth: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # A session re-auth stamp (POST /api/v1/approvals/reauth) younger than this counts as the
    # password. 0 = the password is needed on every approve.
    reauth_max_age_seconds: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    notify_channels: Mapped[list[str]] = mapped_column(
        JSONB, server_default=text("'[\"discord\"]'::jsonb")
    )
    risk_level: Mapped[str] = mapped_column(String(16), server_default=text("'medium'"))
    description: Mapped[str | None] = mapped_column(String(300))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # No FK: core.users is truncated in tests and policies are configuration, not user data.
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApprovalArtifact(Base):
    __tablename__ = "approval_artifacts"
    __table_args__ = (
        UniqueConstraint("request_id", "name", name="uq_approval_artifacts_request_name"),
        {"schema": "audit"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("audit.approval_requests.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(64))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    # The canonical text, capped (APPROVALS_ARTIFACT_MAX_CHARS); `truncated` when cut. Binary
    # artifacts store no content, only `content_ref` (a pointer, e.g. "sha256:<hex>").
    content: Mapped[str | None] = mapped_column(Text)
    content_ref: Mapped[str | None] = mapped_column(String(300))
    truncated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


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
    # Phase 20 hash chain (per request): hash = sha256(prev_hash + canonical(this event)).
    # NULL on events written before the chain existed.
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    hash: Mapped[str | None] = mapped_column(String(64))
