from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class ApprovalDetail(ApprovalOut):
    events: list[ApprovalEventOut] = []


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The admin's own password, re-verified server-side. Never stored or logged.
    password: str = Field(min_length=1, max_length=1024)


class DenyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)
