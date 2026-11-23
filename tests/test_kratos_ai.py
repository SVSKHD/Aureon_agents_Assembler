"""The KRATOS research AI route (app/api/v1/kratos_ai.py). No network: a scripted fake client
stands in for the SDK, an in-memory store for the usage table and a fake Redis for the limiter."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.agents.trading.ratelimit import RateLimitExceeded
from app.ai.budget import MemoryUsageStore
from app.ai.gateway import ClaudeGateway
from app.api.router import api_router
from app.api.v1 import kratos_ai
from app.core.config import AI_AGENT_BUDGET_DEFAULTS, Settings
from tests.fake_claude import FakeClaude, text_reply


class FakeLimiter:
    """Stands in for the Redis-scripted limiter: allows `allow` calls, then refuses."""

    def __init__(self, allow: int = 10**6, broken: bool = False) -> None:
        self.allow, self.broken = allow, broken

    async def acquire(self, name: str, max_wait: float = 0) -> None:
        if self.broken:
            raise ConnectionError("redis down")
        if self.allow <= 0:
            raise RateLimitExceeded(name, 7.0)
        self.allow -= 1


URL = "/api/v1/kratos/ai/complete"
TOKEN = "kratos-route-test-token-0123456789-abcdefghij"
KEY = "sk-ant-api03-" + "k" * 40
BODY = {"role": "risk_stats", "system": "You are a test.", "user": "hello", "max_tokens": 500}
MODEL = "claude-haiku-5-5"  # stands in for whatever the owner selects in KRATOS_AI_MODEL


def _app(
    settings: Settings, fake: FakeClaude | None, **kw: Any
) -> tuple[FastAPI, MemoryUsageStore]:
    s = settings.model_copy(
        update={
            "ANTHROPIC_API_KEY": SecretStr(KEY),
            "KRATOS_AI_TOKEN": SecretStr(TOKEN),
            "KRATOS_AI_MODEL": MODEL,
            "AI_MODEL_MAIN": "claude-opus-5-5",  # must never be used for KRATOS
            "AI_AGENT_BUDGETS_USD": {**AI_AGENT_BUDGET_DEFAULTS, "kratos": Decimal("5")},
            **kw,
        }
    )
    app = FastAPI()
    app.include_router(kratos_ai.router, prefix="/api")
    store = MemoryUsageStore()
    app.state.settings = s
    app.state.redis = None
    app.state.kratos_ai_limiter = FakeLimiter()
    app.state.ai = ClaudeGateway(s, store, client=fake) if fake is not None else None
    return app, store


def _system_text(call: dict[str, Any]) -> str:
    system = call["system"]
    return system if isinstance(system, str) else "".join(b["text"] for b in system)


async def _post(app: FastAPI, body: dict[str, Any] | None = None, token: str | None = TOKEN) -> Any:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        return await c.post(URL, json=body or BODY, headers=headers)


def test_route_is_mounted_and_kratos_budget_defaults_to_zero() -> None:
    app = FastAPI()
    app.include_router(api_router)
    assert "post" in app.openapi()["paths"][URL]
    assert AI_AGENT_BUDGET_DEFAULTS["kratos"] == Decimal("0")
    assert Settings.model_fields["KRATOS_AI_TOKEN"].default is None


async def test_answers_through_the_gateway_on_the_kratos_budget(settings: Settings) -> None:
    fake = FakeClaude([text_reply('{"summary": "ok"}', input_tokens=120, output_tokens=30)])
    app, store = _app(settings, fake)
    r = await _post(app)
    assert r.status_code == 200, r.text
    d = r.json()
    assert (
        d["text"] == '{"summary": "ok"}'
        and d["budget"] == "kratos"
        and d["model_selected"] == MODEL
    )
    assert d["usage"]["input_tokens"] == 120 and d["usage"]["output_tokens"] == 30
    assert Decimal(d["cost_usd"]) > 0
    call = fake.calls[0]
    assert call["model"] == MODEL  # KRATOS_AI_MODEL: chosen by the owner, never by the caller
    assert "tools" not in call and call["messages"] == [{"role": "user", "content": "hello"}]
    assert call["max_tokens"] == 500
    spend = await store.spend()
    assert spend.by_budget.get("kratos", Decimal(0)) > 0 and set(spend.by_budget) == {"kratos"}


async def test_the_caller_cannot_name_a_model_or_send_anything_else(settings: Settings) -> None:
    fake = FakeClaude([text_reply("x")])
    app, _ = _app(settings, fake)
    for extra in (
        {"model": "claude-opus-5-5"},
        {"tools": []},
        {"budget": "chat"},
        {"tier": "main"},
    ):
        r = await _post(app, {**BODY, **extra})
        assert r.status_code == 422, extra
    assert (await _post(app, {**BODY, "role": "Risk Stats; DROP"})).status_code == 422
    assert fake.calls == []


async def test_auth_off_weak_reused_and_wrong_tokens(settings: Settings) -> None:
    fake = FakeClaude([text_reply("x")])
    app, _ = _app(settings, fake, KRATOS_AI_TOKEN=None)
    r = await _post(app)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "kratos_ai_disabled"
    app, _ = _app(settings, fake, KRATOS_AI_TOKEN=SecretStr("short"))
    assert (await _post(app, token="short")).json()["detail"]["code"] == "kratos_ai_misconfigured"
    app, _ = _app(settings, fake, KRATOS_AI_TOKEN=SecretStr(KEY))  # reuses the Anthropic key
    r = await _post(app, token=KEY)
    assert (
        r.status_code == 503
        and "ANTHROPIC_API_KEY" in r.json()["detail"]["message"]
        and KEY not in r.text
    )
    app, _ = _app(settings, fake)
    for bad in (None, "wrong-token-wrong-token-wrong-token-0000", TOKEN[:-1]):
        r = await _post(app, token=bad)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        r = await c.post(URL, json=BODY, headers={"Authorization": f"Basic {TOKEN}"})
    assert r.status_code == 401 and fake.calls == []


async def test_zero_or_exhausted_budget_refuses_before_any_call(settings: Settings) -> None:
    fake = FakeClaude([text_reply("x")])
    app, _ = _app(settings, fake, AI_AGENT_BUDGETS_USD=dict(AI_AGENT_BUDGET_DEFAULTS))  # kratos = 0
    r = await _post(app)
    assert (
        r.status_code == 402
        and r.json()["detail"]["code"] == "budget_exceeded"
        and fake.calls == []
    )
    app, _ = _app(settings, fake, AI_MONTHLY_BUDGET_USD=Decimal("0"))
    assert (await _post(app)).status_code == 402 and fake.calls == []


async def test_size_rate_and_availability_limits(settings: Settings) -> None:
    fake = FakeClaude([text_reply("a"), text_reply("b"), text_reply("c")])
    app, _ = _app(settings, fake, KRATOS_AI_MAX_REQUEST_CHARS=1_000)
    assert (await _post(app, {**BODY, "user": "x" * 2_000})).status_code == 413 and fake.calls == []
    app, _ = _app(settings, fake, KRATOS_AI_MAX_OUTPUT_TOKENS=300)
    app.state.kratos_ai_limiter = FakeLimiter(allow=2)
    assert (await _post(app)).status_code == 200 and (await _post(app)).status_code == 200
    assert fake.calls[0]["max_tokens"] == 300  # the route's ceiling wins over the caller's request
    r = await _post(app)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and len(fake.calls) == 2
    app, _ = _app(settings, fake)
    app.state.kratos_ai_limiter = FakeLimiter(
        broken=True
    )  # a paid route fails closed when the limiter is down
    assert (await _post(app)).json()["detail"]["code"] == "rate_limiter_unavailable"
    app, _ = _app(settings, None)
    assert (await _post(app)).json()["detail"]["code"] == "ai_not_configured"
    assert len(fake.calls) == 2


async def test_model_is_the_owners_selection_with_no_default_and_no_fallback(
    settings: Settings,
) -> None:
    fake = FakeClaude(
        [
            text_reply("a", model="claude-sonnet-5-5"),
            text_reply("b", model="claude-sonnet-5-5-20261101"),
        ]
    )
    app, _ = _app(settings, fake, KRATOS_AI_MODEL="")
    r = await _post(app)  # nothing selected: refused, AI_MODEL_MAIN is NOT used instead
    assert (
        r.status_code == 503
        and r.json()["detail"]["code"] == "kratos_model_not_selected"
        and fake.calls == []
    )
    app, _ = _app(settings, fake, KRATOS_AI_MODEL="claude-not-a-real-model")
    r = await _post(app)  # unknown / unpriced model: refused before any call, never replaced
    assert (
        r.status_code == 503
        and r.json()["detail"]["code"] == "model_not_priced"
        and fake.calls == []
    )
    app, _ = _app(settings, fake, KRATOS_AI_MODEL="claude-sonnet-5-5")
    d = (await _post(app)).json()
    assert (
        fake.calls[0]["model"] == "claude-sonnet-5-5"
        and d["model"] == d["model_selected"] == "claude-sonnet-5-5"
    )
    d = (await _post(app)).json()  # the response reports what the API actually served
    assert d["model_selected"] == "claude-sonnet-5-5" and d["model"] == "claude-sonnet-5-5-20261101"
    assert Settings.model_fields["KRATOS_AI_MODEL"].default == ""


def test_every_process_holding_a_gateway_reloads_on_a_policy_change(settings: Settings) -> None:
    """A gateway reads the caps from the settings it was built with. A process that kept the old
    policy would refuse KRATOS (API) or charge KRATOS spend to the shared cap (workers)."""
    from app.services import app_settings as svc
    from app.workers import research, scheduler

    new = settings.model_copy(update={"KRATOS_AI_BUDGET_POLICY": "metered"})
    for prefixes in (research.RELOAD_PREFIXES, scheduler.RELOAD_PREFIXES):
        assert svc.changed_keys(settings, new, prefixes) == ["KRATOS_AI_BUDGET_POLICY"]
        assert {"ANTHROPIC_", "AI_"} <= set(prefixes)  # what they reloaded on before
    quiet = settings.model_copy(update={"KRATOS_AI_MODEL": "some-model"})
    assert svc.changed_keys(settings, quiet, scheduler.RELOAD_PREFIXES) == []  # no worker restart


async def test_evidence_reaches_the_model_exactly_as_sent(settings: Settings) -> None:
    """The personal-data rules would rewrite numeric evidence; this route sends it whole."""
    from app.ai.redact import redact

    evidence = (
        '{"D:t0.stop_initial":{"value":3989.5950567873733},"bars":"3345 3346 3350",'
        '"ticket":4111111111111111,"note":"desk: 98765 43210, see a.b@example.com"}'
    )
    system = "Assess the evidence. Mr. Stop Loss is not a person. 4000 4100 4200 are prices."
    assert redact(evidence) != evidence and redact(system) != system  # the default would edit both
    fake = FakeClaude([text_reply("ok")])
    app, _ = _app(settings, fake)
    r = await _post(app, {**BODY, "system": system, "user": evidence})
    assert r.status_code == 200, r.text
    assert fake.calls[0]["messages"] == [{"role": "user", "content": evidence}]
    assert _system_text(fake.calls[0]) == system


@pytest.mark.parametrize(
    "leak",
    [
        "sk-ant-api03-" + "z" * 40,
        "Authorization: Bearer abcdefghijklmnop0123456789",
        "postgresql://kratos:hunter2secret@db:5432/kratos",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk",
    ],
)
async def test_credential_shaped_text_is_refused_before_any_call(
    settings: Settings, leak: str
) -> None:
    fake = FakeClaude([text_reply("never sent")])
    for field in ("system", "user"):
        app, store = _app(settings, fake)
        r = await _post(app, {**BODY, field: f"evidence line\n{leak}\nmore evidence"})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "credential_like_content"
        assert leak not in r.text and fake.calls == [] and (await store.spend()).total == 0


async def test_metered_policy_records_usage_without_a_ceiling_and_spares_the_shared_cap(
    settings: Settings,
) -> None:
    fake = FakeClaude(
        [text_reply("x", input_tokens=200_000, output_tokens=8_000) for _ in range(4)]
    )
    base = dict(
        AI_AGENT_BUDGETS_USD=dict(AI_AGENT_BUDGET_DEFAULTS), AI_MONTHLY_BUDGET_USD=Decimal("0.05")
    )
    app, _ = _app(settings, fake, **base)  # default policy "capped": kratos cap is 0
    assert (await _post(app)).status_code == 402 and fake.calls == []
    app, store = _app(settings, fake, KRATOS_AI_BUDGET_POLICY="metered", **base)
    for _ in range(3):  # no kratos cap, and far past the 0.05 monthly cap: still served
        assert (await _post(app)).status_code == 200
    spend = await store.spend()
    assert spend.by_budget["kratos"] > Decimal("0.05")  # ... and every call is still accounted
    s = app.state.settings
    from app.ai.budget import refusal

    assert refusal(s, spend, "kratos", Decimal("100")) is None
    # the shared cap still protects everyone else, and KRATOS spend has not used any of it
    assert refusal(s, spend, "chat", Decimal("0.04")) is None
    assert refusal(s, spend, "chat", Decimal("0.06")) == ("monthly", Decimal("0.05"), Decimal("0"))
    from app.ai.budget import shared_total

    assert shared_total(s, spend) == Decimal(
        "0"
    )  # status cards and /api/ai/usage compare THIS with the cap
    capped = s.model_copy(update={"KRATOS_AI_BUDGET_POLICY": "capped"})
    assert shared_total(capped, spend) == spend.total  # default policy: nothing changes
    assert (
        refusal(capped, spend, "chat", Decimal("0.01"))[0] == "monthly"
    )  # default: counted as before
    assert Settings.model_fields["KRATOS_AI_BUDGET_POLICY"].default == "capped"


async def test_config_endpoint_reports_what_a_call_would_run_with(settings: Settings) -> None:
    fake = FakeClaude([], models=[MODEL, "claude-opus-5-5"])
    app, _ = _app(settings, fake)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        assert (await c.get("/api/v1/kratos/ai/config")).status_code == 401
        h = {"Authorization": f"Bearer {TOKEN}"}
        d = (await c.get("/api/v1/kratos/ai/config", headers=h)).json()
        assert (
            d["model_selected"] == MODEL
            and d["model_priced"] is True
            and d["model_available"] is None
        )
        assert (
            d["budget_policy"] == "capped"
            and d["kratos_cap_usd"] == "5"
            and d["claude_configured"] is True
        )
        d = (await c.get("/api/v1/kratos/ai/config?probe=true", headers=h)).json()
        assert d["model_available"] is True and fake.calls == []  # the probe spends no tokens
    app, _ = _app(
        settings, fake, KRATOS_AI_MODEL="claude-sonnet-5-5", KRATOS_AI_BUDGET_POLICY="metered"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        d = (
            await c.get(
                "/api/v1/kratos/ai/config?probe=true", headers={"Authorization": f"Bearer {TOKEN}"}
            )
        ).json()
    assert (
        d["model_available"] is False
        and d["kratos_cap_usd"] is None
        and d["budget_policy"] == "metered"
    )


async def test_gateway_errors_keep_their_status_and_never_leak_content(settings: Settings) -> None:
    import anthropic
    import httpx2

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    rate = anthropic.RateLimitError(
        "slow down", response=httpx2.Response(429, request=req), body=None
    )
    app, _ = _app(settings, FakeClaude([rate]))
    r = await _post(app, {**BODY, "user": "a private research note"})
    assert (
        r.status_code == 429
        and r.json()["detail"]["code"] == "rate_limited"
        and "private research note" not in r.text
    )
    app, _ = _app(settings, FakeClaude([text_reply("", stop_reason="refusal")]))
    assert (await _post(app)).status_code == 422


@pytest.mark.parametrize("name", kratos_ai.DISTINCT_FROM)
def test_token_must_differ_from_every_other_credential(settings: Settings, name: str) -> None:
    s = settings.model_copy(update={"KRATOS_AI_TOKEN": SecretStr(TOKEN), name: SecretStr(TOKEN)})
    problem = kratos_ai.token_problem(s)
    assert problem == f"KRATOS_AI_TOKEN must not reuse the value of {name}" and TOKEN not in problem
