"""Incidents and repair tasks (docs/21-self-repair.md)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    fingerprint: str
    source: str
    severity: str
    title: str
    status: str
    count: int
    first_seen: datetime
    last_seen: datetime
    resolved_at: datetime | None
    recovery_attempts: int
    last_recovery_at: datetime | None


class IncidentDetail(IncidentOut):
    details: dict[str, Any]
    repairs: list[RepairOut] = Field(default_factory=list)


class RepairOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    incident_id: uuid.UUID
    status: str
    repo: str
    github_repo: str | None
    target_paths: list[str]
    branch: str | None
    model: str | None
    prompt_digest: str | None
    files: list[str]
    pr_url: str | None
    pr_number: int | None
    blocked_reason: str | None
    error: str | None
    attempts: int
    parent_repair_id: uuid.UUID | None
    created_by: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RepairDetail(RepairOut):
    base_sha: str | None
    worktree: str | None
    patch: str | None
    test_output: str | None


class RepairCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: uuid.UUID
    # Override the incident's code location (repo-relative files the model may change).
    paths: list[str] | None = Field(default=None, max_length=10)


class RepairStatusOut(BaseModel):
    enabled: bool
    reason: str | None
    paused: bool
    model: str | None
    repo: str | None
    github_repo: str | None


IncidentDetail.model_rebuild()
