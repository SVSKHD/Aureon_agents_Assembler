from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApprovalEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event: str
    actor: uuid.UUID | None
    details: dict[str, Any]
    at: datetime


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    target: str
    payload: dict[str, Any]
    artifact_sha256: str | None
    status: str
    requested_by: uuid.UUID | None
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    expires_at: datetime
    executed_at: datetime | None
    result: dict[str, Any] | None
    created_at: datetime
    # Phase 20
    policy_subject: str | None = None
    required_approvals: int = 1
    received_approvals: int = 0
    risk_level: str = "medium"
    summary: str | None = None


class ApprovalArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    sha256: str
    size: int
    content: str | None
    content_ref: str | None
    truncated: bool
    captured_at: datetime


class PolicyOut(BaseModel):
    """A policy as applied to a request (snapshot) or as stored."""

    id: int | None
    kind_pattern: str
    required_approvals: int
    expiry_seconds: int | None
    allow_self_approval: bool
    require_reauth: bool
    reauth_max_age_seconds: int
    notify_channels: list[str]
    risk_level: str


class StoredPolicyOut(PolicyOut):
    model_config = ConfigDict(from_attributes=True)

    description: str | None
    enabled: bool
    updated_by: uuid.UUID | None
    updated_at: datetime


class DriftOut(BaseModel):
    # ok | drifted | unknown | not_checked | no_artifact
    status: str
    drifted: list[str] = []
    detail: str | None = None


class ChainOut(BaseModel):
    # ok | broken | legacy | empty
    status: str
    verified: int
    legacy: int
    broken_at: int | None = None
    reason: str | None = None


class ApprovalDetail(ApprovalOut):
    events: list[ApprovalEventOut] = []
    # Phase 20
    artifacts: list[ApprovalArtifactOut] = []
    policy: PolicyOut | None = None
    drift: DriftOut | None = None
    chain: ChainOut | None = None
    approvers: list[uuid.UUID] = []


class ApprovalEventsOut(BaseModel):
    approval_id: uuid.UUID
    events: list[ApprovalEventOut]
    chain: ChainOut


class ApprovalPage(BaseModel):
    items: list[ApprovalOut]
    total: int
    limit: int
    offset: int
    # Requests by status (for the `kind` filter, whatever `status` is).
    counts: dict[str, int]


class InboxItem(ApprovalOut):
    # What approving needs right now: "password", "session_stamp" (a valid stamp counts),
    # or "none" (the policy needs no re-auth).
    reauth: Literal["password", "session_stamp", "none"]


class InboxOut(BaseModel):
    items: list[InboxItem]
    total: int
    reauth_stamp_expires_at: datetime | None


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The admin's own password, re-verified server-side. Never stored or logged.
    password: str | None = Field(default=None, min_length=1, max_length=1024)
    # Phase 20: use this session's re-auth stamp (POST /api/v1/approvals/reauth) instead of
    # the password, where the request's policy accepts one. One of the two is required.
    use_reauth: bool = False

    @model_validator(mode="after")
    def _one_of(self) -> ApproveIn:
        if self.password is None and not self.use_reauth:
            raise ValueError("password is required (or use_reauth=true with a session stamp)")
        return self


class ReauthIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=1024)


class ReauthOut(BaseModel):
    stamped_at: datetime
    expires_at: datetime
    ttl_seconds: int


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind_pattern: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_.*?\-]+$")
    required_approvals: int = Field(default=1, ge=1, le=5)
    expiry_seconds: int | None = Field(default=None, ge=60, le=604800)
    allow_self_approval: bool = True
    require_reauth: bool = True
    reauth_max_age_seconds: int = Field(default=0, ge=0, le=3600)
    notify_channels: list[Literal["discord"]] = Field(default_factory=lambda: ["discord"])  # type: ignore[arg-type]
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    description: str | None = Field(default=None, max_length=300)
    enabled: bool = True


class PoliciesPutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Upserted by kind_pattern; policies not listed are left as they are (disable instead of
    # deleting). Changing approval rules needs the admin's password.
    password: str = Field(min_length=1, max_length=1024)
    policies: list[PolicyIn] = Field(min_length=1, max_length=100)


class ApprovalFiledOut(BaseModel):
    """202 answer of an endpoint that filed an approval request instead of acting."""

    approval_id: uuid.UUID
    status: str
    kind: str
    target: str
    artifact_sha256: str | None
    expires_at: datetime
    required_approvals: int
    received_approvals: int


class DenyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)
