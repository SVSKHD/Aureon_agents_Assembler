"""agents.image_jobs and agents.generated_assets."""

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

JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")
ACTIVE_STATUSES = ("queued", "running")


class ImageJob(Base):
    __tablename__ = "image_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','done','failed','cancelled')", name="status"
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="progress"),
        Index("ix_agents_image_jobs_status_created_at", "status", "created_at"),
        Index("ix_agents_image_jobs_user_id_created_at", "user_id", "created_at"),
        {"schema": "agents"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    progress: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    prompt: Mapped[str] = mapped_column(Text)
    negative_prompt: Mapped[str | None] = mapped_column(Text)
    seed: Mapped[int] = mapped_column(BigInteger)
    steps: Mapped[int] = mapped_column(SmallInteger)
    width: Mapped[int] = mapped_column(SmallInteger)
    height: Mapped[int] = mapped_column(SmallInteger)
    model: Mapped[str] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GeneratedAsset(Base):
    __tablename__ = "generated_assets"
    __table_args__ = (
        Index("ix_agents_generated_assets_user_id_created_at", "user_id", "created_at"),
        {"schema": "agents"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.image_jobs.id", ondelete="CASCADE"), unique=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id", ondelete="CASCADE"))
    # Relative to DATA_ROOT, always images/<user_id>/<job_id>.png (built from UUIDs only).
    path: Mapped[str] = mapped_column(Text)
    width: Mapped[int] = mapped_column(SmallInteger)
    height: Mapped[int] = mapped_column(SmallInteger)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
