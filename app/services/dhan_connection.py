"""Dhan connection state: verify the credential in use and keep SAFE metadata in Postgres.

Verification uses Dhan's supported Profile API (GET {DHAN_BASE_URL}/profile, header
`access-token`; https://dhanhq.co/docs/v2/authentication/). It answers `dhanClientId`,
`tokenValidity`, `activeSegment`, `dataPlan` and `dataValidity`. From that:

| outcome        | when                                                        | stored status |
|----------------|-------------------------------------------------------------|---------------|
| ok             | Dhan answered for the configured client id                  | connected     |
| mismatch       | Dhan answered for ANOTHER client id                         | mismatch      |
| rejected       | Dhan refused the token (401, DH-901, 807-810): invalid,     | rejected      |
|                | revoked or no longer accepted. Dhan does not tell these apart |             |
| expired        | the token's own expiry has passed (checked locally, no call) | expired       |
| unavailable    | timeout, network error, 429/5xx, unreadable answer          | (unchanged)   |
| no_token       | a client id but no usable token                             | disconnected* |
| not_configured | no DHAN_CLIENT_ID                                           | not_configured |

(*) `not_configured` when no admin ever logged out, i.e. there never was a session.

`unavailable` never downgrades a verdict: an outage says nothing about the credential, so
the previous status is kept and `consecutive_failures` counts up. A credential seen for the
first time during an outage is `unverified`.

Renewal is manual unless automatic login is configured. With DHAN_PIN + DHAN_TOTP_SECRET
set, the scheduler logs in again before the session expires (app/services/dhan_auto_login.py;
`renewal: auto`). Otherwise, or after Dhan refused the PIN + TOTP, an expired or rejected
credential needs a person: "Log in to Dhan" on /settings, or a new manual token. Dhan's
RenewToken is not used: it only applies to tokens generated on Dhan Web.

No token, secret or Dhan body is ever returned, logged or stored here. `credential_fp` is an
HMAC of the token keyed by SECRET_KEY: enough to notice a replaced credential, useless to
anyone who reads it, and never part of an API response.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.trading.base import CredentialsExpired
from app.agents.trading.dhan import parse
from app.agents.trading.dhan.client import DhanApiError, DhanClient
from app.core.config import Settings, jwt_expiry
from app.core.logging import get_logger
from app.models.brokers import DhanConnection
from app.services import app_settings as svc

log = get_logger("services.dhan_connection")

Outcome = Literal[
    "ok", "rejected", "mismatch", "unavailable", "expired", "no_token", "not_configured"
]
UNAVAILABLE_CODES = {"timeout", "network", "DH-904", "805", "DH-908", "DH-909"}

# What a person has to do for each status (shown by the API and in incidents).
ACTIONS: dict[str, str | None] = {
    "connected": None,
    "unverified": "wait for the next check, or press Verify on /settings",
    "expired": "log in to Dhan on /settings (or set a new DHAN_ACCESS_TOKEN)",
    "rejected": "log in to Dhan on /settings: Dhan no longer accepts this token",
    "mismatch": "the token belongs to another Dhan account: check DHAN_CLIENT_ID, log in again",
    "disconnected": "log in to Dhan on /settings",
    "not_configured": "set DHAN_CLIENT_ID and log in to Dhan on /settings",
}
# Statuses where the credential itself is unusable (monitoring raises an auth incident).
UNUSABLE = frozenset({"expired", "rejected", "mismatch", "disconnected"})


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    client_id: str
    token_valid_until: datetime | None
    data_plan: str | None
    data_valid_until: str | None
    active_segments: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Verification:
    outcome: Outcome
    error_code: str | None = None
    profile: ProfileFacts | None = None
    source: str = "none"  # session | manual | none
    credential_fp: str | None = None


def credential_fp(settings: Settings, token: str) -> str:
    key = settings.SECRET_KEY.get_secret_value().encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()[:32]


def _facts(profile: dict[str, Any]) -> ProfileFacts:
    segs = profile.get("activeSegment")
    segments = [s.strip() for s in segs.split(",") if s.strip()] if isinstance(segs, str) else []
    plan, valid = profile.get("dataPlan"), profile.get("dataValidity")
    return ProfileFacts(
        client_id=str(profile.get("dhanClientId") or "").strip(),
        token_valid_until=parse.token_expiry(profile),
        data_plan=str(plan)[:32] if plan is not None else None,
        data_valid_until=str(valid)[:40] if valid is not None else None,
        active_segments=segments[:20],
    )


async def verify_token(
    settings: Settings,
    token: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Verification:
    """Ask Dhan's Profile API who this token belongs to. Never raises for Dhan's answers."""
    cid = (settings.DHAN_CLIENT_ID or "").strip()
    fp = credential_fp(settings, token)
    if not cid:
        return Verification("not_configured", "dhan_client_id_missing", credential_fp=fp)
    client = DhanClient(
        cid,
        token,
        base_url=settings.DHAN_BASE_URL,
        limiter=None,
        timeout=settings.DHAN_HTTP_TIMEOUT,
        transport=transport,
    )
    try:
        profile = await client.profile()
    except CredentialsExpired:
        return Verification("rejected", "dhan_token_rejected", credential_fp=fp)
    except DhanApiError as exc:
        status = exc.http_status or 0
        if exc.code in UNAVAILABLE_CODES or status == 429 or status >= 500:
            return Verification("unavailable", f"dhan_{exc.code or status}"[:64], credential_fp=fp)
        return Verification("rejected", f"dhan_refused_{status or exc.code}"[:64], credential_fp=fp)
    except ValueError:  # not JSON
        return Verification("unavailable", "dhan_bad_response", credential_fp=fp)
    finally:
        await client.aclose()
    if not isinstance(profile, dict):
        return Verification("unavailable", "dhan_bad_response", credential_fp=fp)
    facts = _facts(profile)
    if facts.client_id != cid:
        # Never echo the other client id: it identifies someone else's account.
        return Verification("mismatch", "dhan_client_mismatch", facts, credential_fp=fp)
    return Verification("ok", None, facts, credential_fp=fp)


def token_in_effect(settings: Settings, now: datetime) -> tuple[str, str | None]:
    """(source, token): the login session's token while valid, else a non-expired manual one."""
    if settings.dhan_session_active(now):
        tok = settings.DHAN_SESSION_TOKEN
        return "session", tok.get_secret_value() if tok else None
    token = settings.dhan_access_token(now)
    return ("manual", token.get_secret_value()) if token is not None else ("none", None)


def _expired_credential(settings: Settings, now: datetime) -> bool:
    """A credential exists but its own expiry has passed (no Dhan call needed)."""
    exp = svc.as_utc(settings.DHAN_SESSION_EXPIRES_AT)
    session_expired = settings.DHAN_SESSION_TOKEN is not None and exp is not None and exp <= now
    manual = settings.DHAN_ACCESS_TOKEN
    manual_exp = jwt_expiry(manual.get_secret_value()) if manual is not None else None
    manual_expired = manual_exp is not None and manual_exp <= now
    return session_expired or manual_expired


async def verify_current(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
) -> Verification:
    """Verify whatever credential every Dhan call would use right now."""
    now = now or datetime.now(UTC)
    if not (settings.DHAN_CLIENT_ID or "").strip():
        return Verification("not_configured", "dhan_client_id_missing")
    source, token = token_in_effect(settings, now)
    if token is None:
        if _expired_credential(settings, now):
            return Verification("expired", "dhan_token_expired")
        return Verification("no_token", "dhan_no_token")
    v = await verify_token(settings, token, transport=transport)
    return Verification(v.outcome, v.error_code, v.profile, source, v.credential_fp)


# --- persistence ----------------------------------------------------------------------------


_STATUS_FOR: dict[str, str] = {
    "ok": "connected",
    "rejected": "rejected",
    "mismatch": "mismatch",
    "expired": "expired",
    "not_configured": "not_configured",
}


@dataclass(frozen=True, slots=True)
class Recorded:
    row: DhanConnection
    previous_status: str | None
    replaced: bool  # a different credential than the last one seen

    @property
    def changed(self) -> bool:
        return self.previous_status != self.row.status


async def record(
    db: AsyncSession,
    settings: Settings,
    v: Verification,
    *,
    now: datetime | None = None,
) -> Recorded | None:
    """Store a check's result (caller's transaction). None when there is no client id."""
    now = now or datetime.now(UTC)
    cid = (settings.DHAN_CLIENT_ID or "").strip()
    if not cid:
        return None
    row = await db.get(DhanConnection, cid, with_for_update=True)
    previous = row.status if row is not None else None
    if row is None:
        row = DhanConnection(
            dhan_client_id=cid,
            status="unverified",
            token_source="none",  # noqa: S106 - a source name, not a secret
            active_segments=[],
            consecutive_failures=0,
            version=1,
        )
        db.add(row)
    replaced = v.credential_fp is not None and v.credential_fp != row.credential_fp
    if replaced:
        # A new credential: what we knew about the old one no longer applies.
        row.credential_fp = v.credential_fp
        row.consecutive_failures = 0
        row.last_verified_at = None
        row.last_error_code = None
    row.token_source = v.source
    if v.outcome == "unavailable":
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        if replaced or previous is None or previous in ("not_configured", "disconnected"):
            row.status = "unverified"
    elif v.outcome == "no_token":
        row.consecutive_failures = 0
        row.status = "disconnected" if row.disconnected_at is not None else "not_configured"
        row.credential_fp = None
    else:
        row.status = _STATUS_FOR[v.outcome]
        row.consecutive_failures = 0 if v.outcome == "ok" else (row.consecutive_failures or 0) + 1
        if v.outcome == "expired":
            row.credential_fp = None
    if v.outcome == "ok" and v.profile is not None:
        row.last_verified_at = now
        row.data_plan = v.profile.data_plan
        row.data_valid_until = v.profile.data_valid_until
        row.active_segments = list(v.profile.active_segments)
        if v.profile.token_valid_until is not None:
            row.expires_at = v.profile.token_valid_until
    if v.source == "session":
        row.expires_at = svc.as_utc(settings.DHAN_SESSION_EXPIRES_AT) or row.expires_at
    elif v.source == "manual":
        tok = settings.DHAN_ACCESS_TOKEN
        row.expires_at = (jwt_expiry(tok.get_secret_value()) if tok else None) or row.expires_at
    row.last_check_at = now
    row.last_check_outcome = v.outcome
    row.last_error_code = v.error_code
    if previous is not None and (previous != row.status or replaced):
        row.version = (row.version or 0) + 1
    row.updated_at = now
    await db.flush()
    if previous != row.status:
        log.info("dhan_connection_status", status=row.status, previous=previous, outcome=v.outcome)
    return Recorded(row, previous, replaced)


async def mark_login(
    db: AsyncSession,
    settings: Settings,
    v: Verification,
    expires_at: datetime,
    user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> Recorded | None:
    """A completed "Log in to Dhan" (the token is already stored by the caller)."""
    now = now or datetime.now(UTC)
    rec = await record(
        db,
        settings,
        Verification(v.outcome, v.error_code, v.profile, "session", v.credential_fp),
        now=now,
    )
    if rec is None:
        return None
    rec.row.connected_at, rec.row.connected_by = now, user_id
    rec.row.disconnected_at = rec.row.disconnected_by = None
    rec.row.expires_at = expires_at
    if rec.row.status == "disconnected":  # record() saw the old logout; this is a new login
        rec.row.status = "connected" if v.outcome == "ok" else "unverified"
    await db.flush()
    return rec


async def mark_logout(
    db: AsyncSession,
    settings: Settings,
    user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> DhanConnection | None:
    """An admin logged out. The row stays (history, broker accounts untouched). With a usable
    manual token the connection becomes `unverified` until the next check."""
    now = now or datetime.now(UTC)
    cid = (settings.DHAN_CLIENT_ID or "").strip()
    if not cid:
        return None
    row = await db.get(DhanConnection, cid, with_for_update=True)
    if row is None:
        row = DhanConnection(
            dhan_client_id=cid,
            active_segments=[],
            consecutive_failures=0,
            version=1,
            status="disconnected",
        )
        db.add(row)
    manual = settings.dhan_access_token(now) if not settings.dhan_session_active(now) else None
    previous = row.status
    row.disconnected_at, row.disconnected_by = now, user_id
    if manual is not None:
        row.status, row.token_source = "unverified", "manual"
        row.credential_fp = credential_fp(settings, manual.get_secret_value())
    else:
        row.status, row.token_source, row.credential_fp = "disconnected", "none", None
    row.last_verified_at = None
    row.consecutive_failures = 0
    if previous != row.status:
        row.version = (row.version or 0) + 1
    row.updated_at = now
    await db.flush()
    return row


def view(row: DhanConnection | None, settings: Settings) -> dict[str, Any]:
    """The API's view of the connection: safe fields only (no token, no fingerprint, the
    client id masked like every other secret setting)."""
    cid = settings.DHAN_CLIENT_ID
    if row is None:
        status = "not_configured" if not cid else "unverified"
        return {
            "client_id": svc.mask(cid, True) if cid else None,
            "status": status,
            "token_source": "none",
            "expires_at": None,
            "connected_at": None,
            "disconnected_at": None,
            "data_plan": None,
            "data_valid_until": None,
            "active_segments": [],
            "last_verified_at": None,
            "last_check_at": None,
            "last_check_outcome": None,
            "last_error_code": None,
            "consecutive_failures": 0,
            "renewal": "auto" if settings.dhan_auto_login_configured else "manual",
            "action": ACTIONS[status],
            "version": 0,
        }
    return {
        "client_id": svc.mask(row.dhan_client_id, True),
        "status": row.status,
        "token_source": row.token_source,
        "expires_at": row.expires_at,
        "connected_at": row.connected_at,
        "disconnected_at": row.disconnected_at,
        "data_plan": row.data_plan,
        "data_valid_until": row.data_valid_until,
        "active_segments": list(row.active_segments or []),
        "last_verified_at": row.last_verified_at,
        "last_check_at": row.last_check_at,
        "last_check_outcome": row.last_check_outcome,
        "last_error_code": row.last_error_code,
        "consecutive_failures": row.consecutive_failures,
        "renewal": "auto" if settings.dhan_auto_login_configured else "manual",
        "action": ACTIONS.get(row.status),
        "version": row.version,
    }
