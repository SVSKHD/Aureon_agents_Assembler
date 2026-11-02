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
    UniqueConstraint,
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


# --- Phase 16: issues + PRs (docs/19-github-issues-prs.md) --------------------------------

OPERATION_STATUSES = (
    "pending_approval",
    "running",
    "succeeded",
    "failed",
    "denied",
    "expired",
    "invalidated",
)
_OP_STATUS_CHECK = "status IN (" + ",".join(f"'{s}'" for s in OPERATION_STATUSES) + ")"


class GitHubIssueLink(Base):
    """A GitHub issue linked to a Spasta task. `task_ref` is an opaque id the frontend
    supplies (tasks live in the workspace document, public.astra_documents)."""

    __tablename__ = "issue_links"
    __table_args__ = (
        UniqueConstraint("repo_full_name", "issue_number", "task_ref", name="uq_issue_task"),
        Index("ix_github_issue_links_task_ref", "task_ref"),
        {"schema": "github"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    repo_full_name: Mapped[str] = mapped_column(String(201))
    issue_number: Mapped[int] = mapped_column(Integer)
    issue_node_id: Mapped[str | None] = mapped_column(String(100))
    task_ref: Mapped[str] = mapped_column(Text)
    task_kind: Mapped[str] = mapped_column(Text, server_default=text("'spasta_task'"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GitHubTrackedOperation(Base):
    """Every write this service sends (or asks approval to send) to GitHub."""

    __tablename__ = "tracked_operations"
    __table_args__ = (
        CheckConstraint(_OP_STATUS_CHECK, name="status"),
        Index("ix_github_tracked_operations_created_at", "created_at"),
        Index("ix_github_tracked_operations_repo", "repo"),
        Index("ix_github_tracked_operations_approval", "approval_request_id"),
        {"schema": "github"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # e.g. "issue.create", "pr.merge"
    op: Mapped[str] = mapped_column(String(64))
    repo: Mapped[str] = mapped_column(String(201))
    # e.g. "issue#12", "pr#3", "branch:main"
    target: Mapped[str | None] = mapped_column(String(300))
    # The request body, token-redacted and size-capped.
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(20), server_default=text("'running'"))
    approval_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("audit.approval_requests.id", ondelete="SET NULL")
    )
    github_response_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
