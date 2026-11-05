"""Dhan connection state, feed control and monitoring against Postgres + Redis (Dhan mocked).

Covers what needs the database: the Profile-API check at login, dhan.connections across an
app restart, logout keeping broker accounts, credential replacement through /settings, the
admin-only reconnect with cooldown and audit, and the monitor -> incident -> bounded
recovery -> escalation loop. Run with `make test-db`.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager
from cryptography.fernet import Fernet
from pydantic import SecretStr
from redis.asyncio import Redis

from app.core.config import Settings, set_effective_settings
from app.core.logging import register_secret_values
from app.main import create_app
from app.services import dhan_feed_health as fh
from app.services import dhan_monitor, repair_recovery
from tests import test_finance_db
from tests.conftest import TEST_REDIS_URL
from tests.test_dhan_auth_db import (
    ACCESS,
    API_KEY,
    API_SECRET,
    AUTH,
    CLIENT,
    MANUAL,
    TOKEN_ID,
    FakeDhan,
)
from tests.test_finance_db import PASSWORD, Env, EnvFactory

pytestmark = pytest.mark.db

MASTER = Fernet.generate_key().decode()
URL = "/api/v1/dhan/auth"
make_env = test_finance_db.make_env
OPEN = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)  # Monday 10:00 IST


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "SETTINGS_MASTER_KEY": SecretStr(MASTER),
            "DHAN_CLIENT_ID": CLIENT,
            "DHAN_API_KEY": SecretStr(API_KEY),
            "DHAN_API_SECRET": SecretStr(API_SECRET),
            "DHAN_AUTH_URL": AUTH,
            "DHAN_ACCESS_TOKEN": SecretStr(MANUAL),
            "DHAN_FEED_INSTRUMENTS": ["NSE_EQ:3499"],
        }
    )


@pytest.fixture(autouse=True)
def _reset_process_settings() -> Any:
    yield
    set_effective_settings(None)
    register_secret_values([])


def _install(env: Env, fake: FakeDhan) -> None:
    env.app.state.dhan_auth_transport = httpx.MockTransport(fake)


async def _login(env: Env) -> httpx.Response:
    assert (await env.send("admin", "POST", f"{URL}/start")).status_code == 200
    r: httpx.Response = await env.send(
        "admin", "POST", f"{URL}/consume", json={"token_id": TOKEN_ID}
    )
    return r


async def _audit(env: Env) -> list[tuple[str, dict[str, Any]]]:
    rows = await env.sql("SELECT event_type, details FROM audit.audit_events ORDER BY id")
    return [(r[0], r[1]) for r in rows]


# --- login verification -----------------------------------------------------------------------


async def test_login_is_verified_with_profile_and_persisted(make_env: EnvFactory) -> None:
    async with make_env() as env:
        fake = FakeDhan()
        _install(env, fake)
        r = await _login(env)
        assert r.status_code == 200, r.text
        assert r.json()["verification"] == "ok" and ACCESS not in r.text
        profile = [c for c in fake.calls if c.url.path.endswith("/profile")]
        assert profile and profile[0].headers["access-token"] == ACCESS
        c = await env.get("a", f"{URL}/connection")  # any signed-in user may read it
        body = c.json()
        assert c.status_code == 200 and body["status"] == "connected"
        assert body["token_source"] == "session" and body["data_plan"] == "Active"
        assert body["renewal"] == "manual" and body["connected_at"]
        assert ACCESS not in c.text and CLIENT not in c.text and MANUAL not in c.text
        row = (await env.sql("SELECT status, credential_fp FROM dhan.connections"))[0]
        assert row[0] == "connected" and ACCESS not in row[1]
        events = dict(await _audit(env))
        assert events["dhan_login_completed"]["verification"] == "ok"


@pytest.mark.parametrize(
    ("fake", "status", "code"),
    [
        (FakeDhan(profile_status=401), 502, "dhan_token_rejected"),
        (FakeDhan(profile_client="9999999999"), 403, "dhan_client_mismatch"),
    ],
)
async def test_login_token_refused_by_profile_is_never_stored(
    make_env: EnvFactory, fake: FakeDhan, status: int, code: str
) -> None:
    async with make_env() as env:
        _install(env, fake)
        r = await _login(env)
        assert r.status_code == status and r.json()["detail"]["code"] == code
        assert ACCESS not in r.text and "9999999999" not in r.text
        assert (await env.sql("SELECT count(*) FROM core.app_settings"))[0][0] == 0
        failed = [d for e, d in await _audit(env) if e == "dhan_login_failed"]
        assert failed and failed[0]["step"] == "profile"


async def test_login_during_dhan_api_outage_is_stored_unverified(make_env: EnvFactory) -> None:
    async with make_env() as env:
        _install(env, FakeDhan(profile_status=503))
        r = await _login(env)
        assert r.status_code == 200 and r.json()["verification"] == "unavailable"
        c = (await env.get("admin", f"{URL}/connection")).json()
        assert c["status"] == "unverified" and c["consecutive_failures"] >= 1
        # Dhan is back: an admin verifies, the session becomes connected.
        _install(env, FakeDhan())
        v = await env.send("admin", "POST", f"{URL}/verify")
        assert v.status_code == 200 and v.json()["status"] == "connected"


# --- logout, replacement, restart ------------------------------------------------------------


async def test_logout_keeps_broker_accounts_and_the_row(make_env: EnvFactory) -> None:
    async with make_env() as env:
        _install(env, FakeDhan())
        acct = await env.send(
            "admin",
            "POST",
            "/api/trading/accounts",
            json={"broker": "dhan", "account_id": CLIENT, "label": "main",
                  "base_currency": "INR", "owner_user_id": str(env.ids["a"])},
        )  # fmt: skip
        assert acct.status_code in (200, 201), acct.text
        await _login(env)
        r = await env.send("admin", "DELETE", f"{URL}/session")
        assert r.status_code == 200 and r.json()["source"] == "manual"
        assert (await env.sql("SELECT count(*) FROM trading.broker_accounts"))[0][0] == 1
        rows = await env.sql("SELECT status, token_source, disconnected_at FROM dhan.connections")
        assert rows[0][0] == "unverified" and rows[0][1] == "manual" and rows[0][2] is not None
        logout = [d for e, d in await _audit(env) if e == "dhan_logout"]
        assert logout[0]["status"] == "unverified"


async def test_credential_replacement_and_expiry_via_settings(make_env: EnvFactory) -> None:
    exp = int((datetime.now(UTC) - timedelta(hours=1)).timestamp())
    payload = json.dumps({"exp": exp}).encode()
    expired = (
        "eyJhbGciOiJIUzUxMiJ9."
        + base64.urlsafe_b64encode(payload).decode().rstrip("=")
        + ".sig"
        + "Z" * 30
    )
    async with make_env() as env:
        fake = FakeDhan()
        _install(env, fake)
        v = await env.send("admin", "POST", f"{URL}/verify")
        assert v.json()["status"] == "connected" and v.json()["token_source"] == "manual"
        first_version = v.json()["version"]
        put = await env.send(
            "admin", "PUT", "/api/v1/settings/DHAN_ACCESS_TOKEN",
            json={"value": expired, "password": PASSWORD},
        )  # fmt: skip
        assert put.status_code == 200, put.text
        calls = len(fake.calls)
        v = await env.send("admin", "POST", f"{URL}/verify")
        assert v.json()["status"] == "expired" and len(fake.calls) == calls  # no Dhan call
        new = "eyJhbGciOiJIUzUxMiJ9." + "Q" * 60 + ".new" + "R" * 30
        fake.valid_tokens.add(new)
        await env.send(
            "admin", "PUT", "/api/v1/settings/DHAN_ACCESS_TOKEN",
            json={"value": new, "password": PASSWORD},
        )  # fmt: skip
        v = await env.send("admin", "POST", f"{URL}/verify")
        assert v.json()["status"] == "connected" and v.json()["version"] > first_version
        assert new not in v.text and expired not in v.text
        outcomes = [d["outcome"] for e, d in await _audit(env) if e == "dhan_credentials_verified"]
        assert outcomes == ["ok", "expired", "ok"]
        for e, d in await _audit(env):
            assert new not in json.dumps(d) and expired not in json.dumps(d)


@asynccontextmanager
async def _second_app(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app) as mgr:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=mgr.app), base_url="https://test"
        ) as c:
            r = await c.post(
                "/api/auth/login", json={"email": "admin@spasta.online", "password": PASSWORD}
            )
            assert r.status_code == 200
            yield c


async def test_connection_state_survives_a_restart(
    make_env: EnvFactory, settings: Settings
) -> None:
    async with make_env() as env:
        _install(env, FakeDhan())
        await _login(env)
        before = (await env.get("admin", f"{URL}/connection")).json()
    # A new process (deploy, crash restart): the stored state is what it reports.
    async with _second_app(settings) as c:
        after = (await c.get(f"{URL}/connection")).json()
        status = (await c.get(f"{URL}/status")).json()
    assert after == before and after["status"] == "connected"
    assert status["source"] == "session"  # the encrypted session token was reloaded too


# --- feed control and health ---------------------------------------------------------------


async def test_reconnect_is_admin_only_rate_limited_and_audited(make_env: EnvFactory) -> None:
    async with make_env() as env:
        assert (await env.send("a", "POST", "/api/v1/dhan/feed/reconnect")).status_code == 403
        r = await env.send("admin", "POST", "/api/v1/dhan/feed/reconnect")
        assert r.status_code == 200 and r.json()["receivers"] == 0
        assert "dhan profile" in r.json()["detail"]
        again = await env.send("admin", "POST", "/api/v1/dhan/feed/reconnect")
        assert again.status_code == 429 and int(again.headers["retry-after"]) >= 1
        events = [d for e, d in await _audit(env) if e == "dhan_feed_reconnect_requested"]
        assert [d["accepted"] for d in events] == [True, False]


async def test_feed_health_reports_a_missing_worker(make_env: EnvFactory) -> None:
    async with make_env() as env:
        r = await env.get("a", "/api/v1/dhan/feed/health")
        body = r.json()
        assert r.status_code == 200 and body["status"] == "not_running"
        assert not body["healthy"] and body["codes"] == ["not_running"]
        card = (await env.get("admin", "/api/v1/system/integrations")).json()
        dhan = card["integrations"]["dhan"]
        assert dhan["feed_health"]["status"] == "not_running"
        assert any("not running" in p for p in dhan["problems"])


# --- monitor -> incident -> bounded recovery -> escalation ----------------------------------


async def test_monitor_recovery_is_bounded_then_escalates(
    make_env: EnvFactory, settings: Settings
) -> None:
    redis = Redis.from_url(TEST_REDIS_URL)
    async with make_env() as env:
        maker = env.maker
        down = {"state": "running", "connected": False, "subscribed": 0, "fresh": 0,
                "stale": 0, "redis_ok": True, "updated_at": OPEN.timestamp() - 2}  # fmt: skip
        await redis.set(fh.STATUS_KEY, json.dumps(down))
        out = await dhan_monitor.run(maker, settings, redis, now=OPEN)
        assert "feed_disconnected" in out["failing_checks"], out
        rows = await env.sql(
            "SELECT id, source, details->>'check', status FROM agents.incidents "
            "WHERE source = 'dhan'"
        )
        inc_id = next(r[0] for r in rows if r[2] == "feed_disconnected")
        deps = repair_recovery.Deps(maker, settings, redis)
        # Attempt 1: no worker listening -> failed; recorded and audited.
        assert await repair_recovery.handle(deps, inc_id, now=OPEN) == "attempted"
        # Inside the cooldown nothing runs.
        assert await repair_recovery.handle(deps, inc_id, now=OPEN) == "cooldown"
        later = OPEN + timedelta(seconds=settings.INCIDENT_RECOVERY_COOLDOWN_SECONDS + 1)
        assert await repair_recovery.handle(deps, inc_id, now=later) == "attempted"
        # Retries exhausted: a person is asked; no further automatic action.
        much_later = later + timedelta(seconds=settings.INCIDENT_RECOVERY_COOLDOWN_SECONDS + 1)
        assert await repair_recovery.handle(deps, inc_id, now=much_later) == "manual"
        assert await repair_recovery.handle(deps, inc_id, now=much_later) == "skipped"
        details = (await env.sql(
            "SELECT recovery_attempts, details FROM agents.incidents WHERE id = :i", i=inc_id
        ))[0]  # fmt: skip
        assert details[0] == 2 and details[1]["_manual"] and len(details[1]["_recovery"]) == 2
        attempts = [d for e, d in await _audit(env) if e == "incident_recovery_attempt"]
        assert len(attempts) == 2
        assert all(r["action"] == "reconnect_dhan_feed" for a in attempts for r in a["results"])
        # The feed comes back: the incident resolves itself.
        healthy = down | {"connected": True, "subscribed": 1, "fresh": 1,
                          "last_packet_at": OPEN.timestamp()}  # fmt: skip
        await redis.set(fh.STATUS_KEY, json.dumps(healthy))
        out = await dhan_monitor.run(maker, settings, redis, now=OPEN)
        assert out["resolved"] >= 1
        st = await env.sql("SELECT status FROM agents.incidents WHERE id = :i", i=inc_id)
        assert st[0][0] == "resolved"
    await redis.aclose()


async def test_expired_credential_escalates_to_a_person_at_once(
    make_env: EnvFactory, settings: Settings
) -> None:
    redis = Redis.from_url(TEST_REDIS_URL)
    gone = settings.model_copy(update={"DHAN_ACCESS_TOKEN": None, "DHAN_FEED_INSTRUMENTS": []})
    async with make_env() as env:
        out = await dhan_monitor.run(env.maker, gone, redis, now=OPEN)
        assert "auth" in out["failing_checks"]
        inc = (await env.sql(
            "SELECT id, severity, details FROM agents.incidents WHERE details->>'check' = 'auth'"
        ))[0]  # fmt: skip
        assert inc[1] == "error" and inc[2]["renewal"] == "manual"
        deps = repair_recovery.Deps(env.maker, gone, redis)
        assert await repair_recovery.handle(deps, inc[0], now=OPEN) == "manual"
        assert (await env.sql(
            "SELECT recovery_attempts FROM agents.incidents WHERE id = :i", i=inc[0]
        ))[0][0] == 0  # fmt: skip
        assert await redis.get(fh.RECONNECT_COOLDOWN_KEY) is None  # nothing was "tried"
    await redis.aclose()
