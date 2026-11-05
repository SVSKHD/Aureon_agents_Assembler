"""Reading the feed's health and asking it to reconnect (docs/28-dhan-operations.md).

`load_health` is what the health API, the integrations card and the monitor all use, so they
can never disagree. `request_reconnect` is the ONLY recovery action for market data: it asks
the dhan-feed worker to drop and re-open its WebSocket (re-reading the token). It cannot
place, modify or cancel anything, change an account, a mode, a risk policy or a credential.
It is bounded by a shared cooldown (Redis SET NX), so neither an admin nor the automatic
recovery can make the worker reconnect in a loop.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.registry import effective_state
from app.core.config import Settings
from app.models.agents import Agent
from app.services import dhan_feed_health as fh

FEED_AGENT = "dhan-feed"


def agent_view(agent: Agent | None, settings: Settings, now: datetime) -> dict[str, Any]:
    if agent is None:
        return {
            "registered": False,
            "state": None,
            "stale": True,
            "last_heartbeat_at": None,
            "age_s": None,
            "paused": False,
            "details": {},
        }
    state = effective_state(agent, settings.AGENT_STALE_SECONDS, now)
    age = (now - agent.last_heartbeat_at).total_seconds() if agent.last_heartbeat_at else None
    return {
        "registered": True,
        "state": state,
        "stale": state in ("stale", "stopped"),
        "last_heartbeat_at": agent.last_heartbeat_at,
        "age_s": round(age, 1) if age is not None else None,
        "paused": agent.paused,
        "details": agent.details or {},
    }


async def read_status(
    redis: Redis | None,
) -> tuple[dict[str, Any] | None, bool, dict[str, Any] | None]:
    """(status record, readable, last crash). Never raises."""
    if redis is None:
        return None, False, None
    try:
        raw = await redis.get(fh.STATUS_KEY)
        crash = await redis.get(fh.LAST_CRASH_KEY)
    except Exception:
        return None, False, None
    return fh.parse_status(raw), True, fh.parse_status(crash)


async def load_health(
    maker: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    status, readable, crash = await read_status(redis)
    async with maker() as db:
        agent = await db.scalar(select(Agent).where(Agent.id == FEED_AGENT))
    view = agent_view(agent, settings, now)
    out = fh.assess(settings, status, view, now, status_readable=readable)
    # The heartbeat details duplicate the status record; keep the response small.
    if out["worker"] is not None:
        out["worker"] = {k: v for k, v in out["worker"].items() if k != "details"}
    out["last_crash"] = crash
    return out


@dataclass(frozen=True, slots=True)
class ReconnectResult:
    accepted: bool
    retry_after_s: int = 0
    receivers: int = 0


class ControlUnavailable(Exception):
    """Redis is down: a reconnect cannot be requested (fail closed)."""


async def request_reconnect(
    redis: Redis | None, settings: Settings, origin: str
) -> ReconnectResult:
    """Ask the feed worker to reconnect, at most once per cooldown (shared by every caller)."""
    if redis is None:
        raise ControlUnavailable("no Redis")
    cooldown = max(1, int(settings.DHAN_FEED_RECONNECT_COOLDOWN_SECONDS))
    try:
        if not await redis.set(fh.RECONNECT_COOLDOWN_KEY, origin, nx=True, ex=cooldown):
            ttl = await redis.ttl(fh.RECONNECT_COOLDOWN_KEY)
            return ReconnectResult(False, max(1, int(ttl or cooldown)))
        n = await redis.publish(
            fh.CONTROL_CHANNEL,
            json.dumps({"cmd": "reconnect", "origin": origin, "at": datetime.now(UTC).isoformat()}),
        )
    except Exception as exc:
        raise ControlUnavailable(type(exc).__name__) from exc
    return ReconnectResult(True, 0, int(n or 0))
