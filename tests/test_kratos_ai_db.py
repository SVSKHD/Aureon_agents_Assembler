"""The KRATOS AI route against the real ai.usage table (docs/39-kratos-ai-gateway.md).

tests/test_kratos_ai.py uses the in-memory usage store, which has no schema. These tests write
through Postgres: the `kratos` budget and the "kratos:<role>" label must fit ai.usage
(migration 0035), and a metered KRATOS budget must stay out of everything the other agents'
caps and status views are computed from. Claude is a scripted fake: no network, no key."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.ai.budget import PgUsageStore, shared_total
from app.ai.gateway import ClaudeGateway
from app.core.config import AI_AGENT_BUDGET_DEFAULTS
from app.models.ai import Usage
from app.services.integrations import IntegrationsSummary
from tests.fake_claude import FakeClaude, text_reply
from tests.test_agents import Env, EnvFactory, make_env  # noqa: F401
from tests.test_kratos_ai import KEY, MODEL, TOKEN, URL, FakeLimiter

pytestmark = pytest.mark.db

AUTH = {"Authorization": f"Bearer {TOKEN}"}
LONGEST_ROLE = "market_structure"  # the longest KRATOS specialist label in use


def _install(env: Env, replies: list[Any], **kw: Any) -> FakeClaude:
    fake = FakeClaude(replies)
    s = env.app.state.settings.model_copy(
        update={
            "ANTHROPIC_API_KEY": SecretStr(KEY),
            "KRATOS_AI_TOKEN": SecretStr(TOKEN),
            "KRATOS_AI_MODEL": MODEL,
            **kw,
        }
    )
    env.app.state.settings = s
    env.app.state.kratos_ai_limiter = FakeLimiter()
    env.app.state.ai = ClaudeGateway(s, PgUsageStore(env.maker), client=fake)
    return fake


def _body(role: str = LONGEST_ROLE) -> dict[str, Any]:
    return {"role": role, "system": "You are a test.", "user": "hello", "max_tokens": 500}


async def _rows(env: Env) -> list[Usage]:
    async with env.maker() as db:
        return list((await db.scalars(select(Usage).order_by(Usage.id))).all())


async def test_a_metered_call_is_recorded(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env() as env:
        fake = _install(
            env,
            [text_reply('{"summary": "ok"}', input_tokens=1200, output_tokens=300)],
            KRATOS_AI_BUDGET_POLICY="metered",
        )
        r = await env.admin.post(URL, json=_body(), headers=AUTH)
        assert r.status_code == 200, r.text
        assert fake.calls[0]["model"] == MODEL
        (row,) = await _rows(env)
        assert row.budget == "kratos" and row.agent == f"kratos:{LONGEST_ROLE}"
        assert row.outcome == "ok" and row.model == MODEL
        assert row.input_tokens == 1200 and row.output_tokens == 300 and row.cost_usd > 0
        assert Decimal(r.json()["cost_usd"]) == row.cost_usd


async def test_the_longest_role_fits_the_agent_column(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env() as env:
        _install(env, [text_reply("ok")], KRATOS_AI_BUDGET_POLICY="metered")
        role = "r" * 48
        assert (await env.admin.post(URL, json=_body(role), headers=AUTH)).status_code == 200
        assert (await _rows(env))[0].agent == f"kratos:{role}"
        assert (await env.admin.post(URL, json=_body("r" * 49), headers=AUTH)).status_code == 422
        assert len(await _rows(env)) == 1


async def test_default_policy_refuses_and_writes_nothing(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env() as env:
        fake = _install(env, [text_reply("never sent")])  # capped, cap 0: the shipped default
        r = await env.admin.post(URL, json=_body(), headers=AUTH)
        assert r.status_code == 402 and r.json()["detail"]["code"] == "budget_exceeded"
        assert fake.calls == [] and await _rows(env) == []


async def test_metered_spend_never_reaches_other_agents(make_env: EnvFactory) -> None:  # noqa: F811
    """KRATOS spends more than the whole monthly cap; chat is not refused or shown as used up."""
    async with make_env() as env:
        tiny = {**AI_AGENT_BUDGET_DEFAULTS, "chat": Decimal("1.00")}
        fake = _install(
            env,
            # A scripted token count large enough to cost well above the 2 USD monthly cap below.
            [text_reply("big", input_tokens=10_000_000, output_tokens=7_000), text_reply("hello")],
            KRATOS_AI_BUDGET_POLICY="metered",
            AI_MONTHLY_BUDGET_USD=Decimal("2.00"),
            AI_AGENT_BUDGETS_USD=tiny,
        )
        s = env.app.state.settings
        assert (await env.admin.post(URL, json=_body(), headers=AUTH)).status_code == 200
        spend = await PgUsageStore(env.maker).spend()
        kratos = spend.by_budget["kratos"]
        assert kratos > s.AI_MONTHLY_BUDGET_USD  # more than every other agent may spend together
        assert shared_total(s, spend) == 0

        # Another agent still gets its call, on its own budget.
        chat = await env.post("admin", "/api/ai/chat", json={"message": "hi", "agent": "general"})
        assert chat.status_code == 200 and len(fake.calls) == 2
        spend = await PgUsageStore(env.maker).spend()
        assert spend.by_budget["chat"] > 0 and spend.by_budget["kratos"] == kratos

        # The admin usage view: the total and what remains leave KRATOS out; its line shows it.
        usage = (await env.admin.get("/api/ai/usage")).json()
        assert Decimal(usage["spent_usd"]) == spend.by_budget["chat"]
        assert Decimal(usage["remaining_usd"]) == s.AI_MONTHLY_BUDGET_USD - spend.by_budget["chat"]
        line = next(b for b in usage["budgets"] if b["budget"] == "kratos")
        assert Decimal(line["spent_usd"]) == kratos and usage["calls"] == 2

        # KRATOS's own preflight reports the same split: shared spend by the cap, its own apart.
        cfg = (await env.admin.get(URL.replace("complete", "config"), headers=AUTH)).json()
        assert Decimal(cfg["month_to_date_usd"]) == spend.by_budget["chat"]
        assert Decimal(cfg["kratos_month_to_date_usd"]) == kratos and cfg["kratos_cap_usd"] is None
        assert cfg["budget_policy"] == "metered" and cfg["model_selected"] == MODEL

        # The integrations card does not call Claude exhausted because of KRATOS.
        card = await IntegrationsSummary(s, env.maker, None).claude_api()
        assert (
            card["status"] == "ok" and Decimal(card["month_spent_usd"]) == spend.by_budget["chat"]
        )

        # The same spend under the default policy WOULD count: the exclusion is the opt-in only.
        capped = s.model_copy(update={"KRATOS_AI_BUDGET_POLICY": "capped"})
        assert shared_total(capped, spend) == spend.total
        card = await IntegrationsSummary(capped, env.maker, None).claude_api()
        assert card["status"] == "degraded"


async def test_a_runtime_policy_change_reaches_the_gateway(make_env: EnvFactory) -> None:  # noqa: F811
    """Settings saved in Spasta are applied without a restart. The gateway keeps the settings it was
    built with, so a policy change has to rebuild it: both ways, capped -> metered -> capped."""
    from app.services import app_settings_runtime as runtime

    async with make_env() as env:
        _install(env, [])  # the shipped default: capped
        first = env.app.state.ai
        assert first.settings.ai_metered_budgets == frozenset()

        metered = env.app.state.settings.model_copy(update={"KRATOS_AI_BUDGET_POLICY": "metered"})
        assert runtime.apply(env.app, metered) == ["KRATOS_AI_BUDGET_POLICY"]
        second = env.app.state.ai
        assert second is not first and second.settings.ai_metered_budgets == {"kratos"}
        cfg = (await env.admin.get(URL.replace("complete", "config"), headers=AUTH)).json()
        assert cfg["budget_policy"] == "metered" and cfg["kratos_cap_usd"] is None

        capped = env.app.state.settings.model_copy(update={"KRATOS_AI_BUDGET_POLICY": "capped"})
        assert runtime.apply(env.app, capped) == ["KRATOS_AI_BUDGET_POLICY"]
        third = env.app.state.ai
        assert third is not second and third.settings.ai_metered_budgets == frozenset()
        fake = FakeClaude([text_reply("never sent")])
        env.app.state.ai = ClaudeGateway(third.settings, PgUsageStore(env.maker), client=fake)
        r = await env.admin.post(URL, json=_body(), headers=AUTH)  # the stop takes effect at once
        assert r.status_code == 402 and fake.calls == [] and await _rows(env) == []


async def test_the_real_rate_limiter_counts_calls_in_redis(make_env: EnvFactory) -> None:  # noqa: F811
    """Every other test injects a stand-in limiter. This one uses the route's own, on Redis."""
    async with make_env() as env:
        fake = _install(
            env,
            [text_reply("one"), text_reply("two"), text_reply("never sent")],
            KRATOS_AI_BUDGET_POLICY="metered",
            KRATOS_AI_RATE_PER_MINUTE=2,
        )
        del env.app.state.kratos_ai_limiter  # the route builds the real one on first use
        for _ in range(2):
            r = await env.admin.post(URL, json=_body(), headers=AUTH)
            assert r.status_code == 200, r.text
        r = await env.admin.post(URL, json=_body(), headers=AUTH)
        assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"
        assert int(r.headers["Retry-After"]) >= 1
        assert len(fake.calls) == 2 and len(await _rows(env)) == 2  # the third was never reserved
