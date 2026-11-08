"""devops.*: CI/CD and runtime monitoring for the DevOps dashboard (docs/32-devops-monitoring.md).

Reused, not duplicated: GitHub Actions runs/jobs stay in github.workflow_runs / workflow_jobs
and raw webhook deliveries in github.webhook_events. This schema adds only what had no home:

- repo_events        normalized GitHub activity (PR opened/closed/merged, pushes, GitHub
                     deployments and their statuses), one row per provider fact (dedup_key).
- deployments        server deploys (scripts/deploy.sh via app.workers.deploy_record) and
                     GitHub deployments, with phase timestamps and health verification.
- services           the current state of each monitored compose service (host collector).
- service_snapshots  every collector observation (history; purged after a retention period).
- events             the persisted live feed (build.*, pr.merged, deployment.*, service.*):
                     only real state transitions, one row per transition (dedup_key). The id is
                     the SSE / polling cursor.
- notification_prefs, notification_reads  per-user notification controls and read marks.
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

S = {"schema": "devops"}

DEPLOY_STATUSES = (
    "running",  # started, not finished
    "succeeded",  # finished AND health verified
    "unverified",  # the deploy command reported success but no health verification was recorded
    "failed",  # failed; rollback (if any) did not restore a healthy release
    "rolled_back",  # failed, then rolled back to the previous release, which is healthy
    "unknown",  # a GitHub deployment state this service does not map
)
SERVICE_STATUSES = ("healthy", "unhealthy", "stopped", "starting", "unknown")
EVENT_TYPES = (
    "build.started",
    "build.completed",
    "build.failed",
    "pr.merged",
    "deployment.started",
    "deployment.completed",
    "deployment.failed",
    "deployment.rolled_back",
    "service.unhealthy",
    "service.recovered",
)
SEVERITIES = ("info", "success", "warning", "error")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN (" + ",".join(f"'{v}'" for v in values) + ")"


class RepoEvent(Base):
    __tablename__ = "repo_events"
    __table_args__ = (
        Index("ix_devops_repo_events_repo_occurred", "repo", "occurred_at"),
        Index("ix_devops_repo_events_occurred", "occurred_at"),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # Stable provider identity, e.g. "github:pr:me/app#12:merged", so a webhook redelivery,
    # an out-of-order delivery and the backfill all land on the same row.
    dedup_key: Mapped[str] = mapped_column(String(300), unique=True)
    provider: Mapped[str] = mapped_column(String(16), server_default=text("'github'"))
    # pr.opened | pr.closed | pr.merged | pr.reopened | push | deployment | deployment_status
    kind: Mapped[str] = mapped_column(String(32))
    repo: Mapped[str] = mapped_column(String(201))
    branch: Mapped[str | None] = mapped_column(String(255))
    sha: Mapped[str | None] = mapped_column(String(64))
    number: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(100))
    url: Mapped[str | None] = mapped_column(Text)
    environment: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(32))
    # When it happened at the provider (not when we heard of it).
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # "webhook" | "backfill"
    source: Mapped[str] = mapped_column(String(16))
    delivery_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        CheckConstraint(_in("status", DEPLOY_STATUSES), name="status"),
        CheckConstraint("source IN ('server','github')", name="source"),
        Index("ix_devops_deployments_started", "started_at"),
        Index("ix_devops_deployments_repo_env_started", "repo", "environment", "started_at"),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # "server" (scripts/deploy.sh on this host) | "github" (a GitHub deployment)
    source: Mapped[str] = mapped_column(String(16))
    # server: "agent-run:<id>"; github: "github:<repo>:<deployment id>". Unique.
    external_id: Mapped[str] = mapped_column(String(300), unique=True)
    repo: Mapped[str | None] = mapped_column(String(201))
    # server deploys: "backend" | "frontend"
    target: Mapped[str | None] = mapped_column(String(32))
    environment: Mapped[str] = mapped_column(String(100), server_default=text("'production'"))
    branch: Mapped[str | None] = mapped_column(String(255))
    sha: Mapped[str | None] = mapped_column(String(64))
    previous_sha: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'running'"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    build_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    build_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deploy_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deploy_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the release passed its health verification (null = never verified).
    health_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # null (no rollback) | "healthy" | "unhealthy"
    rollback_status: Mapped[str | None] = mapped_column(String(16))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # The deployer agent run (agents.agent_runs) for server deploys.
    agent_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.agent_runs.id", ondelete="SET NULL")
    )
    url: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # --- live progress (docs/33-deploy-progress.md) ---
    # The commit the deploy script actually checked out and verified on the running release
    # (`sha` above is what the poller expected when it started the deploy).
    deployed_sha: Mapped[str | None] = mapped_column(String(64))
    # The PR merged by that commit, when it is reliably known (a recorded pr.merged event for
    # the commit, else GitHub's "Merge pull request #N" subject).
    pr_number: Mapped[int | None] = mapped_column(Integer)
    # The last progress report from the host (stale detection).
    progress_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # This deploy's own log lines (scrubbed, capped, newest kept).
    log_tail: Mapped[str | None] = mapped_column(Text)
    log_truncated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))


STAGE_STATUSES = ("pending", "running", "succeeded", "failed", "skipped")


class DeploymentStage(Base):
    """One stage of a deploy as the deploy script reported it (never inferred)."""

    __tablename__ = "deployment_stages"
    __table_args__ = (
        CheckConstraint(_in("status", STAGE_STATUSES), name="status"),
        Index("uq_devops_deployment_stages_deployment_name", "deployment_id", "name", unique=True),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("devops.deployments.id", ondelete="CASCADE")
    )
    # queued | pulling | validating | building | migrating | restarting | health_checking |
    # verifying | rolling_back
    name: Mapped[str] = mapped_column(String(32))
    seq: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # Sanitized, one line.
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint(_in("status", SERVICE_STATUSES), name="status"),
        Index("uq_devops_services_host_service", "host", "service", unique=True),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    host: Mapped[str] = mapped_column(String(100))
    service: Mapped[str] = mapped_column(String(100))
    container_id: Mapped[str | None] = mapped_column(String(80))
    container_name: Mapped[str | None] = mapped_column(String(200))
    # docker: running | exited | restarting | paused | created | dead | missing
    state: Mapped[str | None] = mapped_column(String(32))
    # docker health: healthy | unhealthy | starting | "" (no healthcheck)
    health: Mapped[str | None] = mapped_column(String(16))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    image: Mapped[str | None] = mapped_column(Text)
    image_id: Mapped[str | None] = mapped_column(String(100))
    # The git commit the image was built from (OCI revision label), when known.
    revision: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'unknown'"))
    status_since: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    failure_reason: Mapped[str | None] = mapped_column(Text)
    # The collector last reported this service (its heartbeat for this row).
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Last time a health verification passed (docker healthcheck healthy / readiness ok).
    last_healthy_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ServiceSnapshot(Base):
    __tablename__ = "service_snapshots"
    __table_args__ = (
        Index("ix_devops_service_snapshots_observed", "observed_at"),
        Index("ix_devops_service_snapshots_service_observed", "service_id", "observed_at"),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("devops.services.id", ondelete="CASCADE"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    state: Mapped[str | None] = mapped_column(String(32))
    health: Mapped[str | None] = mapped_column(String(16))
    revision: Mapped[str | None] = mapped_column(String(64))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class DevopsEvent(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(_in("type", EVENT_TYPES), name="type"),
        CheckConstraint(_in("severity", SEVERITIES), name="severity"),
        Index("ix_devops_events_occurred", "occurred_at"),
        Index("ix_devops_events_repo_id", "repo", "id"),
        S,
    )

    # The cursor: SSE `id:`, Last-Event-ID, ?after_id= for polling.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    # One row per state transition, e.g. "build.failed:me/app:run:123:attempt:1".
    dedup_key: Mapped[str] = mapped_column(String(300), unique=True)
    type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    repo: Mapped[str | None] = mapped_column(String(201))
    branch: Mapped[str | None] = mapped_column(String(255))
    sha: Mapped[str | None] = mapped_column(String(64))
    environment: Mapped[str | None] = mapped_column(String(100))
    service: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    # What the event refers to: {"workflow_run_id": 1} / {"deployment_id": 3} / ...
    ref: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NotificationPrefs(Base):
    __tablename__ = "notification_prefs"
    __table_args__ = (S,)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.users.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    failures_only: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Repository subscriptions ("owner/name"); empty = every monitored repository.
    repos: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # Event types to notify about; empty = every type.
    types: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # Everything with id <= this is read ("mark all read").
    read_up_to: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NotificationRead(Base):
    __tablename__ = "notification_reads"
    __table_args__ = (S,)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.users.id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[int] = mapped_column(
        ForeignKey("devops.events.id", ondelete="CASCADE"), primary_key=True
    )
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
