"""Automatic Dhan login with PIN + TOTP (docs/11-dhan.md, "Automatic login").

The scheduler's `dhan-auto-login` job calls `run()` every DHAN_AUTO_LOGIN_CRON. It logs in
only when needed, i.e. no session token is active or the active one expires within
DHAN_AUTO_LOGIN_BEFORE_MINUTES, so a 24 h token is replaced about once a day.

Each login goes through the same checks as "Log in to Dhan":

1. generateAccessToken with the client id, the PIN and the current TOTP code
   (dhan_auth.generate_with_totp);
2. Dhan's Profile API must confirm the token belongs to DHAN_CLIENT_ID. A rejected or
   foreign token is never stored;
3. the token is stored encrypted as DHAN_SESSION_TOKEN / DHAN_SESSION_EXPIRES_AT, the
   connection is marked, the event is audited, and the change is published so every
   process picks the new token up.

Safety:

- **No retry on a refusal.** If Dhan refuses the PIN + TOTP, the job remembers a
  fingerprint of that pair (an HMAC keyed by SECRET_KEY) and does not try it again until
  the PIN or the secret changes. Repeated wrong logins could lock the Dhan account.
- **A logout sticks.** Logging out of Dhan pauses automatic login until someone logs in to
  Dhan again (that login also clears a remembered refusal).
- **One login at a time** (a Redis lock), and no new login within MIN_GAP of the last
  success, even if Dhan returned a short expiry.
- The PIN, the TOTP secret, the code and the token are never logged, returned or audited.
  The last outcome (time, outcome, code, expiry) is kept in Redis for the UI.

Nothing here places orders: the token is the same read-only data session the login gives.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.services import app_settings as svc
from app.services import audit
from app.services import dhan_auth as dhan
from app.services import dhan_connection as conn
from app.services.audit import ClientInfo

log = get_logger("services.dhan_auto_login")

CLIENT = ClientInfo(user_agent="aureon-dhan-auto-login")
LOCK_KEY = "dhan:auto_login:lock"
REFUSED_KEY = "dhan:auto_login:refused"  # fingerprint of a refused PIN + secret
LAST_KEY = "dhan:auto_login:last"  # {at, outcome, code, expires_at}
PAUSED_KEY = "dhan:auto_login:paused"  # set by a logout, cleared by a login
LOCK_TTL_S = 120
LAST_TTL_S = 14 * 24 * 3600
MIN_GAP = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Result:
    outcome: str  # logged_in | not_needed | not_configured | paused | blocked | busy | failed
    code: str | None = None
    expires_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "code": self.code,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def fingerprint(settings: Settings) -> str:
    """HMAC of the PIN + TOTP secret under SECRET_KEY: notices a change, reveals nothing."""
    pin = settings.DHAN_PIN.get_secret_value() if settings.DHAN_PIN else ""
    secret = settings.DHAN_TOTP_SECRET.get_secret_value() if settings.DHAN_TOTP_SECRET else ""
    key = settings.SECRET_KEY.get_secret_value().encode()
    return hmac.new(key, f"{pin}\0{secret}".encode(), hashlib.sha256).hexdigest()[:32]


def needs_login(settings: Settings, now: datetime) -> bool:
    if not settings.dhan_session_active(now):
        return True
    exp = svc.as_utc(settings.DHAN_SESSION_EXPIRES_AT)
    before = timedelta(minutes=settings.DHAN_AUTO_LOGIN_BEFORE_MINUTES)
    return exp is None or exp - now <= before


async def last(redis: Redis) -> dict[str, Any] | None:
    """The last attempt, for the UI. Never raises."""
    try:
        raw = await redis.get(LAST_KEY)
        data = json.loads(raw) if raw else None
    except Exception:
        return None
    return data if isinstance(data, dict) else None


async def pause(redis: Redis) -> None:
    """An admin logged out of Dhan: do not log back in until someone logs in again."""
    try:
        await redis.set(PAUSED_KEY, "1")
    except Exception as exc:
        log.warning("dhan_auto_login_pause_failed", error=type(exc).__name__)


async def resume(redis: Redis) -> None:
    try:
        await redis.delete(PAUSED_KEY, REFUSED_KEY)
    except Exception as exc:
        log.warning("dhan_auto_login_resume_failed", error=type(exc).__name__)


async def _remember(redis: Redis, res: Result, now: datetime) -> None:
    try:
        await redis.set(LAST_KEY, json.dumps({"at": now.isoformat(), **res.as_dict()}),
                        ex=LAST_TTL_S)  # fmt: skip
    except Exception as exc:
        log.warning("dhan_auto_login_not_remembered", error=type(exc).__name__)


async def _recent_success(redis: Redis, now: datetime) -> bool:
    prev = await last(redis)
    if not prev or prev.get("outcome") != "logged_in":
        return False
    try:
        at = datetime.fromisoformat(str(prev["at"]))
    except (KeyError, ValueError):
        return False
    return now - at < MIN_GAP


async def _audit(
    maker: async_sessionmaker[AsyncSession], event: str, details: dict[str, Any]
) -> None:
    async with maker() as db, db.begin():
        audit.record(db, event, CLIENT, details=details)


async def run(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    http: httpx.AsyncClient,
    *,
    now: datetime | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Result:
    now = now or datetime.now(UTC)
    if not settings.dhan_auto_login_configured:
        return Result("not_configured")
    if await redis.get(PAUSED_KEY) is not None:
        return Result("paused", "logged_out")
    if not needs_login(settings, now):
        return Result("not_needed", expires_at=svc.as_utc(settings.DHAN_SESSION_EXPIRES_AT))
    if svc.cipher_for(settings) is None:
        res = Result("failed", "settings_encryption_unavailable")
        await _remember(redis, res, now)
        return res
    fp = fingerprint(settings)
    refused = await redis.get(REFUSED_KEY)
    if refused is not None and (refused.decode() if isinstance(refused, bytes) else refused) == fp:
        # Already refused: wait for a new PIN or secret instead of risking a lockout.
        return Result("blocked", "dhan_refused")
    if await _recent_success(redis, now):
        return Result("not_needed", "recent_login")
    if not await redis.set(LOCK_KEY, "1", nx=True, ex=LOCK_TTL_S):
        return Result("busy")
    try:
        res = await _login(maker, settings, redis, http, fp, now, transport)
    finally:
        await redis.delete(LOCK_KEY)
    await _remember(redis, res, now)
    return res


async def _login(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    http: httpx.AsyncClient,
    fp: str,
    now: datetime,
    transport: httpx.AsyncBaseTransport | None,
) -> Result:
    try:
        session = await dhan.generate_with_totp(settings, http, now)
    except dhan.DhanAuthError as exc:
        if exc.code in ("dhan_refused", "dhan_client_mismatch"):
            await redis.set(REFUSED_KEY, fp)  # no TTL: only a new PIN / secret clears it
        log.warning("dhan_auto_login_failed", code=exc.code, step="generate")
        await _audit(maker, "dhan_auto_login_failed", {"code": exc.code, "step": "generate"})
        return Result("failed", exc.code)

    verified = await conn.verify_token(settings, session.access_token, transport=transport)
    if verified.outcome in ("rejected", "mismatch", "not_configured"):
        code = "dhan_client_mismatch" if verified.outcome == "mismatch" else "dhan_token_rejected"
        log.warning("dhan_auto_login_failed", code=code, step="profile")
        await _audit(maker, "dhan_auto_login_failed", {"code": code, "step": "profile"})
        return Result("failed", code)

    cipher = svc.cipher_for(settings)
    assert cipher is not None  # checked by run()
    async with maker() as db, db.begin():
        await dhan.store_session(db, cipher, session, None)
        await conn.mark_login(db, settings, verified, session.expires_at, None, now=now)
        audit.record(
            db,
            "dhan_auto_login_completed",
            CLIENT,
            details={
                "expires_at": session.expires_at.isoformat(),
                "verification": verified.outcome,
                **({"error_code": verified.error_code} if verified.error_code else {}),
            },
        )
    await redis.delete(REFUSED_KEY)
    # Every process (API, feed, scheduler, research) reloads and uses the new token.
    await svc.publish(redis, list(dhan.SESSION_KEYS))
    log.info("dhan_auto_login_completed", expires_at=session.expires_at.isoformat())
    return Result("logged_in", expires_at=session.expires_at)


__all__ = [
    "LAST_KEY",
    "PAUSED_KEY",
    "REFUSED_KEY",
    "Result",
    "fingerprint",
    "last",
    "needs_login",
    "pause",
    "resume",
    "run",
]
