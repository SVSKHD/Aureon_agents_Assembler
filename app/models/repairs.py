"""agents.incidents and agents.repair_tasks (docs/21-self-repair.md)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

INCIDENT_SOURCES = ("exception", "heartbeat", "docker", "actions", "db", "api")
INCIDENT_SEVERITIES = ("info", "warning", "error", "critical")
INCIDENT_STATUSES = ("open", "recovering", "repair_queued", "resolved", "ignored")
# An incident in one of these is "open": its fingerprint is unique among them.
INCIDENT_ACTIVE = ("open", "recovering", "repair_queued")
REPAIR_STATUSES = (
    "queued",
    "running",
    "patch_ready",
    "tests_passed",
    "tests_failed",
    "pr_opened",
    "blocked",
    "failed",
    "cancelled",
)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ",".join(f"'{v}'" for v in values) + ")"


class Incident(Base):
    """Something went wrong: an exception, a stale heartbeat, an unhealthy container, a failed
    Actions run, a slow/unreachable DB or a not-ready API. Repeats bump `count`/`last_seen`."""

    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint(_in("source", INCIDENT_SOURCES), name="source"),
        CheckConstraint(_in("severity", INCIDENT_SEVERITIES), name="severity"),
        CheckConstraint(_in("status", INCIDENT_STATUSES), name="status"),
        Index(
            "uq_agents_incidents_open_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text(_in("status", INCIDENT_ACTIVE)),
        ),
        Index("ix_agents_incidents_status_last_seen", "status", "last_seen"),
        {"schema": "agents"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    fingerprint: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(16))
    severity: Mapped[str] = mapped_column(String(10), server_default=text("'error'"))
    title: Mapped[str] = mapped_column(String(300))
    # Redacted. Exceptions: type, message, location {file,line,function}, frames.
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    count: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'open'"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Automatic recovery bookkeeping (max INCIDENT_MAX_RECOVERY_ATTEMPTS, then escalate).
    recovery_attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    last_recovery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RepairTask(Base):
    """One attempt to fix an incident with a local-model patch, tested in a sandboxed
    worktree and offered as a PR (never merged here)."""

    __tablename__ = "repair_tasks"
    __table_args__ = (
        CheckConstraint(_in("status", REPAIR_STATUSES), name="status"),
        Index("ix_agents_repair_tasks_status_created_at", "status", "created_at"),
        Index("ix_agents_repair_tasks_incident_id", "incident_id"),
        {"schema": "agents"},
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.incidents.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    repo: Mapped[str] = mapped_column(String(64))
    github_repo: Mapped[str | None] = mapped_column(String(201))
    # Files the model is shown and allowed to change (checked against the hard blocks).
    target_paths: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    branch: Mapped[str | None] = mapped_column(String(100))
    worktree: Mapped[str | None] = mapped_column(Text)
    base_sha: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(200))
    prompt_digest: Mapped[str | None] = mapped_column(String(64))
    patch: Mapped[str | None] = mapped_column(Text)
    files: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    test_output: Mapped[str | None] = mapped_column(Text)
    pr_url: Mapped[str | None] = mapped_column(String(500))
    pr_number: Mapped[int | None] = mapped_column(Integer)
    # Why it is blocked, or (while queued) why it is waiting, e.g. repairs disabled.
    blocked_reason: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    parent_repair_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agents.repair_tasks.id", ondelete="SET NULL")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
