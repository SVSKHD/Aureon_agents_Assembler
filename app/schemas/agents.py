from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.schemas.common import Page

__all__ = ["Page"]


class AgentOut(BaseModel):
    id: str
    name: str
    kind: str
    state: str  # effective: paused > stale > reported (idle/busy/error/stopped)
    reported_state: str
    paused: bool
    version: str | None
    host: str | None
    last_heartbeat_at: datetime | None
    seconds_since_heartbeat: float | None
    last_activity_at: datetime | None
    registered_at: datetime
    details: dict[str, Any]


class AgentList(BaseModel):
    agents: list[AgentOut]


class RunOut(BaseModel):
    id: int
    agent_id: str
    task: str
    status: str
    attempt: int
    scheduled_job_id: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    error: str | None
    details: dict[str, Any]


class LogOut(BaseModel):
    id: int
    agent_id: str
    run_id: int | None
    level: str
    message: str
    context: dict[str, Any]
    created_at: datetime


class ScheduledJobOut(BaseModel):
    id: str
    agent_id: str
    task: str
    cron: str
    paused: bool
    max_retries: int
    retry_backoff_seconds: int
    timeout_seconds: int
    attempt: int
    consecutive_failures: int
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    last_error: str | None
    running: bool


class AgentDetail(AgentOut):
    scheduled_jobs: list[ScheduledJobOut]
    runs_last_24h: dict[str, int]
