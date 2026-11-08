"""V1.6.1 A2: the MT5 ingest token is a single-purpose credential.

It authorizes POST /api/trading/mt5/ingest and nothing else; nothing else authorizes that
route; and it may never share its value with another credential (above all MT5_STATUS_TOKEN,
which the API sends out to the bot). The check holds at startup (ENV=prod), on every request
(the token can change at runtime through managed settings) and on a managed-settings write.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Environment, Settings
from app.core.prod_checks import (
    INGEST_TOKEN_DISTINCT_FROM,
    ingest_token_problems,
    production_problems,
)
from app.services import app_settings as svc
from tests.mt5_push_fixture import TOKEN, push_doc
from tests.test_mt5_ingest_unit import _ctx, _post, _settings

READS = (
    "/api/trading/mt5/bot-status",
    "/api/trading/mt5/market",
    "/api/trading/mt5/positions",
    "/api/trading/mt5/signals",
    "/api/trading/mt5/failures",
    "/api/trading/mt5/strategy",
    "/api/trading/mt5/report",
)


# --- the token authorizes exactly one route ----------------------------------------------


async def test_ingest_token_grants_no_session_route(settings: Settings) -> None:
    """A valid bot token is not a session: every dashboard read (and a session-only write)
    still answers 401 with it."""
    async for ctx in _ctx(_settings(settings, MT5_INGEST_TOKEN=TOKEN)):
        auth = {"authorization": f"Bearer {TOKEN}"}
        for path in READS:
            r = await ctx.c.get(path, headers=auth)
            assert r.status_code == 401, (path, r.status_code)
        r = await ctx.c.get("/api/auth/me", headers=auth)
        assert r.status_code == 401
        r = await ctx.c.post("/api/trading/order-intents", headers=auth, json={})
        assert r.status_code in (401, 403), r.status_code
        assert ctx.store.calls == 0


async def test_only_the_ingest_token_authorizes_ingest(settings: Settings) -> None:
    """Other configured credentials presented as the bearer are refused (401) and store
    nothing; the right token is accepted."""
    status_tok = "S" * 8 + "status-token-for-the-bot-endpoint-123"
    webhook = "W" * 8 + "github-webhook-secret-value-abcdefghij"
    s = _settings(
        settings,
        MT5_INGEST_TOKEN=TOKEN,
        MT5_STATUS_TOKEN=status_tok,
        GITHUB_WEBHOOK_SECRET=webhook,
    )
    async for ctx in _ctx(s):
        for other in (status_tok, webhook, s.SECRET_KEY.get_secret_value()):
            r = await _post(ctx.c, push_doc(), authorization=f"Bearer {other}")
            assert r.status_code == 401, r.text
        assert ctx.store.calls == 0
        r = await _post(ctx.c, push_doc())
        assert r.status_code == 202, r.text


# --- one value, one credential -------------------------------------------------------------


@pytest.mark.parametrize("name", ["MT5_STATUS_TOKEN", "GITHUB_WEBHOOK_SECRET", "SECRET_KEY"])
async def test_reused_token_fails_closed_at_request_time(settings: Settings, name: str) -> None:
    """A token shared with another credential disables ingest (503), even with the right
    bearer: whoever holds the other credential must not be able to push as the bot."""
    s = _settings(settings, MT5_INGEST_TOKEN=TOKEN, **{name: TOKEN})
    async for ctx in _ctx(s):
        r = await _post(ctx.c, push_doc())
        assert r.status_code == 503, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "ingest_misconfigured"
        assert name in detail["detail"] and TOKEN not in r.text
        assert ctx.store.calls == 0


async def test_runtime_weak_token_fails_closed_in_prod(settings: Settings) -> None:
    """The prod startup checklist cannot see a token changed later through managed settings;
    the route re-checks it. (Outside prod a short token keeps working, as before.)"""
    async for ctx in _ctx(_settings(settings, MT5_INGEST_TOKEN="short-but-set")):
        assert (await _post(ctx.c, push_doc(), authorization="Bearer short-but-set")).status_code
        live: Settings = ctx.app.state.settings
        live.ENV = Environment.PROD  # what an overlay of a managed value would leave behind
        r = await _post(ctx.c, push_doc(), authorization="Bearer short-but-set")
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "ingest_misconfigured"
        assert ctx.store.calls == 1  # only the pre-prod push was stored


@pytest.mark.parametrize("name", INGEST_TOKEN_DISTINCT_FROM)
def test_prod_checklist_rejects_reuse_of_every_named_credential(
    settings: Settings, name: str
) -> None:
    s = settings.model_copy()
    s.MT5_INGEST_TOKEN = SecretStr(TOKEN)
    value: Any = SecretStr(f"{TOKEN},other-master-key") if name == "SETTINGS_MASTER_KEY" else None
    setattr(s, name, value or SecretStr(TOKEN))
    problems = production_problems(s)
    assert any(name in p and "reuse" in p for p in problems), problems
    assert all(TOKEN not in p for p in problems)


def test_distinct_strong_token_has_no_problems(settings: Settings) -> None:
    s = _settings(settings, MT5_INGEST_TOKEN=TOKEN, MT5_STATUS_TOKEN="x" * 8 + TOKEN[::-1])
    assert ingest_token_problems(s) == []


# --- managed settings can no longer bypass the checks --------------------------------------


def test_managed_write_rejects_a_weak_ingest_token() -> None:
    spec = svc.REGISTRY["MT5_INGEST_TOKEN"]
    with pytest.raises(svc.SettingsError) as exc:
        svc.normalize(spec, "too-short-token")
    assert exc.value.code == "invalid_value" and "32" in exc.value.detail
    with pytest.raises(svc.SettingsError):
        svc.normalize(spec, "change-me-" + "a1b2c3d4" * 4)  # placeholder marker
    assert svc.normalize(spec, TOKEN) == TOKEN


def test_managed_write_rejects_a_reused_ingest_token(settings: Settings) -> None:
    env = _settings(settings, MT5_STATUS_TOKEN=TOKEN)
    with pytest.raises(svc.SettingsError) as exc:
        svc.checked_overlay(env, {"MT5_INGEST_TOKEN": TOKEN}, "MT5_INGEST_TOKEN")
    assert "MT5_STATUS_TOKEN" in exc.value.detail and TOKEN not in exc.value.detail
    # ...and the other direction: a managed GITHUB_TOKEN equal to the ingest token.
    env = _settings(settings, MT5_INGEST_TOKEN=TOKEN)
    with pytest.raises(svc.SettingsError):
        svc.checked_overlay(env, {"GITHUB_TOKEN": TOKEN}, "GITHUB_TOKEN")
    ok = svc.checked_overlay(env, {"GITHUB_TOKEN": "ghp_" + "z" * 36}, "GITHUB_TOKEN")
    assert ok.MT5_INGEST_TOKEN is not None


async def test_settings_test_button_uses_the_same_bar(settings: Settings) -> None:
    import httpx

    async with httpx.AsyncClient() as http:
        weak = await svc._mt5(_settings(settings, MT5_INGEST_TOKEN="x" * 24 + "abc"), http)
        assert weak.ok is False and "32" in weak.detail
        reused = await svc._mt5(
            _settings(settings, MT5_INGEST_TOKEN=TOKEN, MT5_STATUS_TOKEN=TOKEN), http
        )
        assert reused.ok is False and "MT5_STATUS_TOKEN" in reused.detail
        good = await svc._mt5(_settings(settings, MT5_INGEST_TOKEN=TOKEN), http)
        assert good.ok is True
