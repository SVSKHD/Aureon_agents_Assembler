"""Safe recovery actions, tried before any repair (docs/21-self-repair.md).

An allowlist of in-code actions; nothing here runs a shell or a command line:

- `restart_agent`: pause then resume a stale agent through the registry (the in-process loop
  picks the flag up on its next heartbeat). Trading / broker agents are never touched.
- `clear_queue_lock`: delete the Redis worker lock owned by a stale agent (a fixed map), so a
  restarted worker can take over.
- `rerun_actions`: re-run the failed jobs of the incident's GitHub Actions run, ONCE per run,
  through the Phase 17 rerun (its dispatch allowlist and production approval gating apply).
- `restart_container`: only when the docker-proxy allows POST restart
  (INCIDENT_DOCKER_PROXY_ALLOWS_RESTART) and the container is in INCIDENT_RESTART_CONTAINERS;
  the shipped proxy is read-only, so this records `manual`.
- `reconnect_dhan_feed`: MARKET DATA ONLY. Asks the dhan-feed worker to re-open its WebSocket
  (app/services/dhan_feed_control.py, shared cooldown). Used only for the Dhan monitor's
  recoverable feed checks (app/services/dhan_monitor.py RECOVERABLE). It cannot touch an
  account, order, execution mode, risk policy or credential; trading agents are still never
  restarted (NEVER_RESTART_*), and a credential problem is never "recovered" here.

Every attempt is audited (`incident_recovery_attempt`), idempotent, and limited to
INCIDENT_MAX_RECOVERY_ATTEMPTS per incident, at least INCIDENT_RECOVERY_COOLDOWN_SECONDS
apart. After that, or when no action applies, the incident escalates
(`incident_escalated`): one that maps to a code location gets a repair task (all the repair
guards apply), anything else is marked for manual attention.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.github.client import GitHubClient, GitHubError
from app.models.agents import Agent
from app.models.repairs import Incident
from app.services import audit, dhan_feed_control, dhan_monitor, repairs
from app.services import github as gh
from app.services import github_actions as actions
from app.services import github_ops as ops
from app.services.approvals import ApprovalError, ApprovalGate

log = get_logger("services.repair_recovery")

Outcome = Literal["done", "manual", "failed", "skipped"]
# Redis locks a stale agent may own (agent id -> keys). See app/workers/imaging.py.
AGENT_LOCKS: dict[str, tuple[str, ...]] = {"imaging": ("aureon:imaging:worker-lock",)}
NEVER_RESTART_KINDS = frozenset({"trading", "broker"})
NEVER_RESTART_PREFIXES = ("dhan", "mt5", "trading", "paper", "risk")
MAX_HISTORY = 10


@dataclass(frozen=True, slots=True)
class Result:
    action: str
    outcome: Outcome
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"action": self.action, "outcome": self.outcome, "detail": self.detail[:300]}


@dataclass
class Deps:
    maker: async_sessionmaker[AsyncSession]
    settings: Settings
    redis: Redis | None = None
    github: GitHubClient | None = None
    http: httpx.AsyncClient | None = None


ActionFn = Callable[[Deps, Incident], Awaitable[Result]]


# --- actions --------------------------------------------------------------------------------


async def restart_agent(deps: Deps, inc: Incident) -> Result:
    agent_id = str(inc.details.get("agent_id") or "")
    name = "restart_agent"
    if not agent_id:
        return Result(name, "skipped", "no agent id")
    if inc.details.get("kind") in NEVER_RESTART_KINDS or agent_id.startswith(
        NEVER_RESTART_PREFIXES
    ):
        return Result(name, "manual", f"{agent_id} is a trading agent: never auto-restarted")
    async with deps.maker() as db, db.begin():
        paused = await db.scalar(select(Agent.paused).where(Agent.id == agent_id))
        if paused is None:
            return Result(name, "skipped", f"agent {agent_id} is not registered")
        if paused:
            return Result(name, "skipped", f"agent {agent_id} is paused by an admin")
        await db.execute(update(Agent).where(Agent.id == agent_id).values(paused=True))
    async with deps.maker() as db, db.begin():
        await db.execute(update(Agent).where(Agent.id == agent_id).values(paused=False))
    return Result(name, "done", f"paused and resumed {agent_id}")


async def clear_queue_lock(deps: Deps, inc: Incident) -> Result:
    agent_id = str(inc.details.get("agent_id") or "")
    keys = AGENT_LOCKS.get(agent_id, ())
    name = "clear_queue_lock"
    if not keys:
        return Result(name, "skipped", "the agent owns no Redis lock")
    if deps.redis is None:
        return Result(name, "failed", "Redis is not available")
    try:
        n = await deps.redis.delete(*keys)
    except Exception as exc:
        return Result(name, "failed", type(exc).__name__)
    return Result(name, "done", f"cleared {n} lock(s)")


async def rerun_actions(deps: Deps, inc: Incident) -> Result:
    name = "rerun_actions"
    d = inc.details
    run_id, full = d.get("run_id"), str(d.get("repo") or "")
    if not isinstance(run_id, int) or "/" not in full:
        return Result(name, "skipped", "no run to re-run")
    if run_id in (d.get("_reruns") or []):
        return Result(name, "skipped", f"run {run_id} was already re-run once")
    if deps.github is None:
        return Result(name, "manual", "GitHub is not configured")
    owner, _, repo = full.partition("/")
    try:
        async with deps.maker() as db:
            full = await gh.require_allowed(db, deps.github, deps.settings, owner, repo)
        out = await actions.rerun(
            deps.maker,
            ApprovalGate(deps.maker, deps.settings),
            deps.github,
            ops.Actor(None, repairs.SYSTEM_CLIENT),
            deps.settings,
            full,
            run_id,
            failed_only=True,
        )
    except actions.ActionsRefused as exc:
        return Result(name, "manual", f"{exc.code}: {exc.message}")
    except (GitHubError, ApprovalError) as exc:
        return Result(name, "failed", str(getattr(exc, "message", exc))[:200])
    return Result(name, "done", f"re-run {out.status} for run {run_id}")


async def restart_container(deps: Deps, inc: Incident) -> Result:
    name = "restart_container"
    container = str(inc.details.get("container") or "")
    s = deps.settings
    if not s.INCIDENT_DOCKER_PROXY_ALLOWS_RESTART:
        return Result(name, "manual", "the docker-proxy is read-only (GET /containers only)")
    if container not in s.INCIDENT_RESTART_CONTAINERS:
        return Result(name, "manual", f"{container} is not in INCIDENT_RESTART_CONTAINERS")
    if deps.http is None or not s.DOCKER_API_URL:
        return Result(name, "failed", "the docker-proxy is not reachable from here")
    try:
        resp = await deps.http.post(
            f"{s.DOCKER_API_URL.rstrip('/')}/containers/{container}/restart", params={"t": "10"}
        )
    except httpx.HTTPError as exc:
        return Result(name, "failed", type(exc).__name__)
    if resp.status_code in (200, 204):
        return Result(name, "done", f"restarted {container}")
    if resp.status_code == 403:
        return Result(name, "manual", "the docker-proxy refused POST restart")
    return Result(name, "failed", f"docker-proxy answered {resp.status_code}")


async def reconnect_dhan_feed(deps: Deps, inc: Incident) -> Result:
    name = "reconnect_dhan_feed"
    try:
        res = await dhan_feed_control.request_reconnect(
            deps.redis, deps.settings, f"recovery:{inc.id}"
        )
    except dhan_feed_control.ControlUnavailable as exc:
        return Result(name, "failed", f"feed control unavailable: {exc}")
    if not res.accepted:
        return Result(name, "skipped", f"reconnect cooldown ({res.retry_after_s}s left)")
    if res.receivers == 0:
        return Result(name, "failed", "no dhan-feed worker is listening")
    return Result(name, "done", "asked the dhan-feed worker to reconnect")


ACTIONS: dict[str, ActionFn] = {
    "restart_agent": restart_agent,
    "clear_queue_lock": clear_queue_lock,
    "rerun_actions": rerun_actions,
    "restart_container": restart_container,
    "reconnect_dhan_feed": reconnect_dhan_feed,
}
PLANS: dict[str, tuple[str, ...]] = {
    "heartbeat": ("restart_agent", "clear_queue_lock"),
    "actions": ("rerun_actions",),
    "docker": ("restart_container",),
}
# Market-data recovery, kept apart from everything else: the only actions a Dhan incident may
# ever run. Nothing in this set can reach trading (tests/test_dhan_operations.py pins it).
MARKET_DATA_ACTIONS = frozenset({"reconnect_dhan_feed"})
DHAN_PLANS: dict[str, tuple[str, ...]] = {
    f"feed_{code}": ("reconnect_dhan_feed",) for code in dhan_monitor.RECOVERABLE
}


def plan(inc: Incident) -> tuple[str, ...]:
    if inc.source == dhan_monitor.SOURCE:
        names = DHAN_PLANS.get(str((inc.details or {}).get("check") or ""), ())
        return tuple(n for n in names if n in MARKET_DATA_ACTIONS)
    return PLANS.get(inc.source, ())


# --- the loop -------------------------------------------------------------------------------


def maps_to_code(inc: Incident) -> bool:
    loc = (inc.details or {}).get("location")
    return isinstance(loc, dict) and bool(loc.get("file"))


async def escalate(db: AsyncSession, settings: Settings, inc: Incident, reason: str) -> str:
    """Hand the incident on: a repair task when it maps to code, else manual attention."""
    now = datetime.now(UTC).isoformat()
    outcome = "manual"
    extra: dict[str, Any] = {"_escalated": {"at": now, "reason": reason}}
    if maps_to_code(inc) and not inc.details.get("self_origin"):
        try:
            # Every refusal is raised before create_repair writes anything.
            task = await repairs.create_repair(db, settings, inc)
            outcome = "blocked" if task.status == "blocked" else "repair_queued"
            extra["_repair_id"] = str(task.id)
        except repairs.RepairRefused as exc:
            extra["_repair_refused"] = f"{exc.code}: {exc.message}"
    elif inc.details.get("self_origin"):
        extra["_repair_refused"] = "recursion_guard: the repair system's own incident"
    if outcome == "manual" or outcome == "blocked":
        extra["_manual"] = True
    inc.details = inc.details | extra
    audit.record(
        db,
        "incident_escalated",
        repairs.SYSTEM_CLIENT,
        details={"incident_id": str(inc.id), "reason": reason, "outcome": outcome}
        | ({"refused": extra["_repair_refused"]} if "_repair_refused" in extra else {}),
    )
    log.warning("incident_escalated", incident=str(inc.id), outcome=outcome, reason=reason)
    return outcome


async def handle(deps: Deps, incident_id: uuid.UUID, now: datetime | None = None) -> str:
    """One recovery step for one incident. Returns what happened."""
    s = deps.settings
    now = now or datetime.now(UTC)
    async with deps.maker() as db, db.begin():
        inc = await db.scalar(
            select(Incident)
            .where(Incident.id == incident_id, Incident.status.in_(("open", "recovering")))
            .with_for_update(skip_locked=True)
        )
        if inc is None or inc.details.get("_escalated"):
            return "skipped"
        names = plan(inc)
        if not names:
            return await escalate(db, s, inc, "no safe recovery action applies")
        if inc.recovery_attempts >= s.INCIDENT_MAX_RECOVERY_ATTEMPTS:
            return await escalate(
                db, s, inc, f"{inc.recovery_attempts} recovery attempts did not fix it"
            )
        if (
            inc.last_recovery_at is not None
            and (now - inc.last_recovery_at).total_seconds() < s.INCIDENT_RECOVERY_COOLDOWN_SECONDS
        ):
            return "cooldown"
        results = [await ACTIONS[n](deps, inc) for n in names]
        inc.recovery_attempts += 1
        inc.last_recovery_at = now
        history = list(inc.details.get("_recovery") or [])
        history.append(
            {
                "attempt": inc.recovery_attempts,
                "at": now.isoformat(),
                "results": [r.as_dict() for r in results],
            }
        )
        extra: dict[str, Any] = {"_recovery": history[-MAX_HISTORY:]}
        for r in results:
            if r.action == "rerun_actions" and r.outcome == "done":
                extra["_reruns"] = [*(inc.details.get("_reruns") or []), inc.details["run_id"]]
        inc.details = inc.details | extra
        audit.record(
            db,
            "incident_recovery_attempt",
            repairs.SYSTEM_CLIENT,
            details={
                "incident_id": str(inc.id),
                "attempt": inc.recovery_attempts,
                "results": [r.as_dict() for r in results],
            },
        )
        outcomes = {r.outcome for r in results}
        if "done" in outcomes:
            inc.status = "recovering"
            return "recovering"
        if outcomes <= {"manual", "skipped"} and "manual" in outcomes:
            return await escalate(db, s, inc, "recovery needs a person (manual)")
        return "attempted"


async def process_incidents(deps: Deps, limit: int = 50) -> dict[str, Any]:
    async with deps.maker() as db:
        ids = (
            await db.scalars(
                select(Incident.id)
                .where(
                    Incident.status.in_(("open", "recovering")),
                    Incident.details["_escalated"].astext.is_(None),
                )
                .order_by(Incident.last_seen)
                .limit(limit)
            )
        ).all()
    out: dict[str, int] = {}
    for iid in ids:
        try:
            what = await handle(deps, iid)
        except Exception as exc:
            log.warning("incident_recovery_failed", incident=str(iid), error=type(exc).__name__)
            what = "error"
        out[what] = out.get(what, 0) + 1
    return {"processed": len(ids), "outcomes": out}
