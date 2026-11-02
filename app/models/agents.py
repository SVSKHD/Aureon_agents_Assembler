"""agents.agents, agent_runs, agent_logs, scheduled_jobs."""

from __future__ import annotations

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
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

AGENT_STATES = ("idle", "busy", "error", "stopped")
RUN_STATUSES = ("running", "succeeded", "failed", "cancelled")
LOG_LEVELS = ("debug", "info", "warning", "error")


class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint("state IN ('idle','busy','error','stopped')", name="state"),
        {"schema": "agents"},
    )

    # Stable slug: "ollama", "imaging", "scheduler", later "coding", "trading-adapter".
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16), server_default=text("'idle'"))
    # Set by an admin; the agent stops taking new work while paused.
    paused: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    version: Mapped[str | None] = mapped_column(String(64))
    host: Mapped[str | None] = mapped_column(String(255))
    pid: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running','succeeded','failed','cancelled')", name="status"),
        Index("ix_agents_agent_runs_agent_id_started_at", "agent_id", "started_at"),
        {"schema": "agents"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agents.id", ondelete="CASCADE"))
    scheduled_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.scheduled_jobs.id", ondelete="SET NULL")
    )
    # What ran: "chat", "image_job", or a scheduled task name.
    task: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'running'"))
    attempt: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    # IDs only (job id, conversation id), never prompts: runs are visible to every user.
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class AgentLog(Base):
    __tablename__ = "agent_logs"
    __table_args__ = (
        CheckConstraint("level IN ('debug','info','warning','error')", name="level"),
        Index("ix_agents_agent_logs_agent_id_created_at", "agent_id", "created_at"),
        {"schema": "agents"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agents.id", ondelete="CASCADE"))
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.agent_runs.id", ondelete="SET NULL")
    )
    level: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ScheduledJob(Base):
    __tablename__ = "scheduled_jobs"
    __table_args__ = (
        Index("ix_agents_scheduled_jobs_next_run_at", "next_run_at"),
        {"schema": "agents"},
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.agents.id", ondelete="CASCADE"))
    # Name of a task registered in code (app/workers/scheduler.py TASKS); never a command.
    task: Mapped[str] = mapped_column(String(100))
    cron: Mapped[str] = mapped_column(String(100))
    paused: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    max_retries: Mapped[int] = mapped_column(SmallInteger, server_default=text("3"))
    retry_backoff_seconds: Mapped[int] = mapped_column(Integer, server_default=text("60"))
    timeout_seconds: Mapped[int] = mapped_column(Integer, server_default=text("300"))
    # Retry number of the current failure streak (0 = on schedule).
    attempt: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_error: Mapped[str | None] = mapped_column(Text)
    # Lease held while running, so a crashed scheduler doesn't block the job forever.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
