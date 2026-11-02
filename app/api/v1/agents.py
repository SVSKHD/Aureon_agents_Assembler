"""Agent registry, scheduler and system status endpoints.

Reads of agents and runs: any signed-in user. Logs, scheduler jobs and system status: admin.
Pause, resume and run-now: admin only, with CSRF, and audited.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import func, select, update

from app.agents.registry import effective_state
from app.auth.deps import AdminRequired, Current, DbSession, SettingsDep
from app.core.config import Settings
from app.models.agents import Agent, AgentLog, AgentRun, ScheduledJob
from app.schemas.agents import (
    AgentDetail,
    AgentList,
    AgentOut,
    LogOut,
    Page,
    RunOut,
    ScheduledJobOut,
)
from app.services import audit
from app.services.audit import ClientInfo
from app.services.system import SystemStatus

router = APIRouter(tags=["agents"])


def _agent_out(a: Agent, settings: Settings) -> AgentOut:
    now = datetime.now(UTC)
    return AgentOut(
        id=a.id,
        name=a.name,
        kind=a.kind,
        state=effective_state(a, settings.AGENT_STALE_SECONDS, now),
        reported_state=a.state,
        paused=a.paused,
        version=a.version,
        host=a.host,
        last_heartbeat_at=a.last_heartbeat_at,
        seconds_since_heartbeat=(
            round((now - a.last_heartbeat_at).total_seconds(), 1) if a.last_heartbeat_at else None
        ),
        last_activity_at=a.last_activity_at,
        registered_at=a.registered_at,
        details=a.details,
    )


def _job_out(j: ScheduledJob) -> ScheduledJobOut:
    running = j.locked_until is not None and j.locked_until > datetime.now(UTC)
    return ScheduledJobOut(
        id=j.id,
        agent_id=j.agent_id,
        task=j.task,
        cron=j.cron,
        paused=j.paused,
        max_retries=j.max_retries,
        retry_backoff_seconds=j.retry_backoff_seconds,
        timeout_seconds=j.timeout_seconds,
        attempt=j.attempt,
        consecutive_failures=j.consecutive_failures,
        next_run_at=j.next_run_at,
        last_run_at=j.last_run_at,
        last_status=j.last_status,
        last_error=j.last_error,
        running=running,
    )


async def _agent_or_404(db: DbSession, agent_id: str) -> Agent:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    return agent


@router.get("/agents", response_model=AgentList)
async def list_agents(db: DbSession, _: Current, settings: SettingsDep) -> AgentList:
    rows = (await db.scalars(select(Agent).order_by(Agent.id))).all()
    return AgentList(agents=[_agent_out(a, settings) for a in rows])


@router.get("/agents/{agent_id}", response_model=AgentDetail)
async def get_agent(agent_id: str, db: DbSession, _: Current, settings: SettingsDep) -> AgentDetail:
    agent = await _agent_or_404(db, agent_id)
    jobs = (
        await db.scalars(
            select(ScheduledJob).where(ScheduledJob.agent_id == agent_id).order_by(ScheduledJob.id)
        )
    ).all()
    since = datetime.now(UTC) - timedelta(hours=24)
    counts = dict(
        (
            await db.execute(
                select(AgentRun.status, func.count())
                .where(AgentRun.agent_id == agent_id, AgentRun.started_at >= since)
                .group_by(AgentRun.status)
            )
        )
        .tuples()
        .all()
    )
    return AgentDetail(
        **_agent_out(agent, settings).model_dump(),
        scheduled_jobs=[_job_out(j) for j in jobs],
        runs_last_24h=counts,
    )


@router.get("/agents/{agent_id}/runs", response_model=Page[RunOut])
async def agent_runs(
    agent_id: str,
    db: DbSession,
    _: Current,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status_filter: Annotated[
        Literal["running", "succeeded", "failed", "cancelled"] | None, Query(alias="status")
    ] = None,
) -> Page[RunOut]:
    await _agent_or_404(db, agent_id)
    cond = [AgentRun.agent_id == agent_id]
    if status_filter:
        cond.append(AgentRun.status == status_filter)
    total = await db.scalar(select(func.count()).select_from(AgentRun).where(*cond))
    rows = (
        await db.scalars(
            select(AgentRun)
            .where(*cond)
            .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page[RunOut](
        items=[RunOut.model_validate(r, from_attributes=True) for r in rows],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


@router.get("/agents/{agent_id}/logs", response_model=Page[LogOut])
async def agent_logs(
    agent_id: str,
    db: DbSession,
    _: AdminRequired,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    level: Literal["debug", "info", "warning", "error"] | None = None,
    run_id: int | None = None,
) -> Page[LogOut]:
    await _agent_or_404(db, agent_id)
    cond = [AgentLog.agent_id == agent_id]
    if level:
        cond.append(AgentLog.level == level)
    if run_id is not None:
        cond.append(AgentLog.run_id == run_id)
    total = await db.scalar(select(func.count()).select_from(AgentLog).where(*cond))
    rows = (
        await db.scalars(
            select(AgentLog)
            .where(*cond)
            .order_by(AgentLog.created_at.desc(), AgentLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page[LogOut](
        items=[LogOut.model_validate(r, from_attributes=True) for r in rows],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


async def _set_agent_paused(
    request: Request,
    agent_id: str,
    paused: bool,
    db: DbSession,
    admin: AdminRequired,
    settings: Settings,
) -> AgentOut:
    agent = await _agent_or_404(db, agent_id)
    before = agent.paused
    agent.paused = paused
    audit.record(
        db,
        "agent_paused" if paused else "agent_resumed",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"agent_id": agent_id, "was_paused": before},
    )
    await db.commit()
    # Agents in this process (the ollama chat agent) see it immediately; others within
    # one heartbeat (AGENT_HEARTBEAT_SECONDS).
    request.app.state.agent_paused[agent_id] = paused
    await db.refresh(agent)
    return _agent_out(agent, settings)


@router.post("/agents/{agent_id}/pause", response_model=AgentOut)
async def pause_agent(
    agent_id: str, request: Request, db: DbSession, admin: AdminRequired, settings: SettingsDep
) -> AgentOut:
    return await _set_agent_paused(request, agent_id, True, db, admin, settings)


@router.post("/agents/{agent_id}/resume", response_model=AgentOut)
async def resume_agent(
    agent_id: str, request: Request, db: DbSession, admin: AdminRequired, settings: SettingsDep
) -> AgentOut:
    return await _set_agent_paused(request, agent_id, False, db, admin, settings)


# --- scheduler ----------------------------------------------------------------------------


@router.get("/scheduler/jobs", response_model=list[ScheduledJobOut])
async def list_jobs(db: DbSession, _: AdminRequired) -> list[ScheduledJobOut]:
    rows = (await db.scalars(select(ScheduledJob).order_by(ScheduledJob.id))).all()
    return [_job_out(j) for j in rows]


async def _job_action(
    request: Request, job_id: str, action: str, db: DbSession, admin: AdminRequired
) -> ScheduledJobOut:
    values: dict[str, Any] = {"updated_at": func.now()}
    if action == "pause":
        values["paused"] = True
    elif action == "resume":
        values["paused"] = False
    else:  # run now: due immediately, fresh retry counter
        values |= {"next_run_at": func.now(), "attempt": 0}
    job = await db.scalar(
        update(ScheduledJob)
        .where(ScheduledJob.id == job_id)
        .values(**values)
        .returning(ScheduledJob)
    )
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "scheduled job not found")
    audit.record(
        db,
        f"scheduled_job_{action}",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"job_id": job_id},
    )
    await db.commit()
    return _job_out(job)


@router.post("/scheduler/jobs/{job_id}/pause", response_model=ScheduledJobOut)
async def pause_job(
    job_id: str, request: Request, db: DbSession, admin: AdminRequired
) -> ScheduledJobOut:
    return await _job_action(request, job_id, "pause", db, admin)


@router.post("/scheduler/jobs/{job_id}/resume", response_model=ScheduledJobOut)
async def resume_job(
    job_id: str, request: Request, db: DbSession, admin: AdminRequired
) -> ScheduledJobOut:
    return await _job_action(request, job_id, "resume", db, admin)


@router.post("/scheduler/jobs/{job_id}/run", response_model=ScheduledJobOut)
async def run_job_now(
    job_id: str, request: Request, db: DbSession, admin: AdminRequired
) -> ScheduledJobOut:
    return await _job_action(request, job_id, "run", db, admin)


# --- system -------------------------------------------------------------------------------


@router.get("/system/status")
async def system_status(request: Request, _: AdminRequired) -> dict[str, Any]:
    s = request.app.state
    return await SystemStatus(
        s.settings, s.engine, s.sessionmaker, s.redis, s.http, s.ollama
    ).collect()
