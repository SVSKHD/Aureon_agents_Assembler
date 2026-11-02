"""Claude review schemas (docs/22-claude-review.md)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Verdict = Literal["APPROVED", "CHANGES_REQUESTED", "BLOCKED", "REVIEW_UNAVAILABLE"]
CLIVerdict = Literal["APPROVED", "CHANGES_REQUESTED", "BLOCKED"]
Severity = Literal["info", "minor", "major", "critical"]


class Finding(BaseModel):
    """One finding from the CLI. Unknown keys are refused: the output must be exact."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    file: str = Field(min_length=1, max_length=1000)
    line: int | None = Field(default=None, ge=1, le=10_000_000)
    severity: Severity
    message: str = Field(min_length=1, max_length=4000)


class ReviewOutput(BaseModel):
    """The strict JSON the review prompt asks for."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    verdict: CLIVerdict
    summary: str = Field(max_length=20_000)
    findings: list[Finding] = Field(default_factory=list, max_length=200)


class ReviewRequest(BaseModel):
    # Re-run even when this head was already reviewed (the new row is not deduplicated).
    force: bool = False


class ReviewQueued(BaseModel):
    id: uuid.UUID
    status: str
    head_sha: str
    deduplicated: bool


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repo: str
    pr_number: int
    head_sha: str
    base_sha: str | None
    status: str
    verdict: Verdict | None
    reason: str | None
    summary: str | None
    findings: list[dict[str, Any]]
    classification: dict[str, Any]
    cli_version: str | None
    duration_ms: int | None
    forced: bool
    published: bool
    published_review_id: int | None
    published_at: datetime | None
    requested_by: uuid.UUID | None
    created_at: datetime
    finished_at: datetime | None
    # Always true: a review is advisory and never counts as an approval.
    advisory: bool = True


class PublishOut(BaseModel):
    review: ReviewOut
    github_review_id: int | None
    event: Literal["COMMENT"]
    inline_comments: int
    body_findings: int
