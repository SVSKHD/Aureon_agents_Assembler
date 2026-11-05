"""Dhan credential verification and connection state (app/services/dhan_connection.py).

Dhan's Profile API is mocked with the documented shape (https://dhanhq.co/docs/v2/
authentication/, "User Profile"). What must hold:

- valid / invalid / expired / revoked (= refused) / mismatched / unreachable are told apart;
- an outage never downgrades a stored verdict, a new credential resets what we knew;
- nothing returned, logged or stored contains a token;
- an expired manual token is not used (docs/27 finding 4);
- broker credential, rate-limit and Redis errors map to clean API errors (finding 7).
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError

from app.agents.trading.base import CredentialsExpired
from app.agents.trading.ratelimit import RateLimitExceeded
from app.core.config import Settings
from app.models.brokers import DhanConnection
from app.services import dhan_connection as conn
from app.services.trading import call_adapter

CLIENT = "1100003626"
BASE = "https://api.dhan.test/v2"


def jwt(exp: datetime, tag: str = "A") -> str:
    def enc(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'HS512'})}.{enc({'exp': int(exp.timestamp()), 't': tag * 40})}.sig{tag}"


NOW = datetime.now(UTC)
TOKEN = jwt(NOW + timedelta(hours=20), "A")
TOKEN2 = jwt(NOW + timedelta(hours=20), "B")
EXPIRED = jwt(NOW - timedelta(hours=1), "C")


@pytest.fixture
def s(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "DHAN_CLIENT_ID": CLIENT,
            "DHAN_BASE_URL": BASE,
            "DHAN_ACCESS_TOKEN": SecretStr(TOKEN),
            "DHAN_HTTP_TIMEOUT": 2.0,
        }
    )


def profile(client_id: str = CLIENT) -> dict[str, Any]:
    return {
        "dhanClientId": client_id,
        "tokenValidity": (NOW + timedelta(hours=20, minutes=5, seconds=30)).strftime(
            "%d/%m/%Y %H:%M"
        ),
        "activeSegment": "Equity, Derivative, Currency, Commodity",
        "ddpi": "Active",
        "mtf": "Deactive",
        "dataPlan": "Active",
        "dataValidity": "2026-12-05 09:37:52.0",
    }


def transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def answer(status: int, body: Any) -> httpx.MockTransport:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        assert req.url.path == "/v2/profile" and req.method == "GET"
        return httpx.Response(status, json=body)

    t = transport(h)
    t.seen = seen  # type: ignore[attr-defined]
    return t


# --- verification outcomes -----------------------------------------------------------------


async def test_valid_credential_is_verified_with_profile_facts(s: Settings) -> None:
    t = answer(200, profile())
    v = await conn.verify_token(s, TOKEN, transport=t)
    assert v.outcome == "ok" and v.error_code is None and v.profile is not None
    assert v.profile.client_id == CLIENT and v.profile.data_plan == "Active"
    assert v.profile.active_segments == ["Equity", "Derivative", "Currency", "Commodity"]
    assert v.profile.token_valid_until is not None
    req = t.seen[0]  # type: ignore[attr-defined]
    assert req.headers["access-token"] == TOKEN and req.headers["client-id"] == CLIENT
    assert TOKEN not in repr(v)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"errorType": "Invalid_Authentication", "errorCode": "DH-901",
               "errorMessage": "Client ID or user generated access token is invalid or expired."}),
        (400, {"errorCode": "DH-901", "errorMessage": "token invalid"}),
        (403, {"errorCode": "808", "errorMessage": "Authentication Failed"}),
    ],
)  # fmt: skip
async def test_invalid_or_revoked_credential_is_rejected(
    s: Settings, status: int, body: dict[str, Any]
) -> None:
    v = await conn.verify_token(s, TOKEN, transport=answer(status, body))
    assert v.outcome == "rejected" and v.error_code and v.error_code.startswith("dhan_")
    assert "invalid or expired" not in (v.error_code or "")  # a code, never Dhan's text


async def test_profile_for_another_client_id_is_a_mismatch(s: Settings) -> None:
    v = await conn.verify_token(s, TOKEN, transport=answer(200, profile("9999999999")))
    assert v.outcome == "mismatch" and v.error_code == "dhan_client_mismatch"


@pytest.mark.parametrize("kind", ["timeout", "network", "503", "429", "not_json"])
async def test_dhan_unavailable_is_not_a_verdict(s: Settings, kind: str) -> None:
    def h(req: httpx.Request) -> httpx.Response:
        if kind == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        if kind == "network":
            raise httpx.ConnectError("down", request=req)
        if kind == "not_json":
            return httpx.Response(200, content=b"<html>maintenance</html>")
        return httpx.Response(int(kind), json={"errorCode": "DH-904"})

    v = await conn.verify_token(s, TOKEN, transport=transport(h))
    assert v.outcome == "unavailable", v


async def test_expired_credential_is_known_without_calling_dhan(s: Settings) -> None:
    expired = s.model_copy(update={"DHAN_ACCESS_TOKEN": SecretStr(EXPIRED)})

    def boom(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no Dhan call for a token that has certainly expired")

    v = await conn.verify_current(expired, transport=transport(boom))
    assert v.outcome == "expired" and v.error_code == "dhan_token_expired"


async def test_session_token_is_preferred_and_falls_back_after_expiry(s: Settings) -> None:
    with_session = s.model_copy(
        update={
            "DHAN_SESSION_TOKEN": SecretStr(TOKEN2),
            "DHAN_SESSION_EXPIRES_AT": NOW + timedelta(hours=2),
        }
    )
    sent: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        sent.append(req.headers["access-token"])
        return httpx.Response(200, json=profile())

    v = await conn.verify_current(with_session, transport=transport(h))
    assert v.source == "session" and sent == [TOKEN2]
    later = NOW + timedelta(hours=3)
    v = await conn.verify_current(with_session, transport=transport(h), now=later)
    assert v.source == "manual" and sent[-1] == TOKEN


def test_expired_manual_token_is_not_used(s: Settings) -> None:
    """docs/27 finding 4: an expired manual token used to keep every job alive and failing."""
    expired = s.model_copy(update={"DHAN_ACCESS_TOKEN": SecretStr(EXPIRED)})
    assert expired.dhan_access_token() is None and not expired.dhan_configured
    assert s.dhan_access_token() is not None  # a valid one still is
    opaque = s.model_copy(update={"DHAN_ACCESS_TOKEN": SecretStr("not-a-jwt-" + "x" * 30)})
    assert opaque.dhan_access_token() is not None  # no readable exp: Dhan decides


# --- persisted state -------------------------------------------------------------------------


class FakeDb:
    """Just enough AsyncSession for record()/mark_*: one table keyed by client id."""

    def __init__(self) -> None:
        self.rows: dict[str, DhanConnection] = {}

    async def get(self, model: Any, key: str, with_for_update: bool = False) -> Any:
        return self.rows.get(key)

    def add(self, row: DhanConnection) -> None:
        self.rows[row.dhan_client_id] = row

    async def flush(self) -> None:
        return None


def v_ok(s: Settings, token: str = TOKEN) -> conn.Verification:
    facts = conn.ProfileFacts(CLIENT, NOW + timedelta(hours=20), "Active", "x", ["Equity"])
    return conn.Verification("ok", None, facts, "manual", conn.credential_fp(s, token))


async def test_record_transitions_and_outage_never_downgrades(s: Settings) -> None:
    db: Any = FakeDb()
    rec = await conn.record(db, s, v_ok(s))
    assert rec is not None and rec.row.status == "connected" and rec.changed
    assert rec.row.last_verified_at is not None and rec.row.data_plan == "Active"
    fp = conn.credential_fp(s, TOKEN)
    # Dhan down three times: still "connected", failures counted, error code kept.
    for i in range(1, 4):
        out = conn.Verification("unavailable", "dhan_timeout", None, "manual", fp)
        rec = await conn.record(db, s, out)
        assert rec is not None and rec.row.status == "connected"
        assert rec.row.consecutive_failures == i and rec.row.last_error_code == "dhan_timeout"
    # Dhan refuses it now (revoked): rejected.
    rec = await conn.record(
        db, s, conn.Verification("rejected", "dhan_token_rejected", None, "manual", fp)
    )
    assert rec is not None and rec.row.status == "rejected" and rec.changed
    assert rec.row.version > 1


async def test_credential_replacement_resets_and_is_detected(s: Settings) -> None:
    db: Any = FakeDb()
    fp1 = conn.credential_fp(s, TOKEN)
    await conn.record(
        db, s, conn.Verification("rejected", "dhan_token_rejected", None, "manual", fp1)
    )
    # The admin pastes a new manual token, Dhan is briefly unreachable: "unverified", not
    # "rejected" (the old verdict was about the old token).
    fp2 = conn.credential_fp(s, TOKEN2)
    rec = await conn.record(
        db, s, conn.Verification("unavailable", "dhan_network", None, "manual", fp2)
    )
    assert rec is not None and rec.replaced and rec.row.status == "unverified"
    assert rec.row.consecutive_failures == 1 and rec.row.credential_fp == fp2
    rec = await conn.record(db, s, v_ok(s, TOKEN2))
    assert rec is not None and rec.row.status == "connected" and not rec.replaced


async def test_login_logout_and_relogin_keep_the_row(s: Settings) -> None:
    db: Any = FakeDb()
    no_manual = s.model_copy(update={"DHAN_ACCESS_TOKEN": None})
    await conn.mark_login(db, no_manual, v_ok(s), NOW + timedelta(hours=24), None)
    row = db.rows[CLIENT]
    assert row.status == "connected" and row.token_source == "session" and row.connected_at
    out = await conn.mark_logout(db, no_manual, None)
    assert out is row and row.status == "disconnected" and row.credential_fp is None
    assert row.disconnected_at is not None and row.token_source == "none"
    # With a manual token in place, logging out leaves "unverified" (it gets checked next).
    out = await conn.mark_logout(db, s, None)
    assert out is not None and out.status == "unverified" and out.token_source == "manual"
    # A later login clears the logout.
    await conn.mark_login(db, no_manual, v_ok(s), NOW + timedelta(hours=24), None)
    assert row.status == "connected" and row.disconnected_at is None
    assert len(db.rows) == 1  # one row per client id, never deleted


async def test_no_token_after_logout_is_disconnected(s: Settings) -> None:
    db: Any = FakeDb()
    none = s.model_copy(update={"DHAN_ACCESS_TOKEN": None})
    v = await conn.verify_current(none)
    assert v.outcome == "no_token"
    rec = await conn.record(db, none, v)
    assert rec is not None and rec.row.status == "not_configured"
    await conn.mark_logout(db, none, None)
    rec = await conn.record(db, none, v)
    assert rec is not None and rec.row.status == "disconnected"


def test_view_is_safe(s: Settings) -> None:
    row = DhanConnection(
        dhan_client_id=CLIENT,
        status="connected",
        token_source="manual",
        credential_fp=conn.credential_fp(s, TOKEN),
        active_segments=["Equity"],
        consecutive_failures=0,
        version=3,
    )
    out = conn.view(row, s)
    text = json.dumps(out, default=str)
    assert row.credential_fp and row.credential_fp not in text
    assert TOKEN not in text and CLIENT not in text
    assert out["client_id"] == "••••"  # masked like every short secret setting
    assert out["renewal"] == "manual" and out["action"] is None
    assert conn.view(None, s)["status"] == "unverified"
    assert conn.view(None, s.model_copy(update={"DHAN_CLIENT_ID": None}))["status"] == (
        "not_configured"
    )


def test_fingerprint_is_keyed_and_short(s: Settings) -> None:
    fp = conn.credential_fp(s, TOKEN)
    assert len(fp) == 32 and TOKEN[:16] not in fp
    other = s.model_copy(update={"SECRET_KEY": SecretStr("u" * 40)})
    assert conn.credential_fp(other, TOKEN) != fp  # HMAC, not a bare hash


# --- structured broker errors (docs/27 finding 7) -------------------------------------------


async def _raise(exc: BaseException) -> None:
    raise exc


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (CredentialsExpired("dhan token refused"), 409, "broker_credentials_invalid"),
        (RateLimitExceeded("quote", 2.2), 429, "broker_rate_limited"),
        (RedisConnectionError("down"), 503, "unavailable"),
    ],
)
async def test_call_adapter_maps_broker_errors(
    settings: Settings, exc: BaseException, status: int, code: str
) -> None:
    with pytest.raises(HTTPException) as info:
        await call_adapter(_raise(exc), settings)
    assert info.value.status_code == status
    assert info.value.detail["code"] == code  # type: ignore[index]
    if status == 429:
        assert info.value.headers == {"Retry-After": "3"}
