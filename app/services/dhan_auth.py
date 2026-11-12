"""Dhan API key login (https://dhanhq.co/docs/v2/authentication/, "API key & secret").

1. generate-consent: POST {DHAN_AUTH_URL}/app/generate-consent?client_id=<DHAN_CLIENT_ID>
   with headers app_id=<DHAN_API_KEY>, app_secret=<DHAN_API_SECRET> -> consentAppId.
2. The admin's browser opens {DHAN_AUTH_URL}/login/consentApp-login?consentAppId=<id>, logs in
   on Dhan, and Dhan redirects it to DHAN_REDIRECT_URL with ?tokenId=<id>.
3. consumeApp-consent: GET {DHAN_AUTH_URL}/app/consumeApp-consent?tokenId=<id> with the same
   headers -> accessToken + expiryTime (IST, no offset) + dhanClientId.

The access token and its expiry are stored as the managed settings DHAN_SESSION_TOKEN /
DHAN_SESSION_EXPIRES_AT (core.app_settings, Fernet-encrypted) and every Dhan call prefers that
token while it is valid (Settings.dhan_access_token; DHAN_ACCESS_TOKEN is the manual fallback).
Nothing here places orders: it only obtains the read-only data session.

No Dhan response body is ever returned or logged (it holds the token); errors are codes.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.app_settings import AppSetting
from app.services import app_settings as svc

IST = ZoneInfo("Asia/Kolkata")
TOKEN_KEY = "DHAN_SESSION_TOKEN"  # noqa: S105 - a settings key name, not a secret
EXPIRES_KEY = "DHAN_SESSION_EXPIRES_AT"
SESSION_KEYS = (TOKEN_KEY, EXPIRES_KEY)
# Dhan's tokenId / consentAppId: opaque ids (UUID-like). Anything else is refused unread.
ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,256}$")
# The consent a browser must finish within (Dhan keeps it until a tokenId is generated).
CONSENT_TTL_S = 600


class DhanAuthError(Exception):
    """A refusal that is safe to show: a code and a sentence, never a Dhan body or secret."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


@dataclass(frozen=True, slots=True)
class Session:
    access_token: str
    expires_at: datetime  # UTC
    client_id: str
    client_name: str | None


def _creds(settings: Settings) -> tuple[str, str, str]:
    cid = (settings.DHAN_CLIENT_ID or "").strip()
    key = settings.DHAN_API_KEY.get_secret_value() if settings.DHAN_API_KEY else ""
    secret = settings.DHAN_API_SECRET.get_secret_value() if settings.DHAN_API_SECRET else ""
    if not (cid and key and secret):
        raise DhanAuthError(
            409,
            "dhan_api_key_missing",
            "set DHAN_CLIENT_ID, DHAN_API_KEY and DHAN_API_SECRET on /settings first",
        )
    return cid, key, secret


def _base(settings: Settings) -> str:
    return settings.DHAN_AUTH_URL.rstrip("/")


def parse_expiry(raw: Any) -> datetime:
    """Dhan's expiryTime ("2025-09-23T12:37:23", optionally with .fff; IST, no offset) as
    an aware UTC datetime. An explicit offset is honoured."""
    if not isinstance(raw, str) or not raw.strip():
        raise DhanAuthError(502, "dhan_bad_response", "Dhan did not return an expiry time")
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise DhanAuthError(502, "dhan_bad_response", "Dhan returned an unreadable expiry") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(UTC)


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise DhanAuthError(502, "dhan_bad_response", "Dhan returned an unexpected answer")
    return data


def _refused(resp: httpx.Response, step: str) -> DhanAuthError:
    if resp.status_code in (400, 401, 403):
        return DhanAuthError(
            502, "dhan_refused", f"Dhan refused the {step} (HTTP {resp.status_code})"
        )
    return DhanAuthError(502, "dhan_unavailable", f"Dhan {step} failed (HTTP {resp.status_code})")


async def generate_consent(settings: Settings, http: httpx.AsyncClient) -> str:
    """Step 1: a consentAppId (Dhan allows 25 a day)."""
    cid, key, secret = _creds(settings)
    try:
        resp = await http.post(
            f"{_base(settings)}/app/generate-consent",
            params={"client_id": cid},
            headers={"app_id": key, "app_secret": secret, "Accept": "application/json"},
            timeout=settings.DHAN_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DhanAuthError(502, "dhan_unavailable", "could not reach Dhan") from exc
    if resp.status_code != 200:
        raise _refused(resp, "consent request")
    data = _json(resp)
    consent = data.get("consentAppId")
    if not isinstance(consent, str) or not ID_RE.match(consent):
        raise DhanAuthError(502, "dhan_bad_response", "Dhan did not return a consent id")
    return consent


def totp(secret_b32: str, at: datetime | None = None, *, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP (HMAC-SHA1, 30 s, 6 digits): the code an authenticator app shows for
    `secret_b32`. Standard library only; the secret never leaves this function."""
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    counter = int((at or datetime.now(UTC)).timestamp()) // step
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**digits
    return str(code).zfill(digits)


def _session_from(data: dict[str, Any], cid: str) -> Session:
    """accessToken + expiryTime + dhanClientId, checked; the same shape for every flow."""
    token = data.get("accessToken")
    if not isinstance(token, str) or len(token) < 20 or len(token) > 8192:
        raise DhanAuthError(502, "dhan_bad_response", "Dhan did not return an access token")
    got = str(data.get("dhanClientId") or "").strip()
    if got != cid:
        raise DhanAuthError(
            403, "dhan_client_mismatch", "the Dhan login was for a different client id"
        )
    expires = parse_expiry(data.get("expiryTime"))
    name = data.get("dhanClientName")
    return Session(token, expires, got, name if isinstance(name, str) else None)


async def generate_with_totp(
    settings: Settings, http: httpx.AsyncClient, now: datetime | None = None
) -> Session:
    """Automatic login: POST {DHAN_AUTH_URL}/app/generateAccessToken with the client id, the
    PIN and the current TOTP code (needs TOTP enabled on the Dhan account). A refusal is
    `dhan_refused`; the caller must not retry the same PIN + secret after one."""
    cid = (settings.DHAN_CLIENT_ID or "").strip()
    pin = settings.DHAN_PIN.get_secret_value() if settings.DHAN_PIN else ""
    secret = settings.DHAN_TOTP_SECRET.get_secret_value() if settings.DHAN_TOTP_SECRET else ""
    if not (cid and pin and secret):
        raise DhanAuthError(
            409,
            "dhan_auto_login_not_configured",
            "set DHAN_CLIENT_ID, DHAN_PIN and DHAN_TOTP_SECRET on /settings first",
        )
    try:
        code = totp(secret, now)
    except (ValueError, binascii.Error) as exc:
        raise DhanAuthError(
            409, "dhan_totp_secret_invalid", "DHAN_TOTP_SECRET is not a base32 key"
        ) from exc
    try:
        resp = await http.post(
            f"{_base(settings)}/app/generateAccessToken",
            params={"dhanClientId": cid, "pin": pin, "totp": code},
            headers={"Accept": "application/json"},
            timeout=settings.DHAN_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DhanAuthError(502, "dhan_unavailable", "could not reach Dhan") from exc
    if resp.status_code != 200:
        raise _refused(resp, "automatic login")
    data = _json(resp)
    if not data.get("accessToken"):
        # Dhan answers some refusals (wrong PIN / TOTP) with 200 and an error body.
        raise DhanAuthError(502, "dhan_refused", "Dhan refused the automatic login")
    return _session_from(data, cid)


def login_url(settings: Settings, consent_id: str) -> str:
    """Step 2: where the admin's browser goes to log in on Dhan."""
    return f"{_base(settings)}/login/consentApp-login?" + urlencode({"consentAppId": consent_id})


async def consume_consent(settings: Settings, http: httpx.AsyncClient, token_id: str) -> Session:
    """Step 3: the access token for the tokenId Dhan redirected the browser with."""
    if not ID_RE.match(token_id):
        raise DhanAuthError(422, "bad_token_id", "the tokenId from Dhan is malformed")
    cid, key, secret = _creds(settings)
    try:
        resp = await http.get(
            f"{_base(settings)}/app/consumeApp-consent",
            params={"tokenId": token_id},
            headers={"app_id": key, "app_secret": secret, "Accept": "application/json"},
            timeout=settings.DHAN_HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise DhanAuthError(502, "dhan_unavailable", "could not reach Dhan") from exc
    if resp.status_code != 200:
        raise _refused(resp, "token exchange")
    # The session must be for the configured account: a tokenId from someone else's login
    # (e.g. a crafted callback link) yields a token for another client id and is refused.
    return _session_from(_json(resp), cid)


async def store_session(
    db: AsyncSession, cipher: svc.Cipher, session: Session, user_id: Any
) -> None:
    """Write the managed rows (encrypted like every setting) in the caller's transaction."""
    now = datetime.now(UTC)
    values = {TOKEN_KEY: session.access_token, EXPIRES_KEY: session.expires_at.isoformat()}
    for key, value in values.items():
        spec = svc.REGISTRY[key]
        row = await db.get(AppSetting, key, with_for_update=True)
        if row is None:
            row = AppSetting(key=key, version=0)
            db.add(row)
        row.value_encrypted = cipher.encrypt(value)
        row.is_secret = spec.secret
        row.category = spec.category
        row.updated_by = user_id
        row.updated_at = now
        row.version = (row.version or 0) + 1


async def clear_session(db: AsyncSession) -> int:
    """Drop the managed rows (log out); the manual DHAN_ACCESS_TOKEN applies again."""
    res = await db.execute(delete(AppSetting).where(AppSetting.key.in_(SESSION_KEYS)))
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


def status(settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    """What the UI and alerts need. No token, no secret."""
    now = now or datetime.now(UTC)
    exp = svc.as_utc(settings.DHAN_SESSION_EXPIRES_AT)
    has_session = settings.DHAN_SESSION_TOKEN is not None and exp is not None
    active = settings.dhan_session_active(now)
    manual_token = settings.DHAN_ACCESS_TOKEN
    manual = manual_token is not None
    manual_exp = (
        svc.dhan_token_expiry(manual_token.get_secret_value()) if manual_token is not None else None
    )
    if active:
        source, valid_until = "session", exp
    elif manual and (manual_exp is None or manual_exp > now):
        source, valid_until = "manual", manual_exp
    else:
        source, valid_until = "none", None
    alert_window = timedelta(minutes=settings.DHAN_SESSION_ALERT_MINUTES)
    return {
        "client_id_set": bool(settings.DHAN_CLIENT_ID),
        "api_key_configured": bool(settings.DHAN_API_KEY and settings.DHAN_API_SECRET),
        "auto_login_configured": settings.dhan_auto_login_configured,
        "redirect_url": settings.DHAN_REDIRECT_URL,
        "session_active": active,
        "session_expires_at": exp if has_session else None,
        "session_expired": has_session and not active,
        "source": source,
        "valid_until": valid_until,
        "expires_soon": bool(valid_until and valid_until - now <= alert_window),
        # Without a usable token every Dhan-derived number (feed, positions, funds) is stale.
        "data_stale": source == "none" and bool(settings.DHAN_CLIENT_ID),
    }
