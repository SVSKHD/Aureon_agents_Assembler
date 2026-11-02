from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator


def _no_control(v: str | None) -> str | None:
    if v is not None and any(ord(c) < 32 and c not in "\n\t" for c in v):
        raise ValueError("control characters are not allowed")
    return v


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    negative_prompt: str | None = Field(default=None, max_length=1000)
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)
    steps: int | None = Field(default=None, ge=1)
    size: str = Field(default="512x512", pattern=r"^\d{2,4}x\d{2,4}$")

    _clean = field_validator("prompt", "negative_prompt")(_no_control)


class JobOut(BaseModel):
    id: uuid.UUID
    status: str
    progress: int
    cancel_requested: bool
    queue_position: int | None
    prompt: str
    negative_prompt: str | None
    seed: int
    steps: int
    width: int
    height: int
    model: str
    error: str | None
    attempts: int
    asset_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AssetOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    url: str
    width: int
    height: int
    size_bytes: int
    sha256: str
    prompt: str
    seed: int
    steps: int
    model: str
    created_at: datetime


class AssetPage(BaseModel):
    items: list[AssetOut]
    total: int
    limit: int
    offset: int
