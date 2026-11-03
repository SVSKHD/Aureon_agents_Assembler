"""Actions workflows, runs, jobs, dispatch/rerun and webhook answers
(docs/20-github-actions-webhooks.md)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.github_ops import GatedOut, Strict

MAX_INPUTS = 25  # GitHub's limit for workflow_dispatch inputs
MAX_INPUT_CHARS = 1000


class WorkflowOut(BaseModel):
    id: int
    name: str
    path: str
    file: str
    state: str | None = None
    html_url: str | None = None
    # Listed in GITHUB_DISPATCH_ALLOWLIST (may be dispatched / re-run).
    dispatch_allowed: bool
    # Matches GITHUB_PROD_WORKFLOWS: dispatch / re-run goes through an approval.
    production: bool


class RunOut(BaseModel):
    id: int
    name: str | None = None
    workflow_id: int | None = None
    path: str | None = None
    event: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    status: str | None = None
    conclusion: str | None = None
    run_number: int | None = None
    run_attempt: int | None = None
    actor: str | None = None
    html_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class StepOut(BaseModel):
    number: int | None = None
    name: str | None = None
    status: str | None = None
    conclusion: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class JobOut(BaseModel):
    id: int
    run_id: int | None = None
    name: str | None = None
    status: str | None = None
    conclusion: str | None = None
    run_attempt: int | None = None
    runner_name: str | None = None
    labels: list[str] = []
    html_url: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    steps: list[StepOut] = []


InputValue = str | int | float | bool


class DispatchRequest(Strict):
    ref: str = Field(min_length=1, max_length=255)
    inputs: dict[str, InputValue] = Field(default_factory=dict)

    @field_validator("inputs")
    @classmethod
    def _inputs(cls, v: dict[str, InputValue]) -> dict[str, InputValue]:
        if len(v) > MAX_INPUTS:
            raise ValueError(f"at most {MAX_INPUTS} inputs")
        for k, val in v.items():
            if not k or len(k) > 100:
                raise ValueError("input names must be 1-100 characters")
            if isinstance(val, str) and len(val) > MAX_INPUT_CHARS:
                raise ValueError(f"input {k!r} is longer than {MAX_INPUT_CHARS} characters")
        return v


class ActionOut(BaseModel):
    """202 answer of dispatch / rerun / cancel: sent now (`sent`) or held for approval."""

    status: Literal["sent", "pending_approval"]
    action: str
    repo: str
    target: str
    approval: GatedOut | None = None


class WebhookAck(BaseModel):
    status: Literal["accepted", "duplicate", "ignored"]
    delivery_id: str


class MonitorItemOut(BaseModel):
    """One row of the combined Actions monitor: a host deploy (a `deployer` agent run) or a
    GitHub Actions CI run (github.workflow_runs), on one timeline."""

    kind: Literal["deploy", "ci"]
    id: str  # "deploy:<agent run id>" | "ci:<GitHub run id>"
    name: str
    repo: str | None = None
    branch: str | None = None
    sha: str | None = None
    # running | queued | success | failure | cancelled | unknown
    status: str
    # raw values: agent run status (deploy) / GitHub status + conclusion (ci)
    raw_status: str | None = None
    conclusion: str | None = None
    event: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    url: str | None = None
    error: str | None = None
    rolled_back: bool | None = None


class MonitorOut(BaseModel):
    items: list[MonitorItemOut]
    # Newest row time across both sources (the client polls; unchanged = nothing new).
    latest_at: datetime | None = None
