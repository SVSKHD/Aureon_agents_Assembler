"""Runtime settings and secrets without a database (docs/settings.md)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core import logging as app_logging
from app.core.config import Settings, get_settings, set_effective_settings
from app.integrations.claude_cli import DENIED_ENV_PREFIXES
from app.services import app_settings as svc

GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2
OR_KEY = "sk-or-v1-" + "f" * 40
DISCORD = "https://discord.com/api/webhooks/123456/abcDEF_secret-token"


@pytest.fixture
def env(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"SETTINGS_MASTER_KEY": SecretStr(Fernet.generate_key().decode())}
    )


# --- registry -----------------------------------------------------------------------------


def test_registry_keys_are_settings_fields_and_never_bootstrap() -> None:
    for key, spec in svc.REGISTRY.items():
        assert key in Settings.model_fields, key
        assert spec.category in svc.CATEGORIES
        assert key not in svc.BOOTSTRAP_KEYS
    for key in ("DATABASE_URL", "SECRET_KEY", "SETTINGS_MASTER_KEY", "REDIS_URL",
                "TRADING_LIVE_ENABLED", "PHASE_G_APPROVED"):  # fmt: skip
        assert key not in svc.REGISTRY


def test_every_dhan_field_the_code_reads_is_editable() -> None:
    dhan = {k for k in Settings.model_fields if k.startswith("DHAN_")}
    assert dhan <= set(svc.REGISTRY)


def test_secrets_and_credential_urls_need_reauth() -> None:
    for spec in svc.REGISTRY.values():
        if spec.secret or spec.kind == "url":
            assert spec.reauth, spec.key
    assert not svc.REGISTRY["OPENROUTER_DEFAULT_MODEL"].reauth


# --- encryption ---------------------------------------------------------------------------


def test_cipher_round_trip_and_ciphertext_is_not_the_value() -> None:
    c = svc.Cipher(Fernet.generate_key().decode())
    token = c.encrypt(GH)
    assert GH not in token
    assert c.decrypt(token) == GH


def test_rotation_new_key_first_still_reads_old_rows() -> None:
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = svc.Cipher(old).encrypt(GH)
    both = svc.Cipher(f"{new},{old}")
    rotated = both.rotate(token)
    assert svc.Cipher(new).decrypt(rotated) == GH


@pytest.mark.parametrize("key", [None, "", "not-a-fernet-key"])
def test_missing_or_bad_master_key_refuses(key: str | None) -> None:
    with pytest.raises(svc.SettingsError) as err:
        svc.Cipher(key)
    assert err.value.code in ("settings_key_missing", "settings_key_invalid")


# --- values -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "raw", "want"),
    [
        ("OPENROUTER_ENABLED", " Yes ", "true"),
        ("OPENROUTER_DAILY_LIMIT_USD", "2.50", "2.50"),
        ("OPENROUTER_ALLOWED_MODELS", " a/b , c/d ,", "a/b,c/d"),
        ("GITHUB_CODING_REPO_ALLOWLIST", '[" o/r "]', '["o/r"]'),
        ("GITHUB_CODING_REPO_ALLOWLIST", "", "[]"),
        ("DHAN_BASE_URL", "https://api.dhan.co/v2", "https://api.dhan.co/v2"),
        ("DHAN_FEED_QUEUE_MAX", "100", "100"),
        ("DISCORD_WEBHOOK_URL", DISCORD, DISCORD),
    ],
)
def test_normalize(key: str, raw: str, want: str) -> None:
    assert svc.normalize(svc.REGISTRY[key], raw) == want


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("OPENROUTER_ENABLED", "maybe"),
        ("OPENROUTER_DAILY_LIMIT_USD", "-1"),
        ("OPENROUTER_DAILY_LIMIT_USD", "NaN"),
        ("DHAN_BASE_URL", "http://evil.example/v2"),
        ("DHAN_FEED_QUEUE_MAX", "1.5"),
        ("GITHUB_CODING_REPO_ALLOWLIST", "o/r"),
        ("GITHUB_TOKEN", "   "),
        ("DISCORD_WEBHOOK_URL", "https://evil.example/api/webhooks/1/x"),
    ],
)
def test_normalize_refuses_without_echoing(key: str, raw: str) -> None:
    with pytest.raises(svc.SettingsError) as err:
        svc.normalize(svc.REGISTRY[key], raw)
    if raw.strip():
        assert raw.strip() not in err.value.detail


def test_overlay_db_wins_and_parses_like_env(env: Settings) -> None:
    base = env.model_copy(update={"GITHUB_TOKEN": SecretStr("ghp_from_env_value_123456")})
    out = svc.overlay(
        base,
        {
            "GITHUB_TOKEN": GH,
            "OPENROUTER_API_KEY": OR_KEY,
            "OPENROUTER_ENABLED": "true",
            "OPENROUTER_ALLOWED_MODELS": "a/b,c/d",
            "GITHUB_CODING_REPO_ALLOWLIST": '["o/r"]',
            "OPENROUTER_DAILY_LIMIT_USD": "2.5",
        },
    )
    assert out.GITHUB_TOKEN is not None and out.GITHUB_TOKEN.get_secret_value() == GH
    assert out.OPENROUTER_API_KEY is not None
    assert out.OPENROUTER_API_KEY.get_secret_value() == OR_KEY
    assert out.OPENROUTER_ENABLED is True
    assert out.OPENROUTER_ALLOWED_MODELS == ["a/b", "c/d"]
    assert out.GITHUB_CODING_REPO_ALLOWLIST == ["o/r"]
    assert str(out.OPENROUTER_DAILY_LIMIT_USD) == "2.5"
    # Untouched fields, including bootstrap ones, are the env's.
    assert out.SECRET_KEY.get_secret_value() == base.SECRET_KEY.get_secret_value()
    assert out.DATABASE_URL.get_secret_value() == base.DATABASE_URL.get_secret_value()
    assert out.REDIS_URL == base.REDIS_URL


def test_overlay_ignores_keys_outside_the_registry(env: Settings) -> None:
    out = svc.overlay(env, {"TRADING_LIVE_ENABLED": "true", "SECRET_KEY": "x" * 40})
    assert out.TRADING_LIVE_ENABLED is False
    assert out.SECRET_KEY.get_secret_value() == env.SECRET_KEY.get_secret_value()


def test_checked_overlay_names_the_field_not_the_value(env: Settings) -> None:
    with pytest.raises(svc.SettingsError) as err:
        svc.checked_overlay(env, {"DHAN_HTTP_TIMEOUT": "-5secret"}, "DHAN_HTTP_TIMEOUT")
    assert "DHAN_HTTP_TIMEOUT" in err.value.detail
    assert "secret" not in err.value.detail.replace("DHAN", "")


def test_mask_never_shows_more_than_the_last_four() -> None:
    assert svc.mask(GH, True) == "••••" + GH[-4:]
    assert svc.mask("short-secret", True) == "••••"
    assert svc.mask(None, True) is None
    assert svc.mask("true", False) == "••••"


def test_dhan_token_expiry_reads_the_jwt_exp() -> None:
    exp = int(datetime(2026, 11, 5, 3, 0, tzinfo=UTC).timestamp())
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    assert svc.dhan_token_expiry(f"h.{body}.s") == datetime(2026, 11, 5, 3, 0, tzinfo=UTC)
    assert svc.dhan_token_expiry("not-a-jwt") is None
    assert svc.dhan_token_expiry(None) is None


def test_changed_keys(env: Settings) -> None:
    new = svc.overlay(env, {"GITHUB_TOKEN": GH, "OPENROUTER_DEFAULT_MODEL": "x/y"})
    assert svc.changed_keys(env, new, ("GITHUB_",)) == ["GITHUB_TOKEN"]
    assert svc.changed_keys(env, new, ("DHAN_",)) == []


def test_install_sets_get_settings_and_registers_secrets(env: Settings) -> None:
    new = svc.overlay(env, {"GITHUB_TOKEN": GH})
    try:
        svc.install(new)
        assert get_settings() is new
        assert GH not in app_logging.scrub(f"failed with {GH} in the message")
    finally:
        set_effective_settings(None)
        app_logging.register_secret_values([])


# --- leaks --------------------------------------------------------------------------------


def test_scrubber_removes_known_values_and_discord_webhooks() -> None:
    odd = "zz-unshaped-credential-9876"
    app_logging.register_secret_values([odd])
    try:
        out = app_logging.scrub({"msg": f"x {odd} y", "url": DISCORD})
        assert odd not in json.dumps(out)
        assert "abcDEF_secret-token" not in json.dumps(out)
    finally:
        app_logging.register_secret_values([])


def test_claude_cli_never_gets_settings_secrets() -> None:
    for prefix in ("SETTINGS_", "OPENROUTER_", "DISCORD_", "GITHUB_", "DHAN_", "MT5_"):
        assert prefix in DENIED_ENV_PREFIXES


# --- connection tests ---------------------------------------------------------------------


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_github_test_ok_and_rejected_never_echo_the_token(env: Settings) -> None:
    s = svc.overlay(env, {"GITHUB_TOKEN": GH})
    seen: list[str] = []

    def ok(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers["authorization"])
        return httpx.Response(200, json={"login": "octo", "echo": GH})

    async with _client(httpx.MockTransport(ok)) as http:
        out = await svc.run_test("github", s, http)
    assert out.ok and "octo" in out.detail and GH not in out.detail
    assert seen == [f"Bearer {GH}"]

    def denied(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad credentials {GH}")

    async with _client(httpx.MockTransport(denied)) as http:
        out = await svc.run_test("github", s, http)
    assert not out.ok and GH not in out.detail


async def test_openrouter_test_checks_key_and_default_model(env: Settings) -> None:
    s = svc.overlay(env, {"OPENROUTER_API_KEY": OR_KEY, "OPENROUTER_DEFAULT_MODEL": "a/b"})

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(200, json={"data": [{"id": "x/y"}]})

    async with _client(httpx.MockTransport(handler)) as http:
        out = await svc.run_test("openrouter", s, http)
    assert not out.ok and "not an OpenRouter model" in out.detail and OR_KEY not in out.detail


async def test_dhan_and_discord_tests(env: Settings) -> None:
    s = svc.overlay(
        env,
        {"DHAN_CLIENT_ID": "1100001234", "DHAN_ACCESS_TOKEN": "tok-" + "x" * 30,
         "DISCORD_WEBHOOK_URL": DISCORD},
    )  # fmt: skip
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json={})

    async with _client(httpx.MockTransport(handler)) as http:
        assert (await svc.run_test("dhan", s, http)).ok
        assert (await svc.run_test("discord", s, http)).ok
    assert ("GET", "/v2/fundlimit") in calls or any(p.endswith("/fundlimit") for _, p in calls)
    # The Discord test reads the webhook; it never posts a message.
    assert all(m == "GET" for m, _ in calls)


async def test_unset_credentials_fail_without_a_request(env: Settings) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    async with _client(httpx.MockTransport(boom)) as http:
        # "agents" holds no credential: its test only asks Ollama which models are installed.
        for category in (c for c in svc.CATEGORIES if c != "agents"):
            assert not (await svc.run_test(category, env, http)).ok


async def test_agents_test_reports_missing_local_models(env: Settings) -> None:
    def tags(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]})

    async with _client(httpx.MockTransport(tags)) as http:
        assert (await svc.run_test("agents", env, http)).ok
        s = env.model_copy(update={"AGENT_VOICE_LOCAL_MODEL": "llama3.2:1b"})
        out = await svc.run_test("agents", s, http)
        assert not out.ok and "llama3.2:1b" in out.detail


async def test_discord_payload_is_scrubbed_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations import discord

    secret = "zz-db-sourced-secret-4242"
    app_logging.register_secret_values([secret])
    sent: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req.content)
        return httpx.Response(204)

    real = httpx.AsyncClient

    def fake(*a: object, **kw: object) -> httpx.AsyncClient:
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(discord.httpx, "AsyncClient", fake)
    try:
        ok = await discord.post(
            DISCORD, {"content": f"boom {secret}"}, attempts=1, request_seconds=1,
            retry_base_seconds=0,
        )  # fmt: skip
    finally:
        app_logging.register_secret_values([])
    assert ok and sent and secret.encode() not in sent[0]


def test_prompt_scrub_keeps_ordinary_text() -> None:
    app_logging.register_secret_values(["zz-unshaped-credential-9876"])
    try:
        msgs = [{"role": "user", "content": "hello zz-unshaped-credential-9876 world"},
                {"role": "assistant", "content": "plain"}]  # fmt: skip
        out = app_logging.scrub_messages(msgs)
        assert "zz-unshaped" not in out[0]["content"] and "hello" in out[0]["content"]
        assert out[1] == msgs[1] and out[0]["role"] == "user"
    finally:
        app_logging.register_secret_values([])
