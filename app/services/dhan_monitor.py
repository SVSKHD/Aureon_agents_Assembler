"""Dhan monitoring: auth, Redis, feed worker, subscriptions, staleness -> incidents.

Runs as the scheduler job `dhan-monitor` (DHAN_MONITOR_CRON, every minute). Each check that
fails opens (or bumps) one incident with source `dhan` and a stable fingerprint; a check
that passes resolves it. From there the EXISTING pipeline takes over (docs/21-self-repair.md):

- `incident-scan` runs app/services/repair_recovery.py, which tries the check's recovery plan
  at most INCIDENT_MAX_RECOVERY_ATTEMPTS times, INCIDENT_RECOVERY_COOLDOWN_SECONDS apart,
  audits every attempt, then escalates for a person;
- `alerts-dispatch` sends open warning/error incidents to Discord (deduplicated; a failed
  send is released and retried on the next run).

Only feed problems a reconnect can fix are recoverable (`RECOVERABLE`), and the only action
is `reconnect_dhan_feed` (market data). Credential problems are NEVER recovered
automatically: Dhan's supported renewal needs the user (consent login with OTP), so the
incident says what to do and escalates to manual attention straight away.

Read-only: nothing here calls Dhan, touches an account, order, mode, risk policy or token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.brokers import DhanConnection
from app.services import dhan_auth, dhan_connection, dhan_feed_control, incidents

log = get_logger("services.dhan_monitor")

SOURCE = "dhan"
# Feed problem codes (dhan_feed_health.assess) a reconnect can fix.
RECOVERABLE = frozenset(
    {"disconnected", "no_fresh_data", "stale_instruments", "subscriptions_incomplete"}
)
FEED_CODES = (
    "redis_writes_failing",
    "worker_not_ready",
    "status_unreadable",
    "not_running",
    "status_missing",
    "auth_failed",
    "disconnected",
    "no_fresh_data",
    "stale_instruments",
    "subscriptions_incomplete",
)
WARNING_CODES = frozenset({"stale_instruments", "subscriptions_incomplete", "status_unreadable"})
# After this many failed verification attempts in a row (Dhan unreachable) say so.
UNVERIFIED_WARN_AFTER = 3
RENEWAL_NOTE = (
    "automatic renewal is not available: Dhan's supported renewal needs your own login "
    "(consent + OTP)"
)
FEED_HINTS = {
    "not_running": "start it: add `dhan` to COMPOSE_PROFILES and redeploy "
    "(docker compose --profile dhan up -d)",
    "auth_failed": "log in to Dhan on /settings; the feed reconnects by itself",
    "worker_not_ready": "see the dhan-feed agent log on /dev/agents",
    "redis_writes_failing": "check the Redis container; held ticks are written on recovery",
    "status_unreadable": "check the Redis container",
    "status_missing": "see the dhan-feed agent log on /dev/agents",
}


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    severity: str = "info"
    title: str = ""
    recoverable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self, client_id: str) -> str:
        return incidents.fingerprint(SOURCE, self.name, client_id)


def auth_check(settings: Settings, connection: dict[str, Any], now: datetime) -> list[Check]:
    st = connection["status"]
    session = dhan_auth.status(settings, now)
    unusable = st in dhan_connection.UNUSABLE or session["source"] == "none"
    status = (
        st
        if st in dhan_connection.UNUSABLE
        else ("expired" if session["session_expired"] else "disconnected")
    )
    out = [
        Check(
            "auth",
            not unusable,
            "error",
            f"Dhan credential {status}: {dhan_connection.ACTIONS.get(status)}",
            False,
            {
                "check": "auth",
                "status": status,
                "renewal": "manual",
                "action": dhan_connection.ACTIONS.get(status),
                "note": RENEWAL_NOTE,
            },
        )
    ]
    failing = int(connection.get("consecutive_failures") or 0)
    unreachable = (
        not unusable
        and connection.get("last_check_outcome") == "unavailable"
        and failing >= UNVERIFIED_WARN_AFTER
    )
    out.append(
        Check(
            "auth_unverified",
            not unreachable,
            "warning",
            f"Dhan unreachable for credential checks ({failing} in a row)",
            False,
            {
                "check": "auth_unverified",
                "consecutive_failures": failing,
                "last_error_code": connection.get("last_error_code"),
            },
        )
    )
    return out


def feed_checks(health: dict[str, Any]) -> list[Check]:
    """One check per known feed problem code (passing when the code is absent)."""
    codes = set(health.get("codes") or [])
    problems = dict(zip(health.get("codes") or [], health.get("problems") or [], strict=False))
    out = []
    for code in FEED_CODES:
        failing = code in codes
        out.append(
            Check(
                f"feed_{code}",
                not failing,
                "warning" if code in WARNING_CODES else "error",
                f"Dhan feed: {problems.get(code, code)}",
                code in RECOVERABLE,
                {
                    "check": f"feed_{code}",
                    "code": code,
                    "agent_id": "dhan-feed",
                    "kind": "market_data",
                    "feed_status": health.get("status"),
                    "market": health.get("market"),
                    "recoverable": code in RECOVERABLE,
                    **({"action": FEED_HINTS[code]} if code in FEED_HINTS else {}),
                },
            )
        )
    return out


def evaluate(
    settings: Settings,
    connection: dict[str, Any],
    health: dict[str, Any],
    redis_ok: bool,
    now: datetime,
) -> list[Check]:
    if not (settings.DHAN_CLIENT_ID or "").strip():
        return []
    checks = auth_check(settings, connection, now)
    checks.append(
        Check(
            "redis",
            redis_ok,
            "error",
            "Redis unreachable: Dhan ticks, rate limits and feed control are unavailable",
            False,
            {"check": "redis"},
        )
    )
    if settings.DHAN_FEED_INSTRUMENTS:
        checks.extend(feed_checks(health))
    return checks


async def _redis_ok(redis: Redis | None) -> bool:
    if redis is None:
        return False
    try:
        return bool(await redis.ping())
    except Exception:
        return False


async def apply(db: AsyncSession, client_id: str, checks: list[Check]) -> dict[str, int]:
    """Open/bump failing checks, resolve passing ones (caller's transaction)."""
    opened = resolved = 0
    for c in checks:
        fp = c.fingerprint(client_id)
        if c.ok:
            resolved += await incidents.resolve_fp(db, fp, f"dhan monitor: {c.name} healthy")
        else:
            await incidents.record(
                db,
                source=SOURCE,
                fp=fp,
                severity=c.severity,
                title=c.title,
                details={**c.details, "recoverable": c.recoverable},
            )
            opened += 1
    return {"failing": opened, "resolved": resolved}


async def run(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    cid = (settings.DHAN_CLIENT_ID or "").strip()
    if not cid:
        return {"skipped": "DHAN_CLIENT_ID not set"}
    redis_ok = await _redis_ok(redis)
    health = await dhan_feed_control.load_health(maker, redis if redis_ok else None, settings, now)
    async with maker() as db:
        row = await db.get(DhanConnection, cid)
    connection = dhan_connection.view(row, settings)
    checks = evaluate(settings, connection, health, redis_ok, now)
    async with maker() as db, db.begin():
        counts = await apply(db, cid, checks)
    failing = [c.name for c in checks if not c.ok]
    if failing:
        log.warning("dhan_monitor_failing", checks=failing)
    return {
        **counts,
        "failing_checks": failing,
        "feed_status": health["status"],
        "connection": connection["status"],
        "redis_ok": redis_ok,
    }
