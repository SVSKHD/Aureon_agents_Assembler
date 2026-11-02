"""github.connections, github.repositories, github.sync_runs (docs/18-github-foundation.md).

No token is ever stored: a connection row is keyed by the token's sha256 prefix.
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

SYNC_STATUSES = ("running", "succeeded", "failed")


class GitHubConnection(Base):
    __tablename__ = "connections"
    __table_args__ = ({"schema": "github"},)

    # sha256(token)[:16]; the token itself never reaches the DB.
    fingerprint: Mapped[str] = mapped_column(String(16), primary_key=True)
    # "fine_grained" | "classic" | "other" (from the token prefix)
    token_kind: Mapped[str] = mapped_column(String(16))
    api_url: Mapped[str] = mapped_column(Text)
    login: Mapped[str | None] = mapped_column(String(100))
    github_user_id: Mapped[int | None] = mapped_column(BigInteger)
    # x-oauth-scopes (classic tokens only; fine-grained PATs report none).
    scopes: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    ok: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    detail: Mapped[str | None] = mapped_column(Text)
    rate_limit: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GitHubRepository(Base):
    __tablename__ = "repositories"
    __table_args__ = (
        Index("ix_github_repositories_full_name_lower", text("lower(full_name)")),
        {"schema": "github"},
    )

    # GitHub's numeric repository id (stable across renames).
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    full_name: Mapped[str] = mapped_column(String(201))
    owner: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    private: Mapped[bool] = mapped_column(Boolean)
    default_branch: Mapped[str | None] = mapped_column(String(255))
    archived: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    html_url: Mapped[str] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Set when a sync no longer sees the repo (deleted, access removed, off the allowlist);
    # cleared when it is seen again. Rows are never deleted.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GitHubSyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running','succeeded','failed')", name="status"),
        Index("ix_github_sync_runs_started_at", "started_at"),
        {"schema": "github"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # "scheduler" | "api"
    trigger: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'running'"))
    connection_fingerprint: Mapped[str | None] = mapped_column(String(16))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    repos_seen: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    repos_upserted: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    repos_removed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
