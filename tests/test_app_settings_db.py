"""/api/v1/settings against Postgres + Redis (docs/settings.md).

What must hold: admin only, CSRF, password re-auth for secrets, values never in a response,
an error, an audit row or the table's plaintext, DB over .env with revert, live reload, and
rate limits.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.api.v1 import app_settings as api
from app.core.config import Settings, get_settings, set_effective_settings
from app.core.logging import configure_logging, register_secret_values, scrub_messages
from app.services import app_settings as svc
from tests import test_finance_db
from tests.test_finance_db import PASSWORD, Env, EnvFactory

pytestmark = pytest.mark.db

MASTER = Fernet.generate_key().decode()
ENV_TOKEN = "ghp_" + "EnvTokenValue000" * 2
GH = "ghp_" + "DbTokenValue1111" * 2
URL = "/api/v1/settings"
make_env = test_finance_db.make_env  # the admin + users + CSRF harness, as a fixture here


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "SETTINGS_MASTER_KEY": SecretStr(MASTER),
            "GITHUB_TOKEN": SecretStr(ENV_TOKEN),
        }
    )


@pytest.fixture(autouse=True)
def _reset_process_settings() -> Any:
    yield
    set_effective_settings(None)
    register_secret_values([])


def _item(body: dict[str, Any], key: str) -> dict[str, Any]:
    return next(s for s in body["settings"] if s["key"] == key)


async def _audit(env: Env) -> str:
    rows = await env.sql("SELECT event_type, details::text FROM audit.audit_events")
    return json.dumps([list(r) for r in rows])


async def test_list_is_admin_only_and_shows_no_values(make_env: EnvFactory) -> None:
    async with make_env() as env:
        assert (await env.get("a", URL)).status_code == 403
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=env.app), base_url="https://test"
        ) as anon:
            assert (await anon.get(URL)).status_code == 401
        r = await env.get("admin", URL)
        assert r.status_code == 200
        assert ENV_TOKEN not in r.text and MASTER not in r.text
        body = r.json()
        assert body["encryption_ready"] is True
        gh = _item(body, "GITHUB_TOKEN")
        assert gh["source"] == "env" and gh["is_set"] and gh["masked"] == "••••" + ENV_TOKEN[-4:]
        assert _item(body, "ANTHROPIC_API_KEY")["source"] == "unset"
        assert {s["key"] for s in body["settings"]} == set(svc.REGISTRY)


async def test_writes_need_csrf(make_env: EnvFactory) -> None:
    async with make_env() as env:
        r = await env.clients["admin"].put(
            f"{URL}/GITHUB_TOKEN", json={"value": GH, "password": PASSWORD}
        )
        assert r.status_code == 403
        assert (await env.send("a", "PUT", f"{URL}/AI_MODEL_MAIN",
                               json={"value": "x/y"})).status_code == 403  # fmt: skip


async def test_secret_needs_password_and_failures_are_audited(make_env: EnvFactory) -> None:
    async with make_env() as env:
        for body in ({"value": GH}, {"value": GH, "password": "wrong"}):
            r = await env.send("admin", "PUT", f"{URL}/GITHUB_TOKEN", json=body)
            assert r.status_code == 403 and r.json()["detail"]["code"] == "reauth_failed"
            assert GH not in r.text
        events = await _audit(env)
        assert events.count("settings_reauth_failed") == 2
        assert GH not in events
        assert (await env.sql("SELECT count(*) FROM core.app_settings"))[0][0] == 0


async def test_reauth_failures_lock_out(make_env: EnvFactory) -> None:
    async with make_env() as env:
        for _ in range(api.MAX_REAUTH_FAILURES):
            await env.send(
                "admin", "PUT", f"{URL}/GITHUB_TOKEN", json={"value": GH, "password": "x"}
            )
        r = await env.send(
            "admin", "PUT", f"{URL}/GITHUB_TOKEN", json={"value": GH, "password": PASSWORD}
        )
        assert r.status_code == 429


async def test_set_secret_overrides_env_live_then_revert(make_env: EnvFactory) -> None:
    async with make_env() as env:
        r = await env.send(
            "admin", "PUT", f"{URL}/GITHUB_TOKEN", json={"value": GH, "password": PASSWORD}
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["source"] == "db" and out["version"] == 1 and out["masked"] == "••••" + GH[-4:]
        assert GH not in r.text
        # Stored encrypted, decryptable with the master key.
        (row,) = await env.sql("SELECT value_encrypted, is_secret, category FROM core.app_settings")
        assert GH not in row[0] and row[1] is True and row[2] == "github"
        assert svc.Cipher(MASTER).decrypt(row[0]) == GH
        # Live in this process without a restart, client rebuilt with the new token.
        live = env.app.state.settings
        assert live.GITHUB_TOKEN.get_secret_value() == GH
        assert get_settings() is live
        # Replace bumps the version.
        r = await env.send(
            "admin", "PUT", f"{URL}/GITHUB_TOKEN", json={"value": GH + "x", "password": PASSWORD}
        )
        assert r.json()["version"] == 2
        # Revert: needs the password too, then .env applies again.
        assert (await env.send("admin", "DELETE", f"{URL}/GITHUB_TOKEN")).status_code == 403
        r = await env.send("admin", "DELETE", f"{URL}/GITHUB_TOKEN", json={"password": PASSWORD})
        assert r.status_code == 200 and r.json()["source"] == "env"
        assert env.app.state.settings.GITHUB_TOKEN.get_secret_value() == ENV_TOKEN
        assert (await env.send("admin", "DELETE", f"{URL}/GITHUB_TOKEN",
                               json={"password": PASSWORD})).status_code == 404  # fmt: skip
        events = await _audit(env)
        assert events.count("settings_updated") == 2 and "settings_reverted" in events
        assert GH not in events and ENV_TOKEN not in events


async def test_plain_setting_needs_no_password(make_env: EnvFactory) -> None:
    async with make_env() as env:
        r = await env.send(
            "admin", "PUT", f"{URL}/DHAN_INSTRUMENT_SEGMENTS", json={"value": "NSE_EQ, IDX_I"}
        )
        assert r.status_code == 200, r.text
        assert env.app.state.settings.DHAN_INSTRUMENT_SEGMENTS == ["NSE_EQ", "IDX_I"]


async def test_bootstrap_unknown_and_invalid_keys(make_env: EnvFactory) -> None:
    async with make_env() as env:
        for key in ("DATABASE_URL", "SECRET_KEY", "SETTINGS_MASTER_KEY", "TRADING_LIVE_ENABLED"):
            r = await env.send(
                "admin", "PUT", f"{URL}/{key}", json={"value": "x" * 40, "password": PASSWORD}
            )
            assert r.status_code == 403 and r.json()["detail"]["code"] == "bootstrap_key"
        r = await env.send("admin", "PUT", f"{URL}/NOPE", json={"value": "x"})
        assert r.status_code == 404
        r = await env.send(
            "admin", "PUT", f"{URL}/DHAN_BASE_URL",
            json={"value": "http://leak.example/secretpath", "password": PASSWORD},
        )  # fmt: skip
        assert r.status_code == 422 and "secretpath" not in r.text
        assert (await env.sql("SELECT count(*) FROM core.app_settings"))[0][0] == 0


async def test_connection_test_returns_outcome_only(make_env: EnvFactory) -> None:
    seen: list[str] = []

    def github(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("authorization", ""))
        return httpx.Response(200, json={"login": "octo", "token": ENV_TOKEN})

    async with make_env() as env:
        env.app.state.settings_test_transport = httpx.MockTransport(github)
        r = await env.send("admin", "POST", f"{URL}/GITHUB_TOKEN/test")
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True and ENV_TOKEN not in r.text
        assert seen == [f"Bearer {ENV_TOKEN}"]
        listed = (await env.get("admin", URL)).json()
        assert listed["tests"]["github"]["ok"] is True
        assert "settings_tested" in await _audit(env)


async def test_writes_are_rate_limited(make_env: EnvFactory, monkeypatch: Any) -> None:
    monkeypatch.setattr(api, "WRITES_PER_10_MIN", 2)
    async with make_env() as env:
        codes = [
            (
                await env.send("admin", "PUT", f"{URL}/AI_MODEL_MAIN", json={"value": f"m/{i}"})
            ).status_code
            for i in range(3)
        ]
        assert codes == [200, 200, 429]


async def test_other_processes_hear_the_change(make_env: EnvFactory) -> None:
    async with make_env() as env:
        heard = asyncio.Event()
        stop = asyncio.Event()

        async def on_change() -> bool:
            heard.set()
            return False

        watcher = asyncio.create_task(
            svc.watch(env.maker, env.app.state.redis, on_change, stop, poll_seconds=3600)
        )
        await asyncio.sleep(0.3)  # subscribed
        await env.send("admin", "PUT", f"{URL}/AI_MODEL_MAIN", json={"value": "x/y"})
        await asyncio.wait_for(heard.wait(), 10)
        stop.set()
        await asyncio.wait_for(watcher, 10)


async def test_worker_reload_only_for_its_keys(make_env: EnvFactory, settings: Settings) -> None:
    async with make_env() as env:
        stop = asyncio.Event()
        svc.RELOAD.requested = False
        task = svc.start_worker_watch(
            settings, env.maker, stop, ("DHAN_",), env.app.state.redis, env=settings
        )
        await asyncio.sleep(0.3)
        try:
            await env.send("admin", "PUT", f"{URL}/AI_MODEL_MAIN", json={"value": "a/b"})
            await asyncio.sleep(1.0)
            assert not stop.is_set()
            r = await env.send("admin", "PUT", f"{URL}/DHAN_FEED_QUEUE_MAX", json={"value": "250"})
            assert r.status_code == 200
            await asyncio.wait_for(stop.wait(), 10)
            assert svc.RELOAD.requested
        finally:
            stop.set()
            svc.RELOAD.requested = False
            await asyncio.wait_for(task, 10)


# --- acceptance checks (docs/settings.md) ------------------------------------------------

AN_KEY = "sk-ant-api03-" + "f00dfeed" * 8
WEBHOOK = "https://discord.com/api/webhooks/123456789/" + "WebhookSecretPart_" * 3
ALLOWED_FIELDS = {
    "key", "category", "description", "kind", "is_secret", "requires_reauth", "is_set",
    "masked", "source", "updated_at", "version", "expires_at", "expires_soon",
    "managed",  # a key only its own flow writes (Dhan login); never a value
}  # fmt: skip


async def _put(env: Any, key: str, value: str) -> Any:
    return await env.send(
        "admin", "PUT", f"{URL}/{key}", json={"value": value, "password": PASSWORD}
    )


async def test_get_returns_masks_never_values(make_env: EnvFactory) -> None:
    async with make_env() as env:
        for key, value in (("GITHUB_TOKEN", GH), ("ANTHROPIC_API_KEY", AN_KEY),
                           ("DISCORD_WEBHOOK_URL", WEBHOOK)):  # fmt: skip
            assert (await _put(env, key, value)).status_code == 200
        r = await env.get("admin", URL)
        assert r.status_code == 200
        for secret in (GH, AN_KEY, WEBHOOK, ENV_TOKEN, MASTER, "WebhookSecretPart_"):
            assert secret not in r.text
        for item in r.json()["settings"]:
            assert set(item) <= ALLOWED_FIELDS, set(item) - ALLOWED_FIELDS
            if item["is_secret"] and item["masked"]:
                assert item["masked"].startswith("••••") and len(item["masked"]) == 8
        assert _item(r.json(), "GITHUB_TOKEN")["source"] == "db"


async def test_every_bootstrap_key_is_refused(make_env: EnvFactory) -> None:
    keys = ("DATABASE_URL", "SECRET_KEY", "SETTINGS_MASTER_KEY", "REDIS_URL",
            "TRADING_LIVE_ENABLED", "PHASE_G_APPROVED")  # fmt: skip
    assert set(keys) <= svc.BOOTSTRAP_KEYS
    async with make_env() as env:
        for key in keys:
            r = await _put(env, key, "yes" if key == "PHASE_G_APPROVED" else "x" * 40)
            assert r.status_code == 403 and r.json()["detail"]["code"] == "bootstrap_key", key
            r = await env.send("admin", "DELETE", f"{URL}/{key}", json={"password": PASSWORD})
            assert r.status_code == 403, key
        assert (await env.sql("SELECT count(*) FROM core.app_settings"))[0][0] == 0


async def test_audit_rows_have_key_actor_time_and_no_value(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await _put(env, "GITHUB_TOKEN", GH)
        await env.send("admin", "PUT", f"{URL}/AI_MODEL_MAIN", json={"value": "m/x"})
        await env.send("admin", "DELETE", f"{URL}/GITHUB_TOKEN", json={"password": PASSWORD})
        rows = await env.sql(
            "SELECT event_type, actor_user_id, occurred_at, details::text FROM audit.audit_events"
            " WHERE event_type LIKE 'settings_%' ORDER BY id"
        )
        assert [r[0] for r in rows] == ["settings_updated", "settings_updated", "settings_reverted"]
        for event, actor, at, details in rows:
            assert actor == env.ids["admin"] and at is not None, event
            assert json.loads(details)["key"] in ("GITHUB_TOKEN", "AI_MODEL_MAIN")
            assert GH not in details and "m/x" not in details and "value" not in json.loads(details)


async def test_plain_values_are_encrypted_at_rest_too(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.send("admin", "PUT", f"{URL}/AI_MODEL_MAIN", json={"value": "vendor/m"})
        await _put(env, "GITHUB_TOKEN", GH)
        rows = await env.sql("SELECT key, value_encrypted::text FROM core.app_settings")
        raw = json.dumps([list(r) for r in rows])
        assert GH not in raw and "vendor/m" not in raw
        for _key, blob in rows:
            assert blob.startswith("gAAAA")  # a Fernet token, not the value


async def test_no_secret_in_logs_errors_discord_or_prompts(
    make_env: EnvFactory, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    from app.integrations import discord

    configure_logging("DEBUG")
    sent: list[bytes] = []

    def upstream(req: httpx.Request) -> httpx.Response:
        # A hostile upstream that echoes the credential back in its error body.
        if "discord.com" in str(req.url):
            sent.append(req.content)
            return httpx.Response(204)
        raise httpx.ConnectError(f"refused, auth was {req.headers.get('authorization')}")

    real = httpx.AsyncClient

    def fake(*a: Any, **kw: Any) -> httpx.AsyncClient:
        kw["transport"] = httpx.MockTransport(upstream)
        return real(*a, **kw)

    async with make_env() as env:
        env.app.state.settings_test_transport = httpx.MockTransport(upstream)
        bodies = []
        for password in ("wrong", PASSWORD):
            r = await env.send("admin", "PUT", f"{URL}/GITHUB_TOKEN",
                               json={"value": GH, "password": password})  # fmt: skip
            bodies.append(r.text)
        bodies.append((await _put(env, "ANTHROPIC_API_KEY", AN_KEY)).text)
        bodies.append((await _put(env, "DISCORD_WEBHOOK_URL", WEBHOOK)).text)
        bodies.append((await _put(env, "DHAN_BASE_URL", f"http://x/{GH}")).text)  # invalid
        bodies.append((await env.send("admin", "POST", f"{URL}/GITHUB_TOKEN/test")).text)
        bodies.append((await env.send("admin", "POST", f"{URL}/DISCORD_WEBHOOK_URL/test")).text)
        bodies.append((await env.get("admin", URL)).text)

        monkeypatch.setattr(discord.httpx, "AsyncClient", fake)
        await discord.post(WEBHOOK, {"content": f"leak? {GH} {AN_KEY}"}, attempts=1,
                           request_seconds=1, retry_base_seconds=0)  # fmt: skip
        prompt = scrub_messages([{"role": "user", "content": f"key {AN_KEY} and {GH}"}])

        import structlog

        structlog.get_logger("verify").error("boom", detail=f"{GH} {AN_KEY} {WEBHOOK}")

    out, err = capfd.readouterr()
    haystacks = {
        "logs": out + err + caplog.text,
        "responses": "\n".join(bodies),
        "discord": b"".join(sent).decode(),
        "prompt": json.dumps(prompt),
    }
    assert sent, "the Discord post was not attempted"
    assert '"boom"' in haystacks["logs"], "logging was not captured"
    for where, text in haystacks.items():
        for secret in (GH, AN_KEY, WEBHOOK, "WebhookSecretPart_", MASTER):
            assert secret not in text, f"secret in {where}"
    register_secret_values([])


async def test_diagnostics_layers_and_reload(make_env: EnvFactory) -> None:
    async with make_env() as env:
        assert (await env.get("a", f"{URL}/diagnostics")).status_code == 403
        assert (await _put(env, "ANTHROPIC_API_KEY", AN_KEY)).status_code == 200
        # A row written under another master key, as after a SETTINGS_MASTER_KEY change.
        other = svc.Cipher(Fernet.generate_key().decode()).encrypt(GH)
        await env.sql(
            "UPDATE core.app_settings SET value_encrypted = :v WHERE key = 'ANTHROPIC_API_KEY'",
            v=other,
        )
        r = await env.get("admin", f"{URL}/diagnostics")
        assert r.status_code == 200
        for secret in (AN_KEY, GH, ENV_TOKEN, MASTER):
            assert secret not in r.text
        body = r.json()
        assert body["database_ok"] and body["encryption_ready"] and body["master_keys"] == 1
        assert body["undecryptable"] == ["ANTHROPIC_API_KEY"]
        k = next(i for i in body["keys"] if i["key"] == "ANTHROPIC_API_KEY")
        assert "db_undecryptable" in k["issues"] and "stale" in k["issues"]
        assert k["live_masked"] == "••••" + AN_KEY[-4:] and k["db_masked"] is None
        assert body["ai"]["configured"] and body["ai"]["matches_settings"]
        # The list no longer calls an unreadable row "db".
        listed = _item((await env.get("admin", URL)).json(), "ANTHROPIC_API_KEY")
        assert listed["source"] == "unset"

        assert (await env.send("a", "POST", f"{URL}/reload")).status_code == 403
        r = await env.send("admin", "POST", f"{URL}/reload")
        assert r.status_code == 200 and r.json()["changed"] == ["ANTHROPIC_API_KEY"]
        assert env.app.state.settings.anthropic_key == ""
        assert not env.app.state.ai.configured
        assert "settings_reloaded" in await _audit(env)
