"""Dhan API key login against Postgres + Redis with Dhan mocked (app/api/v1/dhan_auth.py).

Fixtures follow https://dhanhq.co/docs/v2/authentication/ (generate-consent, consentApp-login,
consumeApp-consent). What must hold: admin + CSRF, a login started by this admin from this
browser (the one-time login handoff, which survives a Spasta re-login), the configured client
id, the token stored encrypted and never returned, audited without values, used by the Dhan
adapter at once, logout falls back to the manual token.
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
from app.services import dhan_auth as dhan_svc
from app.services import dhan_login_handoff as handoff
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
    """The two Dhan auth endpoints and the Profile API; records what was sent.

    `profile_status` / `profile_client` shape GET /v2/profile (the verification every new
    session token goes through): 200 for the configured client by default."""

    def __init__(
        self,
        client_id: str = CLIENT,
        expiry: str | None = None,
        secret: str = API_SECRET,
        *,
        profile_status: int = 200,
        profile_client: str | None = None,
        valid_tokens: tuple[str, ...] = (ACCESS, MANUAL),
        consume_fails: str | None = None,
    ) -> None:
        self.calls: list[httpx.Request] = []
        # "network" (Dhan unreachable) or "refuse" (Dhan answers 400) on consumeApp-consent.
        self.consume_fails = consume_fails
        self.client_id = client_id
        self.secret = secret
        self.profile_status = profile_status
        self.profile_client = profile_client or client_id
        self.valid_tokens = set(valid_tokens)
        # Dhan answers in IST without an offset.
        ist = datetime.now(UTC) + timedelta(hours=5, minutes=30) + timedelta(hours=24)
        self.expiry = expiry or ist.strftime("%Y-%m-%dT%H:%M:%S")

    def profile(self, req: httpx.Request) -> httpx.Response:
        if self.profile_status >= 500:
            return httpx.Response(self.profile_status, json={"errorCode": "DH-908"})
        if self.profile_status != 200 or req.headers.get("access-token") not in self.valid_tokens:
            return httpx.Response(
                401,
                json={
                    "errorType": "Invalid_Authentication",
                    "errorCode": "DH-901",
                    "errorMessage": "Client ID or user generated access token is invalid",
                },
            )
        validity = datetime.now(UTC) + timedelta(hours=5, minutes=30) + timedelta(hours=23)
        return httpx.Response(
            200,
            json={
                "dhanClientId": self.profile_client,
                "tokenValidity": validity.strftime("%d/%m/%Y %H:%M"),
                "activeSegment": "Equity, Derivative",
                "ddpi": "Active",
                "mtf": "Deactive",
                "dataPlan": "Active",
                "dataValidity": "2026-12-05 09:37:52.0",
            },
        )

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if req.url.path.endswith("/profile") and req.method == "GET":
            return self.profile(req)
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
            if self.consume_fails == "network":
                raise httpx.ConnectError("dhan down", request=req)
            if self.consume_fails == "refuse":
                return httpx.Response(400, json={"error": "token already used " + TOKEN_ID})
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


def _login_cookie(env: Env, who: str = "admin") -> str | None:
    name = handoff.cookie_name(env.app.state.settings)
    for c in env.clients[who].cookies.jar:
        if c.name == name:
            return c.value
    return None


async def dhan_returns(
    env: Env, who: str = "admin", token: str = TOKEN_ID, state: str | None = "own"
) -> httpx.Response:
    """Dhan sends the browser back: a cross-site navigation, so only the Lax login cookie
    rides along (the Spasta session cookies are SameSite=Strict)."""
    name = handoff.cookie_name(env.app.state.settings)
    value = _login_cookie(env, who) if state == "own" else state
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="https://test",
        cookies={name: value} if value else None,
    ) as browser:  # fmt: skip
        return await browser.get(f"{URL}/callback/", params={"tokenId": token})


def _watch_logs(caplog: pytest.LogCaptureFixture) -> None:
    """create_app() replaces the root handlers (configure_logging): re-attach the capture."""
    import logging

    root = logging.getLogger()
    if caplog.handler not in root.handlers:
        root.addHandler(caplog.handler)
    caplog.set_level(logging.INFO)


def _assert_logs_scrubbed(caplog: pytest.LogCaptureFixture, secrets: tuple[str, ...]) -> None:
    """httpx logs every request URL, including the server's exchange call to Dhan
    (`consumeApp-consent?tokenId=...`). What the log handler writes is the record after
    app.core.logging's scrubber: no secret may survive it."""
    from app.core.logging import scrub

    lines = [r.getMessage() for r in caplog.records]
    assert any("consumeApp-consent" in line for line in lines)  # the exchange was logged
    for line in lines:
        written = scrub(line)
        for secret in secrets:
            assert secret not in written, written


async def consume(env: Env, who: str = "admin", **body: Any) -> httpx.Response:
    r: httpx.Response = await env.send(who, "POST", f"{URL}/consume", json=body or None)
    return r


async def relogin(env: Env, who: str = "admin") -> None:
    """Sign in to Spasta again in the same browser: a NEW session (and CSRF token)."""
    email = "admin@spasta.online" if who == "admin" else f"{who}@spasta.online"
    r = await env.clients[who].post(
        "/api/auth/login", json={"email": email, "password": test_finance_db.PASSWORD}
    )
    assert r.status_code == 200, r.text
    env.csrf[who] = r.json()["csrf_token"]


async def test_full_login_stores_encrypted_session_and_uses_it(
    make_env: EnvFactory, caplog: pytest.LogCaptureFixture
) -> None:
    async with make_env() as env:
        _watch_logs(caplog)
        fake = FakeDhan()
        _install(env, fake)
        r = await env.send("admin", "POST", f"{URL}/start")
        assert r.status_code == 200, r.text
        assert r.json()["login_url"] == (f"{AUTH}/login/consentApp-login?consentAppId={CONSENT}")
        assert API_SECRET not in r.text and API_KEY not in r.text

        # The one-time login cookie: HttpOnly, Lax (rides Dhan's redirect), scoped, short.
        setc = r.headers["set-cookie"]
        assert "HttpOnly" in setc and "SameSite=lax" in setc and "Secure" in setc
        assert "Path=/api/v1/dhan/auth" in setc and "Max-Age=600" in setc
        state = _login_cookie(env)
        assert state and len(state) >= 43

        # Dhan sends the browser back: the tokenId stays on the server.
        cb = await dhan_returns(env)
        assert cb.status_code == 303
        assert cb.headers["location"] == "/dev/settings?dhan_login=ready"
        assert TOKEN_ID not in cb.headers["location"]
        assert cb.headers["cache-control"] == "no-store"
        bad = await dhan_returns(env, token="<script>")
        assert bad.headers["location"] == "/dev/settings?dhan_error=bad_callback"
        # In Redis: the state only hashed, the tokenId only encrypted.
        redis = env.app.state.redis
        keys = [k.decode() for k in await redis.keys("dhan:auth:handoff:*")]
        assert len(keys) == 1 and state not in keys[0]
        stored = (await redis.get(keys[0])).decode()
        assert TOKEN_ID not in stored and state not in stored

        r = await consume(env)
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

        assert _login_cookie(env) is None  # cleared once used
        events = await _audit(env)
        assert "dhan_login_started" in events and "dhan_login_completed" in events
        for secret in (ACCESS, API_KEY, API_SECRET, TOKEN_ID, CONSENT, state):
            assert secret not in events
        _assert_logs_scrubbed(caplog, (ACCESS, API_KEY, API_SECRET, TOKEN_ID, CONSENT, state))


async def test_unsolicited_callbacks_and_consumes_are_refused(make_env: EnvFactory) -> None:
    async with make_env() as env:
        fake = FakeDhan()
        _install(env, fake)
        # A callback with no login started from this browser.
        cb = await dhan_returns(env, state=None)
        assert cb.headers["location"] == "/dev/settings?dhan_error=no_pending_login"
        # A forged / unknown state.
        cb = await dhan_returns(env, state=handoff.new_state())
        assert cb.headers["location"] == "/dev/settings?dhan_error=login_expired"
        r = await consume(env, token_id=TOKEN_ID)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "no_pending_login"
        # Started, but Dhan has not sent the browser back yet.
        await env.send("admin", "POST", f"{URL}/start")
        r = await consume(env)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "login_not_returned"
        assert (await env.sql("SELECT count(*) FROM core.app_settings"))[0][0] == 0
        assert not any("consumeApp-consent" in str(c.url) for c in fake.calls)


async def test_login_survives_a_spasta_relogin_by_the_same_admin(make_env: EnvFactory) -> None:
    """The reported bug: callback, Spasta says 401, the admin signs in again, consume = 409."""
    async with make_env() as env:
        _install(env, FakeDhan())
        assert (await env.send("admin", "POST", f"{URL}/start")).status_code == 200
        assert (await dhan_returns(env)).headers["location"].endswith("dhan_login=ready")
        # The Spasta session expires meanwhile.
        await env.sql("UPDATE core.sessions SET expires_at = now() - interval '1 minute'")
        r = await consume(env)
        assert r.status_code == 401  # admin authentication is not weakened
        await relogin(env)
        r = await consume(env)
        assert r.status_code == 200, r.text
        assert r.json()["source"] == "session"
        assert "dhan_login_completed" in await _audit(env)


async def test_another_user_cannot_claim_the_login(make_env: EnvFactory) -> None:
    async with make_env() as env:
        _install(env, FakeDhan())
        await env.sql("UPDATE core.users SET role_id = 1 WHERE email = 'a@spasta.online'")
        await env.send("admin", "POST", f"{URL}/start")
        await dhan_returns(env)
        # Another admin presents the stolen / shared login cookie.
        name = handoff.cookie_name(env.app.state.settings)
        state = _login_cookie(env)
        assert state
        env.clients["a"].cookies.set(name, state, domain="test.local", path="/api/v1/dhan/auth")
        r = await consume(env, who="a")
        assert r.status_code == 403 and r.json()["detail"]["code"] == "dhan_login_other_user"
        assert (await env.sql("SELECT count(*) FROM core.app_settings"))[0][0] == 0
        assert "dhan_login_other_user" in await _audit(env)
        # The admin who started it can still finish it.
        r = await consume(env)
        assert r.status_code == 200, r.text


async def test_expired_login(make_env: EnvFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    async with make_env() as env:
        _install(env, FakeDhan())
        monkeypatch.setattr(dhan_svc, "CONSENT_TTL_S", 1)
        await env.send("admin", "POST", f"{URL}/start")
        import asyncio

        await asyncio.sleep(1.3)
        cb = await dhan_returns(env)
        assert cb.headers["location"] == "/dev/settings?dhan_error=login_expired"
        r = await consume(env)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] in ("login_expired", "no_pending_login")
        # Expired after Dhan returned, before the admin finished.
        monkeypatch.setattr(dhan_svc, "CONSENT_TTL_S", 2)
        await env.send("admin", "POST", f"{URL}/start")
        assert (await dhan_returns(env)).headers["location"].endswith("dhan_login=ready")
        await asyncio.sleep(2.3)
        r = await consume(env)
        # The browser drops the cookie when it expires with the login; a client that kept it
        # gets login_expired. Either way nothing is exchanged.
        assert r.status_code == 409
        assert r.json()["detail"]["code"] in ("login_expired", "no_pending_login")
        name = handoff.cookie_name(env.app.state.settings)
        env.clients["admin"].cookies.set(name, handoff.new_state(), domain="test.local",
                                         path="/api/v1/dhan/auth")  # fmt: skip
        r = await consume(env)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "login_expired"
        assert (await env.sql("SELECT count(*) FROM core.app_settings"))[0][0] == 0


async def test_duplicate_consumption_and_callbacks(make_env: EnvFactory) -> None:
    async with make_env() as env:
        fake = FakeDhan()
        _install(env, fake)
        await env.send("admin", "POST", f"{URL}/start")
        state = _login_cookie(env)
        assert (await dhan_returns(env)).headers["location"].endswith("dhan_login=ready")
        # The same callback again is harmless; a different tokenId for this login is refused.
        assert (await dhan_returns(env)).headers["location"].endswith("dhan_login=ready")
        other = await dhan_returns(env, token="0" * 36)
        assert other.headers["location"] == "/dev/settings?dhan_error=duplicate_callback"
        # A tokenId in the body must be the one Dhan returned (older pages still send it).
        assert (await consume(env, token_id=TOKEN_ID)).status_code == 200
        # Replay with the old cookie (the response cleared it; an attacker could have kept it).
        name = handoff.cookie_name(env.app.state.settings)
        assert state
        env.clients["admin"].cookies.set(name, state, domain="test.local", path="/api/v1/dhan/auth")
        r = await consume(env)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "login_already_used"
        consumes = [c for c in fake.calls if "consumeApp-consent" in str(c.url)]
        assert len(consumes) == 1  # Dhan was asked exactly once


async def test_dhan_api_failures(make_env: EnvFactory, caplog: pytest.LogCaptureFixture) -> None:
    async with make_env() as env:
        _watch_logs(caplog)
        fake = FakeDhan(consume_fails="network")
        _install(env, fake)
        await env.send("admin", "POST", f"{URL}/start")
        await dhan_returns(env)
        # Dhan unreachable: the tokenId was not spent, so the login can be retried.
        r = await consume(env)
        assert r.status_code == 502
        assert r.json()["detail"] == {"code": "dhan_unavailable", "detail": "could not reach Dhan",
                                      "retry": True}  # fmt: skip
        fake.consume_fails = None
        r = await consume(env)
        assert r.status_code == 200, r.text

        # Dhan refuses the tokenId: final, the handoff is spent and nothing is stored.
        await env.send("admin", "DELETE", f"{URL}/session")
        fake.consume_fails = "refuse"
        await env.send("admin", "POST", f"{URL}/start")
        await dhan_returns(env)
        r = await consume(env)
        assert r.status_code == 502 and r.json()["detail"]["code"] == "dhan_refused"
        assert r.json()["detail"]["retry"] is False and TOKEN_ID not in r.text
        assert (await consume(env)).status_code == 409
        assert (await env.sql("SELECT count(*) FROM core.app_settings"))[0][0] == 0
        events = await _audit(env)
        assert "dhan_login_failed" in events and TOKEN_ID not in events
        _assert_logs_scrubbed(caplog, (ACCESS, TOKEN_ID))


async def test_token_for_another_client_id_is_refused(make_env: EnvFactory) -> None:
    async with make_env() as env:
        _install(env, FakeDhan(client_id="9999999999"))
        await env.send("admin", "POST", f"{URL}/start")
        await dhan_returns(env)
        r = await consume(env)
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
        await dhan_returns(env)
        assert (await consume(env)).status_code == 200
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


async def test_auto_login_with_pin_and_totp_stores_and_reports(make_env: EnvFactory) -> None:
    """The scheduler's PIN + TOTP login end to end: stored encrypted like a manual login,
    the connection marked, audited without values, shown on /status; logout pauses it."""
    from app.services import dhan_auto_login

    pin, secret = "246810", "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    async with make_env() as env:
        fake = FakeDhan()
        sent: list[httpx.Request] = []

        def auth(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/app/generateAccessToken":
                sent.append(req)
                return httpx.Response(
                    200,
                    json={"dhanClientId": CLIENT, "accessToken": ACCESS,
                          "expiryTime": fake.expiry},
                )  # fmt: skip
            return fake(req)

        s = env.app.state.settings.model_copy(
            update={"DHAN_PIN": SecretStr(pin), "DHAN_TOTP_SECRET": SecretStr(secret)}
        )
        redis = env.app.state.redis
        async with httpx.AsyncClient(transport=httpx.MockTransport(auth)) as http:
            res = await dhan_auto_login.run(
                env.maker, s, redis, http, transport=httpx.MockTransport(fake)
            )
        assert res.outcome == "logged_in", res
        assert len(sent) == 1 and sent[0].url.params["pin"] == pin

        rows = dict(await env.sql("SELECT key, value_encrypted FROM core.app_settings"))
        assert svc.Cipher(MASTER).decrypt(rows["DHAN_SESSION_TOKEN"]) == ACCESS
        conn_row = await env.sql("SELECT status, token_source FROM dhan.connections")
        assert list(conn_row[0]) == ["connected", "session"]
        events = await _audit(env)
        assert "dhan_auto_login_completed" in events
        for value in (ACCESS, pin, secret):
            assert value not in events

        st = await env.get("admin", f"{URL}/status")
        body = st.json()
        assert body["auto_login_last"]["outcome"] == "logged_in"
        assert ACCESS not in st.text and pin not in st.text and secret not in st.text

        # A logout sticks: the next run does not log back in.
        await env.app.state.redis.delete(dhan_auto_login.LAST_KEY)  # past the 1 h gap
        out = await env.send("admin", "DELETE", f"{URL}/session")
        assert out.status_code == 200, out.text
        expired = s.model_copy(update={"DHAN_SESSION_TOKEN": None})
        async with httpx.AsyncClient(transport=httpx.MockTransport(auth)) as http:
            again = await dhan_auto_login.run(env.maker, expired, redis, http)
        assert again.outcome == "paused" and len(sent) == 1
