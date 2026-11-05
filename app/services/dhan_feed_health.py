"""Dhan feed health: one place that decides whether the live feed is OK (docs/28).

The dhan-feed worker writes a status record to Redis every few seconds (`STATUS_KEY`, JSON,
short TTL). Its agent heartbeat (Postgres) carries the same numbers, so a Redis outage is
still visible. `assess()` combines the two with the NSE calendar:

| status          | meaning                                                              |
|-----------------|----------------------------------------------------------------------|
| ok              | market open, connected, every subscribed instrument fresh             |
| degraded        | running but some instruments stale, subscriptions incomplete, or     |
|                 | Redis writes failing (ticks are held, newest per instrument)          |
| down            | market open and disconnected / no fresh data; auth failed; crash-loop |
|                 | cooldown; waiting for credentials                                     |
| market_closed   | weekend, holiday (DHAN_MARKET_HOLIDAYS) or outside 09:15-15:30 IST;   |
|                 | silence is expected. Never reported as ok                             |
| not_running     | no fresh status and no fresh heartbeat: the worker is not running     |
| unknown         | the status could not be read (Redis down) and the heartbeat is stale  |
| not_configured  | no DHAN_CLIENT_ID or no DHAN_FEED_INSTRUMENTS                         |

`healthy` is true ONLY for `ok`: a feed is never reported healthy without fresh data.
Every problem also has a stable code (`codes`): the monitor keys incidents and recovery on it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime, time
from typing import Any

from app.agents.trading.dhan.parse import IST
from app.core.config import Settings

STATUS_KEY = "dhan:feed:status"
STATUS_TTL = 120
CONTROL_CHANNEL = "dhan:feed:control"
LEADER_KEY = "dhan:feed:leader"
LEADER_TTL = 30
STARTS_KEY = "dhan:feed:starts"
LAST_CRASH_KEY = "dhan:feed:last_crash"
RECONNECT_COOLDOWN_KEY = "dhan:feed:reconnect-cooldown"
CRASH_WINDOW_S = 600

OPEN_AT = time(9, 15)
CLOSE_AT = time(15, 30)
PRE_OPEN_AT = time(9, 0)


def market_state(now: datetime, holidays: Iterable[date] = ()) -> dict[str, Any]:
    """NSE cash session for `now`: open / pre_open / closed / weekend / holiday."""
    local = now.astimezone(IST)
    day = local.date()
    if local.weekday() >= 5:
        session = "weekend"
    elif day in set(holidays):
        session = "holiday"
    elif OPEN_AT <= local.time() < CLOSE_AT:
        session = "open"
    elif PRE_OPEN_AT <= local.time() < OPEN_AT:
        session = "pre_open"
    else:
        session = "closed"
    opens = datetime.combine(day, OPEN_AT, IST).astimezone(UTC)
    return {"open": session == "open", "session": session, "opened_at": opens}


def is_open(now: datetime, holidays: Iterable[date] = ()) -> bool:
    return bool(market_state(now, holidays)["open"])


def parse_status(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _age(ts: Any, now: datetime) -> float | None:
    if not isinstance(ts, int | float):
        return None
    return max(0.0, now.timestamp() - float(ts))


def assess(
    settings: Settings,
    status: dict[str, Any] | None,
    agent: dict[str, Any] | None,
    now: datetime | None = None,
    *,
    status_readable: bool = True,
) -> dict[str, Any]:
    """The feed's health. `status` is the worker's Redis record (None = missing), `agent` the
    registry view (`registered`, `state`, `stale`, `details`), `status_readable` False when
    Redis could not be read at all."""
    now = now or datetime.now(UTC)
    market = market_state(now, settings.DHAN_MARKET_HOLIDAYS)
    configured = len(settings.DHAN_FEED_INSTRUMENTS)
    out: dict[str, Any] = {
        "status": "ok",
        "healthy": False,
        "problems": [],
        "codes": [],
        "market": {"open": market["open"], "session": market["session"]},
        "configured_instruments": configured,
        "feed": None,
        "worker": agent,
        "checked_at": now,
    }
    problems: list[str] = out["problems"]
    codes: list[str] = []
    out["codes"] = codes

    def problem(code: str, text: str) -> None:
        codes.append(code)
        problems.append(text)

    def done(st: str) -> dict[str, Any]:
        out["status"] = st
        out["healthy"] = st == "ok"
        return out

    if not settings.DHAN_CLIENT_ID or not configured:
        return done("not_configured")
    agent_alive = bool(agent and agent.get("registered") and not agent.get("stale"))
    age = _age(status.get("updated_at"), now) if status else None
    if status is None or age is None or age > settings.DHAN_FEED_STATUS_MAX_AGE_SECONDS:
        details = (agent or {}).get("details") or {}
        if agent_alive and details.get("redis_ok") is False:
            problem("redis_writes_failing", "the feed worker cannot write to Redis")
            return done("down")
        if agent_alive and details.get("state") in ("waiting_config", "standby", "cooldown"):
            problem("worker_not_ready", f"feed worker is {details.get('state')}")
            return done("down")
        if not status_readable and not agent_alive:
            problem(
                "status_unreadable", "feed status unreadable (Redis) and no fresh worker heartbeat"
            )
            return done("unknown")
        if not agent_alive:
            problem("not_running", "the dhan-feed worker is not running (no status, no heartbeat)")
            return done("not_running")
        problem("status_missing", "the feed worker is alive but has not reported its status")
        return done("down")

    feed = {
        k: status.get(k)
        for k in (
            "state",
            "connected",
            "subscribed",
            "fresh",
            "stale",
            "last_packet_at",
            "reconnects",
            "consecutive_failures",
            "redis_ok",
            "pending_writes",
            "invalid",
            "duplicates",
            "out_of_order",
            "dropped",
            "auth_failed",
            "last_disconnect_reason",
            "token_source",
        )
    }
    feed["status_age_s"] = round(age, 1)
    lp_age = _age(status.get("last_packet_at"), now)
    feed["last_packet_age_s"] = round(lp_age, 1) if lp_age is not None else None
    out["feed"] = feed
    state = status.get("state")
    if state == "stopped":
        problem("not_running", "the dhan-feed worker stopped")
        return done("not_running")
    if status.get("auth_failed") or state == "auth_failed":
        problem("auth_failed", "Dhan refused the feed's token (or no data plan): log in to Dhan")
        return done("down")
    if state in ("waiting_config", "cooldown", "standby"):
        problem("worker_not_ready", f"feed worker is {state}")
        return done("down")
    if status.get("redis_ok") is False:
        problem("redis_writes_failing", "Redis writes are failing; newest ticks are held for retry")
    if not market["open"]:
        if problems:
            return done("degraded")
        return done("market_closed")
    if not status.get("connected"):
        problem("disconnected", "disconnected from Dhan while the market is open")
        return done("down")
    fresh = int(status.get("fresh") or 0)
    stale = int(status.get("stale") or 0)
    subscribed = int(status.get("subscribed") or 0)
    if fresh == 0:
        problem("no_fresh_data", "no fresh data from any instrument while the market is open")
        return done("down")
    if stale:
        problem("stale_instruments", f"{stale} instrument(s) stale")
    if subscribed < configured:
        problem("subscriptions_incomplete", f"subscribed {subscribed} of {configured} instruments")
    return done("degraded" if problems else "ok")
