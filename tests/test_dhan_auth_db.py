"""Dhan API key login against Postgres + Redis with Dhan mocked (app/api/v1/dhan_auth.py).

Fixtures follow https://dhanhq.co/docs/v2/authentication/ (generate-consent, consentApp-login,
consumeApp-consent). What must hold: admin + CSRF, a login started from this session, the
configured client id, the token stored encrypted and never returned, audited without values,
used by the Dhan adapter at once, logout falls back to the manual token.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core.config import Settings, set_effective_settings
from app.core.logging import register_secret_values
from app.services import app_settings as svc
from tests import test_finance_db
from tests.test_finance_db import Env, EnvFactory

pytestmark = pytest.mark.db

MASTER = Fernet.generate_key().decode()
CLIENT = "1100003626"
API_KEY = "dhan-api-key-" + "k" * 20
API_SECRET = "dhan-api-secret-" + "s" * 20
CONSENT = "940b0ca1-3ff4-4476-b46e-03a3ce7dc55d"
TOKEN_ID = "7f3c2a10-9e8b-4d2c-a111-0123456789ab"
ACCESS = "eyJhbGciOiJIUzUxMiJ9." + "A" * 60 + ".sig" + "B" * 30
MANUAL = "eyJhbGciOiJIUzUxMiJ9." + "M" * 60 + ".man" + "N" * 30
AUTH = "https://auth.dhan.test"
URL = "/api/v1/dhan/auth"
make_env = test_finance_db.make_env


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
        }
    )


@pytest.fixture(autouse=True)
def _reset_process_settings() -> Any:
    yield
    set_effective_settings(None)
    register_secret_values([])


class FakeDhan:
    """The two Dhan auth endpoints; records what was sent."""

    def __init__(
        self, client_id: str = CLIENT, expiry: str | None = None, secret: str = API_SECRET
    ) -> None:
        self.calls: list[httpx.Request] = []
        self.client_id = client_id
        self.secret = secret
        # Dhan answers in IST without an offset.
        ist = datetime.now(UTC) + timedelta(hours=5, minutes=30) + timedelta(hours=24)
        self.expiry = expiry or ist.strftime("%Y-%m-%dT%H:%M:%S")

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if req.headers.get("app_id") != API_KEY or req.headers.get("app_secret") != self.secret:
            # Echoes what it got, as a careless upstream might: must never reach the caller.
            sent = req.headers.get("app_secret", "")
            return httpx.Response(401, json={"error": "bad app credentials " + sent})
        if req.url.path == "/app/generate-consent" and req.method == "POST":
            assert req.url.params["client_id"] == CLIENT
            return httpx.Response(
                200,
                json={
                    "consentAppId": CONSENT,
                    "consentAppStatus": "GENERATED",
                    "status": "success",
                },
            )
        if req.url.path == "/app/consumeApp-consent" and req.method == "GET":
            assert req.url.params["tokenId"] == TOKEN_ID
            return httpx.Response(
                200,
                json={
                    "dhanClientId": self.client_id,
                    "dhanClientName": "JOHN DOE",
                    "dhanClientUcc": "CEFE4265",
                    "givenPowerOfAttorney": True,
                    "accessToken": ACCESS,
                    "expiryTime": self.expiry,
                },
            )
        return httpx.Response(404)


def _install(env: Env, fake: FakeDhan) -> None:
    env.app.state.dhan_auth_transport = httpx.MockTransport(fake)


async def _audit(env: Env) -> str:
    rows = await env.sql("SELECT event_type, details::text FROM audit.audit_events")
    return json.dumps([list(r) for r in rows])


async def test_full_login_stores_encrypted_session_and_uses_it(make_env: EnvFactory) -> None:
    async with make_env() as env:
        fake = FakeDhan()
        _install(env, fake)
        r = await env.send("admin", "POST", f"{URL}/start")
        assert r.status_code == 200, r.text
        assert r.json()["login_url"] == (f"{AUTH}/login/consentApp-login?consentAppId={CONSENT}")
        assert API_SECRET not in r.text and API_KEY not in r.text

        # Dhan sends the browser back (cross-site, no cookies): just a redirect to the SPA.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=env.app), base_url="https://test"
        ) as anon:
            cb = await anon.get(f"{URL}/callback/", params={"tokenId": TOKEN_ID})
            assert cb.status_code == 303
            assert cb.headers["location"] == f"/dev/settings?dhan_token_id={TOKEN_ID}"
            assert cb.headers["cache-control"] == "no-store"
            bad = await anon.get(f"{URL}/callback", params={"tokenId": "<script>"})
            assert bad.headers["location"] == "/dev/settings?dhan_error=bad_callback"

        r = await env.send("admin", "POST", f"{URL}/consume", json={"token_id": TOKEN_ID})
        assert r.status_code == 200, r.text
        body = r.json()
        assert ACCESS not in r.text
        assert body["session_active"] and body["source"] == "session" and not body["data_stale"]
        exp = datetime.fromisoformat(body["session_expires_at"])
        assert timedelta(hours=23) < exp - datetime.now(UTC) <= timedelta(hours=24, seconds=5)

        # Encrypted at rest, decryptable with the master key; no plaintext anywhere.
        rows = dict(await env.sql("SELECT key, value_encrypted FROM core.app_settings"))
        assert set(rows) == {"DHAN_SESSION_TOKEN", "DHAN_SESSION_EXPIRES_AT"}
        assert ACCESS not in rows["DHAN_SESSION_TOKEN"]
        assert svc.Cipher(MASTER).decrypt(rows["DHAN_SESSION_TOKEN"]) == ACCESS

        # Live: the API's settings and Dhan adapter use the session token now.
        live: Settings = env.app.state.settings
        assert live.dhan_access_token().get_secret_value() == ACCESS  # type: ignore[union-attr]
        adapter = env.app.state.trading_adapters["dhan"]
        assert adapter.client._current_token() == ACCESS

        # Settings list: masked, managed, with the expiry; never the value.
        s = await env.get("admin", "/api/v1/settings")
        item = next(i for i in s.json()["settings"] if i["key"] == "DHAN_SESSION_TOKEN")
        assert item["managed"] and item["source"] == "db" and item["expires_at"]
        assert ACCESS not in s.text and API_SECRET not in s.text

        events = await _audit(env)
        assert "dhan_login_started" in events and "dhan_login_completed" in events
        for secret in (ACCESS, API_KEY, API_SECRET, TOKEN_ID):
            assert secret not in events


async def test_consume_needs_a_login_started_from_this_session(make_env: EnvFactory) -> None:
    async with make_env() as env:
        _install(env, FakeDhan())
        r = await env.send("admin", "POST", f"{URL}/consume", json={"token_id": TOKEN_ID})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "no_pending_login"
        # One-time: a second consume after a successful one is refused too.
        await env.send("admin", "POST", f"{URL}/start")
        assert (await env.send("admin", "POST", f"{URL}/consume",
                               json={"token_id": TOKEN_ID})).status_code == 200  # fmt: skip
        r = await env.send("admin", "POST", f"{URL}/consume", json={"token_id": TOKEN_ID})
        assert r.status_code == 409


async def test_token_for_another_client_id_is_refused(make_env: EnvFactory) -> None:
    async with make_env() as env:
        _install(env, FakeDhan(client_id="9999999999"))
        await env.send("admin", "POST", f"{URL}/start")
        r = await env.send("admin", "POST", f"{URL}/consume", json={"token_id": TOKEN_ID})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "dhan_client_mismatch"
        assert ACCESS not in r.text
        assert (await env.sql("SELECT count(*) FROM core.app_settings"))[0][0] == 0
        assert "dhan_login_failed" in await _audit(env)


async def test_admin_and_csrf_required(make_env: EnvFactory) -> None:
    async with make_env() as env:
        _install(env, FakeDhan())
        assert (await env.send("a", "POST", f"{URL}/start")).status_code == 403
        assert (await env.clients["admin"].post(f"{URL}/start")).status_code == 403  # no CSRF
        assert (await env.send("a", "POST", f"{URL}/consume",
                               json={"token_id": TOKEN_ID})).status_code == 403  # fmt: skip
        assert (await env.send("a", "DELETE", f"{URL}/session")).status_code == 403
        # Status is for any signed-in user (the /trading banner) and has no token.
        st = await env.get("a", f"{URL}/status")
        assert st.status_code == 200 and MANUAL not in st.text
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=env.app), base_url="https://test"
        ) as anon:
            assert (await anon.get(f"{URL}/status")).status_code == 401


async def test_managed_keys_cannot_be_written_through_settings(make_env: EnvFactory) -> None:
    async with make_env() as env:
        for method, body in (("PUT", {"value": ACCESS, "password": "x"}), ("DELETE", None)):
            r = await env.send(
                "admin", method, "/api/v1/settings/DHAN_SESSION_TOKEN",
                **({"json": body} if body else {}),
            )  # fmt: skip
            assert r.status_code == 403 and r.json()["detail"]["code"] == "managed_key"


async def test_logout_falls_back_to_the_manual_token(make_env: EnvFactory) -> None:
    async with make_env() as env:
        _install(env, FakeDhan())
        await env.send("admin", "POST", f"{URL}/start")
        await env.send("admin", "POST", f"{URL}/consume", json={"token_id": TOKEN_ID})
        r = await env.send("admin", "DELETE", f"{URL}/session")
        assert r.status_code == 200 and r.json()["source"] == "manual"
        assert (await env.sql("SELECT count(*) FROM core.app_settings"))[0][0] == 0
        live: Settings = env.app.state.settings
        assert live.dhan_access_token().get_secret_value() == MANUAL  # type: ignore[union-attr]
        assert "dhan_logout" in await _audit(env)


async def test_dhan_refusal_is_a_code_never_a_body(make_env: EnvFactory) -> None:
    async with make_env() as env:
        _install(env, FakeDhan(secret="the-secret-dhan-expects-" + "x" * 10))
        r = await env.send("admin", "POST", f"{URL}/start")
        assert r.status_code == 502 and r.json()["detail"]["code"] == "dhan_refused"
        assert API_SECRET not in r.text and "bad app credentials" not in r.text
