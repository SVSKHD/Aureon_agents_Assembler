"""Automatic Dhan login with PIN + TOTP: pure parts (no DB, no network, no real credentials).
Dhan is an httpx.MockTransport; Redis is tests.fake_redis.FakeRedis."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core.config import Settings
from app.services import app_settings as svc
from app.services import dhan_auth, dhan_auto_login
from app.services import dhan_connection as conn
from app.workers.scheduler import default_jobs
from tests.fake_redis import FakeRedis

NOW = datetime(2026, 11, 12, 2, 0, tzinfo=UTC)
CID = "1100003626"
PIN = "123456"
# RFC 6238 test key ("12345678901234567890") in base32.
SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
TOKEN = "eyJhbGciOiJIUzUxMiJ9." + "T" * 60 + ".sig"


def _s(settings: Settings, **kw: object) -> Settings:
    base: dict[str, object] = {
        "DHAN_CLIENT_ID": CID,
        "DHAN_PIN": SecretStr(PIN),
        "DHAN_TOTP_SECRET": SecretStr(SECRET),
        "SETTINGS_MASTER_KEY": SecretStr(Fernet.generate_key().decode()),
    }
    return settings.model_copy(update={**base, **kw})


def _dhan(answer: httpx.Response, seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return answer

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok(cid: str = CID, hours: int = 24) -> httpx.Response:
    exp = (NOW + timedelta(hours=hours)).astimezone(dhan_auth.IST).replace(tzinfo=None)
    return httpx.Response(
        200,
        json={
            "dhanClientId": cid,
            "dhanClientName": "Test",
            "accessToken": TOKEN,
            "expiryTime": exp.isoformat(),
        },
    )


class _Db:
    def __init__(self, added: list[Any]) -> None:
        self.added = added

    def add(self, row: Any) -> None:
        self.added.append(row)

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield


def _maker(added: list[Any]) -> Any:
    @contextlib.asynccontextmanager
    async def maker() -> AsyncIterator[_Db]:
        yield _Db(added)

    return maker


def _events(added: list[Any]) -> list[str]:
    return [getattr(r, "event_type", "") for r in added]


# --- TOTP ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ts", "code"),
    [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")],
)
def test_totp_matches_rfc_6238_vectors(ts: int, code: str) -> None:
    assert dhan_auth.totp(SECRET, datetime.fromtimestamp(ts, UTC)) == code


def test_totp_accepts_lowercase_and_unpadded_keys() -> None:
    at = datetime.fromtimestamp(59, UTC)
    assert dhan_auth.totp(SECRET.lower(), at) == "287082"
    short = "GEZDGNBVGY3TQOJQGE"  # 18 chars: needs padding to decode
    assert len(dhan_auth.totp(short, at)) == 6


# --- settings -----------------------------------------------------------------------------


def test_pin_and_secret_are_validated_and_normalised(settings: Settings) -> None:
    s = svc.overlay(settings, {"DHAN_PIN": " 123456 ", "DHAN_TOTP_SECRET": "gezd gnbv-gy3t qojq"})
    assert s.DHAN_PIN is not None and s.DHAN_PIN.get_secret_value() == "123456"
    assert s.DHAN_TOTP_SECRET is not None
    assert s.DHAN_TOTP_SECRET.get_secret_value() == "GEZDGNBVGY3TQOJQ"
    for key, bad in (("DHAN_PIN", "12ab56"), ("DHAN_TOTP_SECRET", "not base32 at all 0189")):
        # Saving from /settings goes through checked_overlay: field names only, never values.
        with pytest.raises(svc.SettingsError) as err:
            svc.checked_overlay(settings, {key: bad}, key)
        assert key in str(err.value) and bad not in str(err.value)


def test_keys_are_registered_as_secrets_and_scrubbed(settings: Settings) -> None:
    for key in ("DHAN_PIN", "DHAN_TOTP_SECRET"):
        assert svc.REGISTRY[key].secret and svc.REGISTRY[key].reauth
        assert not svc.REGISTRY[key].managed
    assert {PIN, SECRET} <= set(svc.secret_values(_s(settings)))


def test_configured_only_with_client_pin_secret_and_cron(settings: Settings) -> None:
    assert _s(settings).dhan_auto_login_configured
    assert not _s(settings, DHAN_PIN=None).dhan_auto_login_configured
    assert not _s(settings, DHAN_TOTP_SECRET=None).dhan_auto_login_configured
    assert not _s(settings, DHAN_AUTO_LOGIN_CRON=" ").dhan_auto_login_configured
    assert not _s(settings, DHAN_CLIENT_ID=None).dhan_auto_login_configured


def test_job_is_scheduled_only_when_configured(settings: Settings) -> None:
    assert "dhan-auto-login" in {j[0] for j in default_jobs(_s(settings))}
    assert "dhan-auto-login" not in {j[0] for j in default_jobs(_s(settings, DHAN_PIN=None))}


def test_status_and_connection_say_auto(settings: Settings) -> None:
    assert dhan_auth.status(_s(settings), NOW)["auto_login_configured"] is True
    assert conn.view(None, _s(settings))["renewal"] == "auto"
    assert conn.view(None, _s(settings, DHAN_PIN=None))["renewal"] == "manual"


# --- generateAccessToken ------------------------------------------------------------------


async def test_generate_sends_client_pin_and_current_code(settings: Settings) -> None:
    seen: list[httpx.Request] = []
    async with _dhan(_ok(), seen) as http:
        session = await dhan_auth.generate_with_totp(_s(settings), http, NOW)
    assert session.access_token == TOKEN and session.client_id == CID
    assert session.expires_at == NOW + timedelta(hours=24)
    (req,) = seen
    assert req.method == "POST" and req.url.path == "/app/generateAccessToken"
    assert req.url.host == "auth.dhan.co"
    assert dict(req.url.params) == {
        "dhanClientId": CID,
        "pin": PIN,
        "totp": dhan_auth.totp(SECRET, NOW),
    }


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (httpx.Response(401, json={"error": "bad"}), "dhan_refused"),
        (httpx.Response(200, json={"status": "error", "message": "Invalid TOTP"}), "dhan_refused"),
        (httpx.Response(503), "dhan_unavailable"),
        (_ok(cid="9999999999"), "dhan_client_mismatch"),
    ],
)
async def test_generate_refusals_are_codes(
    settings: Settings, answer: httpx.Response, code: str
) -> None:
    async with _dhan(answer, []) as http:
        with pytest.raises(dhan_auth.DhanAuthError) as err:
            await dhan_auth.generate_with_totp(_s(settings), http, NOW)
    assert err.value.code == code
    assert PIN not in err.value.detail and TOKEN not in err.value.detail


# --- the job ------------------------------------------------------------------------------


async def test_nothing_to_do_without_config_or_with_a_fresh_session(settings: Settings) -> None:
    redis, seen = FakeRedis(), []
    async with _dhan(_ok(), seen) as http:
        off = await dhan_auto_login.run(
            _maker([]), _s(settings, DHAN_PIN=None), redis, http, now=NOW
        )  # type: ignore[arg-type]
        fresh = _s(
            settings,
            DHAN_SESSION_TOKEN=SecretStr(TOKEN),
            DHAN_SESSION_EXPIRES_AT=NOW + timedelta(hours=10),
        )
        ok = await dhan_auto_login.run(_maker([]), fresh, redis, http, now=NOW)  # type: ignore[arg-type]
    assert off.outcome == "not_configured" and ok.outcome == "not_needed"
    assert seen == []


def test_needs_login_inside_the_window(settings: Settings) -> None:
    s = _s(settings, DHAN_SESSION_TOKEN=SecretStr(TOKEN), DHAN_AUTO_LOGIN_BEFORE_MINUTES=120)
    soon = s.model_copy(update={"DHAN_SESSION_EXPIRES_AT": NOW + timedelta(minutes=90)})
    later = s.model_copy(update={"DHAN_SESSION_EXPIRES_AT": NOW + timedelta(hours=3)})
    assert dhan_auto_login.needs_login(soon, NOW)
    assert not dhan_auto_login.needs_login(later, NOW)
    assert dhan_auto_login.needs_login(_s(settings), NOW)  # no session at all


async def test_login_stores_verifies_audits_and_publishes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored: list[Any] = []

    async def verify(s: Settings, token: str, **_: Any) -> conn.Verification:
        assert token == TOKEN
        return conn.Verification("ok", source="session")

    async def store(db: Any, cipher: Any, session: Any, user_id: Any) -> None:
        stored.append((session.access_token, session.expires_at, user_id))

    async def mark(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(conn, "verify_token", verify)
    monkeypatch.setattr(dhan_auth, "store_session", store)
    monkeypatch.setattr(conn, "mark_login", mark)
    redis, added = FakeRedis(), []
    async with _dhan(_ok(), []) as http:
        res = await dhan_auto_login.run(_maker(added), _s(settings), redis, http, now=NOW)  # type: ignore[arg-type]
    assert res.outcome == "logged_in" and res.expires_at == NOW + timedelta(hours=24)
    assert stored == [(TOKEN, NOW + timedelta(hours=24), None)]
    assert _events(added) == ["dhan_auto_login_completed"]
    assert TOKEN not in json.dumps([getattr(r, "details", None) for r in added], default=str)
    keys = json.loads(redis.published[-1][1])["keys"]
    assert set(keys) == set(dhan_auth.SESSION_KEYS)
    last = await dhan_auto_login.last(redis)
    assert last is not None and last["outcome"] == "logged_in"
    assert await redis.get(dhan_auto_login.LOCK_KEY) is None

    # A second run straight after (short expiry from Dhan) does not log in again.
    async with _dhan(_ok(), seen := []) as http:
        again = await dhan_auto_login.run(_maker(added), _s(settings), redis, http, now=NOW)  # type: ignore[arg-type]
    assert again.outcome == "not_needed" and again.code == "recent_login" and seen == []


async def test_a_refused_pin_is_never_retried_until_it_changes(settings: Settings) -> None:
    redis, added, seen = FakeRedis(), [], []
    refused = httpx.Response(401, json={"error": "bad"})
    async with _dhan(refused, seen) as http:
        first = await dhan_auto_login.run(_maker(added), _s(settings), redis, http, now=NOW)  # type: ignore[arg-type]
        second = await dhan_auto_login.run(
            _maker(added), _s(settings), redis, http, now=NOW + timedelta(minutes=10)
        )  # type: ignore[arg-type]
        changed = await dhan_auto_login.run(
            _maker(added),
            _s(settings, DHAN_PIN=SecretStr("654321")),
            redis,
            http,
            now=NOW + timedelta(minutes=20),
        )  # type: ignore[arg-type]
    assert (first.outcome, first.code) == ("failed", "dhan_refused")
    assert (second.outcome, second.code) == ("blocked", "dhan_refused")
    assert changed.outcome == "failed"
    assert len(seen) == 2  # the blocked run never reached Dhan
    assert _events(added) == ["dhan_auto_login_failed", "dhan_auto_login_failed"]
    stored = (await redis.get(dhan_auto_login.REFUSED_KEY)) or b""
    assert PIN.encode() not in stored and SECRET.encode() not in stored


async def test_a_foreign_or_rejected_token_is_not_stored(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def verify(s: Settings, token: str, **_: Any) -> conn.Verification:
        return conn.Verification("rejected", "DH-901")

    async def store(*a: Any, **kw: Any) -> None:
        raise AssertionError("a rejected token must not be stored")

    monkeypatch.setattr(conn, "verify_token", verify)
    monkeypatch.setattr(dhan_auth, "store_session", store)
    redis, added = FakeRedis(), []
    async with _dhan(_ok(), []) as http:
        res = await dhan_auto_login.run(_maker(added), _s(settings), redis, http, now=NOW)  # type: ignore[arg-type]
    assert (res.outcome, res.code) == ("failed", "dhan_token_rejected")
    assert _events(added) == ["dhan_auto_login_failed"]
    assert redis.published == []


async def test_logout_pauses_until_the_next_login(settings: Settings) -> None:
    redis, seen = FakeRedis(), []
    await redis.set(dhan_auto_login.REFUSED_KEY, "x")
    await dhan_auto_login.pause(redis)
    async with _dhan(_ok(), seen) as http:
        res = await dhan_auto_login.run(_maker([]), _s(settings), redis, http, now=NOW)  # type: ignore[arg-type]
    assert (res.outcome, res.code) == ("paused", "logged_out") and seen == []
    await dhan_auto_login.resume(redis)
    assert await redis.get(dhan_auto_login.PAUSED_KEY) is None
    assert await redis.get(dhan_auto_login.REFUSED_KEY) is None


async def test_one_login_at_a_time(settings: Settings) -> None:
    redis, seen = FakeRedis(), []
    await redis.set(dhan_auto_login.LOCK_KEY, "1", ex=60)
    async with _dhan(_ok(), seen) as http:
        res = await dhan_auto_login.run(_maker([]), _s(settings), redis, http, now=NOW)  # type: ignore[arg-type]
    assert res.outcome == "busy" and seen == []
