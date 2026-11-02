"""agents.reviews: Claude PR reviews (docs/22-claude-review.md).

One row per requested review. `(repo, pr_number, head_sha)` is unique among rows that were
not forced and did not end REVIEW_UNAVAILABLE, so the same head is reviewed once unless an
admin forces a re-run, and an unavailable review can simply be requested again.
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
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

REVIEW_VERDICTS = ("APPROVED", "CHANGES_REQUESTED", "BLOCKED", "REVIEW_UNAVAILABLE")
REVIEW_STATUSES = ("queued", "running", "done")
_VERDICT_CHECK = (
    "verdict IS NULL OR verdict IN (" + ",".join(f"'{v}'" for v in REVIEW_VERDICTS) + ")"
)
_STATUS_CHECK = "status IN (" + ",".join(f"'{s}'" for s in REVIEW_STATUSES) + ")"
DEDUPE_WHERE = "NOT forced AND (verdict IS NULL OR verdict <> 'REVIEW_UNAVAILABLE')"


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint(_VERDICT_CHECK, name="verdict"),
        CheckConstraint(_STATUS_CHECK, name="status"),
        Index(
            "uq_agents_reviews_repo_pr_head",
            "repo",
            "pr_number",
            "head_sha",
            unique=True,
            postgresql_where=text(DEDUPE_WHERE),
        ),
        Index("ix_agents_reviews_repo_pr", "repo", "pr_number", "created_at"),
        {"schema": "agents"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    # lower-case owner/repo
    repo: Mapped[str] = mapped_column(String(200))
    pr_number: Mapped[int] = mapped_column(Integer)
    head_sha: Mapped[str] = mapped_column(String(40))
    base_sha: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    # NULL until the run finishes.
    verdict: Mapped[str | None] = mapped_column(String(20))
    # Short machine reason for BLOCKED / REVIEW_UNAVAILABLE (too_large, disabled, timeout...).
    reason: Mapped[str | None] = mapped_column(String(64))
    summary: Mapped[str | None] = mapped_column(Text)
    findings: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # The filter outcome (ALLOW | REDACT | BLOCKED), its reasons and redaction counts.
    classification: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    cli_version: Mapped[str | None] = mapped_column(String(100))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    forced: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    published: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    published_review_id: Mapped[int | None] = mapped_column(BigInteger)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
