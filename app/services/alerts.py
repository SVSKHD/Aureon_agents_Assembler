"""Operational alerts to Discord (scheduler task `alerts_dispatch`, docs/24).

Each run collects what needs a human:

- open incidents (agents.incidents, status open/recovering/repair_queued, severity >= warning;
  heartbeat incidents are reported by the heartbeat check below instead, not twice);
- stale heartbeats (not paused, not deliberately stopped; the same threshold as the incident
  scanner);
- failed or too-old backups, from the status file scripts/ops/backup.sh writes (skipped,
  without an alert, when the file is not readable);
- pending approval requests that expire within ALERTS_APPROVAL_EXPIRY_MINUTES (the approval
  engine already announces the expiry itself);
- the Dhan session (API key login, or the manual token): expired, or expiring within
  DHAN_SESSION_ALERT_MINUTES. Keyed by the expiry time, so a new login starts fresh.

and sends each through the Phase 20 Discord adapter. Deduplicated: one alert per fingerprint
per ALERTS_DEDUP_MINUTES (a Redis key `SET NX EX`; an in-process map without Redis). A failed
send releases its key so the next run retries. A no-op when DISCORD_WEBHOOK_URL is unset.
Nothing here can act on anything: alerts only inform, and the link only opens the app.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations import discord
from app.models.agents import Agent
from app.models.approvals import ApprovalRequest
from app.models.repairs import INCIDENT_ACTIVE, Incident
from app.services import dhan_auth
from app.services.incidents import stale_threshold
from app.services.integrations import read_backup_status

log = get_logger("services.alerts")

DEDUP_PREFIX = "aureon:alerts:sent:"
ALERT_SEVERITIES = ("warning", "error", "critical")
_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}

Sender = Callable[[Settings, Mapping[str, Any]], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class Alert:
    kind: str  # incident | heartbeat | backup | approval | dhan_session
    fingerprint: str
    severity: str
    title: str
    detail: str
    path: str


def _fp(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


async def collect(
    maker: async_sessionmaker[AsyncSession], settings: Settings, now: datetime | None = None
) -> tuple[list[Alert], dict[str, Any]]:
    now = now or datetime.now(UTC)
    alerts: list[Alert] = []
    info: dict[str, Any] = {}
    async with maker() as db:
        incidents = (
            await db.scalars(
                select(Incident)
                .where(
                    Incident.status.in_(INCIDENT_ACTIVE),
                    Incident.severity.in_(ALERT_SEVERITIES),
                    Incident.source != "heartbeat",
                )
                .order_by(Incident.last_seen.desc())
                .limit(100)
            )
        ).all()
        agents = (await db.scalars(select(Agent))).all()
        soon = now + timedelta(minutes=settings.ALERTS_APPROVAL_EXPIRY_MINUTES)
        approvals = (
            await db.scalars(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.status == "pending",
                    ApprovalRequest.expires_at > now,
                    ApprovalRequest.expires_at <= soon,
                )
                .order_by(ApprovalRequest.expires_at)
                .limit(50)
            )
        ).all()
    # Most severe first, so the per-run cap keeps what matters.
    for inc in sorted(incidents, key=lambda i: -_RANK.get(i.severity, 0)):
        alerts.append(
            Alert(
                "incident",
                # Severity is part of the key: an escalation alerts again inside the window.
                _fp("incident", inc.fingerprint, inc.severity),
                inc.severity,
                f"Incident ({inc.source}): {inc.title}",
                f"status {inc.status}, seen {inc.count}x, last {inc.last_seen.isoformat()}",
                f"/dev/repairs?incident={inc.id}",
            )
        )
    for a in agents:
        if a.paused or a.state == "stopped":
            continue
        limit = stale_threshold(a, settings)
        last = a.last_heartbeat_at
        if last is not None and (now - last).total_seconds() <= limit:
            continue
        age = "never" if last is None else f"{(now - last).total_seconds():.0f}s ago"
        alerts.append(
            Alert(
                "heartbeat",
                _fp("heartbeat", a.id),
                "warning",
                f"Agent {a.id} heartbeat is stale",
                f"last heartbeat {age} (threshold {limit:.0f}s), host {a.host or '?'}",
                "/dev/agents",
            )
        )
    backup = await asyncio.to_thread(
        read_backup_status, settings.BACKUP_STATUS_FILE, settings.BACKUP_MAX_AGE_HOURS, now
    )
    info["backup"] = backup.get("status")
    if backup.get("status") == "down":
        problems = "; ".join(backup.get("problems") or [])
        alerts.append(
            Alert(
                "backup",
                _fp("backup", problems),
                "error",
                "Backup problem",
                f"{problems}. Last success: {backup.get('last_success_backup') or 'none'}",
                "/dev/status",
            )
        )
    dhan_alert = _dhan_session_alert(settings, now)
    if dhan_alert is not None:
        alerts.append(dhan_alert)
    for req in approvals:
        left = max(0, int((req.expires_at - now).total_seconds() // 60))
        alerts.append(
            Alert(
                "approval",
                _fp("approval-expiring", str(req.id)),
                "warning",
                f"Approval expires in {left} min: {req.kind}",
                f"{req.target}: {req.summary or ''}"[:400],
                f"/dev/approvals/{req.id}",
            )
        )
    return alerts, info


def _dhan_session_alert(settings: Settings, now: datetime) -> Alert | None:
    """Only when Dhan is in use (a client id and some credential: API key or manual token)."""
    if not settings.DHAN_CLIENT_ID or not (
        settings.DHAN_API_KEY or settings.DHAN_ACCESS_TOKEN or settings.DHAN_SESSION_TOKEN
    ):
        return None
    st = dhan_auth.status(settings, now)
    if st["source"] == "none":
        exp = st["session_expires_at"]
        when = f"expired {exp:%Y-%m-%d %H:%M} UTC" if exp else "no session"
        return Alert(
            "dhan_session",
            _fp("dhan-session-expired", str(exp)),
            "error",
            "Dhan session expired: trading data is stale",
            f"{when}. Log in to Dhan on /settings to start a new session.",
            "/dev/settings",
        )
    until = st["valid_until"]
    if st["expires_soon"] and until is not None:
        left = max(0, int((until - now).total_seconds() // 60))
        return Alert(
            "dhan_session",
            _fp("dhan-session-expiring", until.isoformat()),
            "warning",
            f"Dhan session expires in {left} min",
            f"valid until {until:%Y-%m-%d %H:%M} UTC ({st['source']}). Log in to Dhan on "
            "/settings to renew it.",
            "/dev/settings",
        )
    return None


class Dedup:
    """One send per fingerprint per window. Redis when available (shared, survives restarts),
    else an in-process map."""

    def __init__(self, redis: Redis | None, window_s: int) -> None:
        self.redis = redis
        self.window_s = window_s
        self._local: dict[str, float] = {}

    async def claim(self, fp: str) -> bool:
        if self.redis is not None:
            return bool(await self.redis.set(DEDUP_PREFIX + fp, "1", nx=True, ex=self.window_s))
        now = time.monotonic()
        if self._local.get(fp, 0.0) > now:
            return False
        self._local[fp] = now + self.window_s
        return True

    async def release(self, fp: str) -> None:
        if self.redis is not None:
            await self.redis.delete(DEDUP_PREFIX + fp)
        else:
            self._local.pop(fp, None)


async def _send(settings: Settings, alert: Mapping[str, Any]) -> bool:
    url = settings.discord_webhook_url
    if not url:
        return False
    return await discord.post(
        url,
        discord.build_alert_payload(alert, settings.PUBLIC_APP_URL),
        attempts=settings.DISCORD_MAX_ATTEMPTS,
        request_seconds=settings.DISCORD_REQUEST_SECONDS,
        retry_base_seconds=settings.DISCORD_RETRY_BASE_SECONDS,
    )


_LOCAL_DEDUP: dict[int, Dedup] = {}


async def dispatch(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis | None,
    *,
    now: datetime | None = None,
    sender: Sender = _send,
    dedup: Dedup | None = None,
) -> dict[str, Any]:
    if not settings.discord_webhook_url:
        return {"skipped": "discord not configured", "sent": 0}
    window = settings.ALERTS_DEDUP_MINUTES * 60
    if dedup is None:
        if redis is not None:
            dedup = Dedup(redis, window)
        else:  # keep the in-process map across runs of this process
            dedup = _LOCAL_DEDUP.setdefault(window, Dedup(None, window))
    alerts, info = await collect(maker, settings, now)
    sent, failed, deduped, capped = 0, 0, 0, 0
    kinds: dict[str, int] = {}
    for alert in alerts:
        if sent + failed >= settings.ALERTS_MAX_PER_RUN:
            capped += 1
            continue
        if not await dedup.claim(alert.fingerprint):
            deduped += 1
            continue
        try:
            ok = await sender(settings, asdict(alert))
        except Exception as exc:  # a notifier never fails the run on one alert
            log.warning("alert_send_failed", error=type(exc).__name__)
            ok = False
        if ok:
            sent += 1
            kinds[alert.kind] = kinds.get(alert.kind, 0) + 1
        else:
            failed += 1
            await dedup.release(alert.fingerprint)  # retried next run
    return {
        "candidates": len(alerts),
        "sent": sent,
        "failed": failed,
        "deduplicated": deduped,
        "capped": capped,
        "by_kind": kinds,
        **info,
    }
