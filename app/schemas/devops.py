"""DevOps dashboard API contract (docs/32-devops-monitoring.md).

Four separate status vocabularies, never mixed: a merged PR is not a passing build, and a
passing build is not a deployment. Each has UNKNOWN for "cannot be verified". No progress
percentage is ever reported for a running build (GitHub does not provide one).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.devops import EVENT_TYPES

PRStatus = Literal["OPEN", "MERGED", "CLOSED", "UNKNOWN"]
CIStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"]
DeploymentStatus = Literal["RUNNING", "SUCCEEDED", "UNVERIFIED", "FAILED", "ROLLED_BACK", "UNKNOWN"]
RuntimeStatus = Literal["HEALTHY", "UNHEALTHY", "STOPPED", "STARTING", "UNKNOWN"]
HealthVerification = Literal["VERIFIED", "FAILED", "NOT_VERIFIED", "UNKNOWN"]
EventType = Literal[
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
]
Severity = Literal["info", "success", "warning", "error"]
RepoEventKind = Literal[
    "pr.opened", "pr.reopened", "pr.closed", "pr.merged", "push", "deployment", "deployment_status"
]

assert set(EventType.__args__) == set(EVENT_TYPES)  # type: ignore[attr-defined]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PullRequestOut(_Out):
    repo: str
    number: int
    title: str | None
    status: PRStatus
    branch: str | None = Field(description="Base branch")
    sha: str | None = Field(description="Merge commit for a merged PR, else the head commit")
    actor: str | None
    url: str | None
    occurred_at: datetime


class RepoEventOut(_Out):
    id: int
    kind: RepoEventKind
    repo: str
    branch: str | None
    sha: str | None
    number: int | None
    title: str | None
    actor: str | None
    url: str | None
    environment: str | None
    state: str | None
    occurred_at: datetime
    source: Literal["webhook", "backfill"]


class JobSummary(BaseModel):
    name: str | None
    status: CIStatus
    url: str | None


class BuildOut(BaseModel):
    """A GitHub Actions workflow run (github.workflow_runs)."""

    id: int
    repo: str
    name: str | None
    workflow_path: str | None
    event: str | None
    branch: str | None
    sha: str | None
    status: CIStatus
    conclusion: str | None = Field(description="GitHub's raw conclusion, when completed")
    run_attempt: int | None
    url: str | None
    created_at: datetime | None
    updated_at: datetime | None
    failed_jobs: list[JobSummary] = Field(default_factory=list)


class DeploymentOut(BaseModel):
    id: int
    source: Literal["server", "github"]
    repo: str | None
    target: str | None
    environment: str
    branch: str | None
    sha: str | None
    previous_sha: str | None
    status: DeploymentStatus
    health: HealthVerification = Field(
        description="VERIFIED only when the release passed its health check after deploying"
    )
    health_verified_at: datetime | None
    rolled_back: bool
    rollback_status: Literal["healthy", "unhealthy"] | None
    failure_reason: str | None
    started_at: datetime
    finished_at: datetime | None
    build_started_at: datetime | None
    build_finished_at: datetime | None
    deploy_started_at: datetime | None
    deploy_finished_at: datetime | None
    url: str | None


class ServiceOut(BaseModel):
    id: int
    host: str
    service: str
    status: RuntimeStatus = Field(
        description="UNKNOWN when the collector has not reported within the stale window"
    )
    stale: bool
    state: str | None = Field(description="Docker state as last observed")
    health: str | None = Field(description="Docker healthcheck as last observed")
    exit_code: int | None
    container_id: str | None
    container_name: str | None
    image: str | None
    revision: str | None = Field(description="Commit the image was built from, when labelled")
    started_at: datetime | None
    status_since: datetime
    last_seen_at: datetime
    last_healthy_at: datetime | None
    failure_reason: str | None


class EventOut(_Out):
    id: int
    type: EventType
    severity: Severity
    title: str
    repo: str | None
    branch: str | None
    sha: str | None
    environment: str | None
    service: str | None
    url: str | None
    ref: dict[str, Any]
    occurred_at: datetime
    created_at: datetime


class FeedOut(BaseModel):
    items: list[EventOut]
    last_id: int = Field(description="Pass as after_id (or Last-Event-ID) to continue")
    has_more: bool


class NotificationOut(EventOut):
    read: bool


class NotificationsOut(BaseModel):
    items: list[NotificationOut]
    unread_count: int
    next_before_id: int | None = Field(description="Pass as before_id for the next (older) page")


class MarkReadIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_ids: list[int] = Field(default_factory=list, max_length=500)
    up_to_id: int | None = Field(default=None, ge=0, description="Mark everything up to this id")


class UnreadOut(BaseModel):
    unread_count: int


class PreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    failures_only: bool = False
    repos: list[str] = Field(default_factory=list, max_length=100)
    types: list[EventType] = Field(default_factory=list, max_length=len(EVENT_TYPES))

    @field_validator("repos")
    @classmethod
    def _repos(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for r in v:
            r = r.strip()
            owner, sep, name = r.partition("/")
            if not (sep and owner and name and len(r) <= 201 and "/" not in name):
                raise ValueError("repos must be owner/name")
            if r.lower() not in (x.lower() for x in out):
                out.append(r)
        return out


class PreferencesOut(PreferencesIn):
    model_config = ConfigDict(extra="ignore")


class GitHubStatusOut(BaseModel):
    configured: bool
    webhook_configured: bool
    last_backfill_at: datetime | None
    last_backfill_ok: bool | None


class CollectorStatusOut(BaseModel):
    last_seen_at: datetime | None
    stale: bool


class RepositoryStatusOut(BaseModel):
    repo: str
    default_branch: str | None
    pr: PullRequestOut | None = Field(description="Most recent merged PR")
    ci: BuildOut | None = Field(description="Latest build on the default branch")
    deployment: DeploymentOut | None = Field(description="Latest deployment of this repo")
    deployed: DeploymentOut | None = Field(
        description="Latest deployment that SUCCEEDED (health verified): what is running"
    )


class OverviewOut(BaseModel):
    generated_at: datetime
    github: GitHubStatusOut
    collector: CollectorStatusOut
    recently_merged: list[PullRequestOut]
    building: list[BuildOut]
    recent_failures: list[BuildOut]
    deployments: list[DeploymentOut] = Field(
        description="Latest deployment per (repo/target, environment)"
    )
    services: list[ServiceOut]
    services_by_status: dict[str, int]
    unread_notifications: int
