"""External-first routing (docs/30-external-first-ai.md), without a database.

The provider chain: AI_DEFAULT_EXTERNAL_PROVIDER, then AI_EXTERNAL_FALLBACK_PROVIDERS, then
local Ollama. A refusal or a failure moves to the next provider; a local-only verdict (the
classification filter) ends the walk, so that data reaches no external provider at all.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agents.ollama.limiter import GenerationLimiter
from app.agents.providers.base import ProviderError
from app.agents.research import ai as research_ai
from app.api.v1.ai import _default_external, _target
from app.core.config import Settings
from app.schemas.ai import ChatRequest
from app.services.ai_usage import UsageTracker, reserve_key, utc_today
from app.services.chat import ChatJob, ChatStream
from app.services.inference import ExternalGate, choose
from tests.test_ai_providers_unit import (
    GEM_MODEL,
    PUBLIC,
    FakeExternal,
    FakeOllama,
    FakeRedis,
    prov_settings,
)

OR_MODEL = "openai/test-model"
PERSONAL = [{"role": "user", "content": "my Dhan positions and P&L"}]


def chain_settings(settings: Settings, **over: Any) -> Settings:
    base: dict[str, Any] = {
        "OPENROUTER_ENABLED": True,
        "OPENROUTER_API_KEY": "sk-or-v1-" + "a" * 40,
        "OPENROUTER_DEFAULT_MODEL": OR_MODEL,
        "OPENROUTER_DAILY_LIMIT_USD": "1.00",
        "ANTHROPIC_ENABLED": False,
        "AI_DEFAULT_EXTERNAL_PROVIDER": "gemini",
        "AI_EXTERNAL_FALLBACK_PROVIDERS": "openrouter",
    }
    base.update(over)
    return prov_settings(settings, **base)


def gates_for(s: Settings, clients: dict[str, Any], redis: FakeRedis) -> dict[str, ExternalGate]:
    usage = UsageTracker(redis)
    return {
        p: ExternalGate(s, clients.get(p), usage, provider=p)
        for p in ("openrouter", "anthropic", "gemini")
    }


# --- settings -------------------------------------------------------------------------------


def test_chain_order_and_validation(settings: Settings) -> None:
    s = chain_settings(settings, AI_EXTERNAL_FALLBACK_PROVIDERS="OpenRouter, gemini,openrouter")
    assert s.AI_EXTERNAL_FALLBACK_PROVIDERS == ["openrouter", "gemini"]
    assert s.external_chain("gemini") == ["gemini", "openrouter"]
    assert s.external_chain("ollama") == ["openrouter", "gemini"]
    assert prov_settings(settings).external_chain("gemini") == ["gemini"]  # default: none
    with pytest.raises(ValueError, match="unknown provider"):
        chain_settings(settings, AI_EXTERNAL_FALLBACK_PROVIDERS="ollama")


# --- choosing the first provider -------------------------------------------------------------


async def test_refused_provider_moves_to_the_next(settings: Settings) -> None:
    # Gemini's model is unpriced: OpenRouter answers, and the reason is kept.
    s = chain_settings(settings, GEMINI_MODEL_PRICES="{}")
    gates = gates_for(
        s, {"gemini": FakeExternal("gemini"), "openrouter": FakeExternal("or")}, FakeRedis()
    )
    pick = await choose(gates, s.external_chain("gemini"), GEM_MODEL, PUBLIC)
    assert pick.gate is not None and pick.gate.provider == "openrouter"
    assert pick.model == OR_MODEL and pick.skipped_reason == "no_pricing"
    assert pick.remaining == ()


async def test_usable_first_provider_wins(settings: Settings) -> None:
    s = chain_settings(settings)
    gates = gates_for(
        s, {"gemini": FakeExternal("gemini"), "openrouter": FakeExternal("or")}, FakeRedis()
    )
    pick = await choose(gates, s.external_chain("gemini"), GEM_MODEL, PUBLIC)
    assert pick.gate is not None and pick.gate.provider == "gemini"
    assert pick.skipped_reason is None and pick.remaining == ("openrouter",)


async def test_filtered_data_goes_to_no_external_provider(settings: Settings) -> None:
    s = chain_settings(settings)
    redis = FakeRedis()
    gates = gates_for(
        s, {"gemini": FakeExternal("gemini"), "openrouter": FakeExternal("or")}, redis
    )
    pick = await choose(gates, s.external_chain("gemini"), GEM_MODEL, PERSONAL)
    assert pick.gate is None and pick.decision.local_only
    # Nothing was reserved anywhere: the walk stopped at the first verdict.
    assert not any(k.startswith("ai:") and "reserve" in k for k in redis.data)


async def test_finance_context_stays_local(settings: Settings) -> None:
    s = chain_settings(settings)
    gates = gates_for(
        s, {"gemini": FakeExternal("gemini"), "openrouter": FakeExternal("or")}, FakeRedis()
    )
    pick = await choose(gates, s.external_chain("gemini"), GEM_MODEL, PUBLIC, context="finance")
    assert pick.gate is None and pick.decision.reason == "context"


async def test_nothing_usable_means_local_with_a_reason(settings: Settings) -> None:
    s = chain_settings(settings, GEMINI_DAILY_LIMIT_USD="0", OPENROUTER_ENABLED=False)
    gates = gates_for(s, {"gemini": FakeExternal("gemini")}, FakeRedis())
    pick = await choose(gates, s.external_chain("gemini"), GEM_MODEL, PUBLIC)
    assert pick.gate is None and pick.skipped_reason == "no_budget"


# --- a new conversation's target --------------------------------------------------------------


def test_new_conversation_keeps_an_enabled_but_unusable_default(settings: Settings) -> None:
    """Before docs/30 an unusable default silently made the conversation local for good."""
    s = chain_settings(settings, GEMINI_MODEL_PRICES="{}", GEMINI_DAILY_LIMIT_USD="0")
    gates = gates_for(
        s, {"gemini": FakeExternal("gemini"), "openrouter": FakeExternal("or")}, FakeRedis()
    )
    body = ChatRequest(messages=PUBLIC)
    assert _target(body, None, gates, s) == ("gemini", GEM_MODEL)
    # The catalog's default is the first *usable* one in the chain.
    default = _default_external(s, gates)
    assert default is not None and default.provider == "openrouter"


def test_disabled_providers_mean_local(settings: Settings) -> None:
    s = chain_settings(settings, GEMINI_ENABLED=False, OPENROUTER_ENABLED=False)
    gates = gates_for(s, {}, FakeRedis())
    assert _target(ChatRequest(messages=PUBLIC), None, gates, s) == ("ollama", "")


# --- the chat stream: runtime failure moves down the chain ------------------------------------


def stream_for(
    s: Settings, clients: dict[str, Any], redis: FakeRedis, ollama: FakeOllama, reserved: int
) -> ChatStream:
    gates = gates_for(s, clients, redis)
    job = ChatJob(
        conversation_id=uuid.uuid4(), user_message_ids=[1], model=GEM_MODEL, prompt=list(PUBLIC),
        options={}, provider="gemini", requested_provider="gemini", requested_model=GEM_MODEL,
        fallback_model="qwen2.5:3b", reserved_micro=reserved, alternates=("openrouter",),
    )  # fmt: skip
    return ChatStream(
        job,
        ollama=ollama,  # type: ignore[arg-type]
        maker=None,  # type: ignore[arg-type]
        ticket=None,
        timeout=5,
        heartbeat=1,
        limiter=GenerationLimiter(1, 2, 5),
        external=clients["gemini"],
        usage=UsageTracker(redis),
        gates=gates,
    )


async def drain(stream: ChatStream) -> list[tuple[str, Any]]:
    await stream._produce()
    out = []
    while not stream._queue.empty():
        out.append(stream._queue.get_nowait())
    return out


async def test_gemini_failure_moves_to_openrouter_not_local(settings: Settings) -> None:
    s = chain_settings(settings)
    redis = FakeRedis()
    gem = FakeExternal("gemini", fail=ProviderError("Gemini returned HTTP 503", 503))
    orc = FakeExternal("openrouter", cost="0.002")
    ollama = FakeOllama()
    redis.data[reserve_key(utc_today(), "gemini")] = {"micro": 4000}
    stream = stream_for(s, {"gemini": gem, "openrouter": orc}, redis, ollama, 4000)
    events = await drain(stream)
    switch = [d for k, d in events if k == "fallback"]
    assert switch == [
        {
            "provider": "openrouter",
            "model": OR_MODEL,
            "reason": "upstream_error",
            "from_provider": "gemini",
            "local_only": False,
            "reset": True,
        }
    ]
    t = stream.turn
    assert t.status == "complete" and (t.provider, t.model) == ("openrouter", OR_MODEL)
    assert "".join(t.parts) == "External answer." and t.fallback_reason == "upstream_error"
    assert gem.calls == 1 and orc.calls == 1 and ollama.calls == []
    # Each provider's own counter: Gemini's failure (reservation released), OpenRouter's cost.
    gem_usage = UsageTracker(redis, "gemini")
    assert (await gem_usage.today()).failures == 1 and await gem_usage.reserved_micro() == 0
    assert (await UsageTracker(redis, "openrouter").today()).cost_micro == 2000
    assert [a.provider for a in stream.attempts] == ["gemini", "openrouter"]


async def test_every_external_failure_ends_on_local(settings: Settings) -> None:
    s = chain_settings(settings)
    redis = FakeRedis()
    gem = FakeExternal("gemini", fail=ProviderError("down", 503))
    orc = FakeExternal("openrouter", fail=ProviderError("down", 503))
    ollama = FakeOllama()
    stream = stream_for(s, {"gemini": gem, "openrouter": orc}, redis, ollama, 0)
    events = await drain(stream)
    assert [d["provider"] for k, d in events if k == "fallback"] == ["openrouter", "ollama"]
    assert stream.turn.provider == "ollama" and ollama.calls == ["qwen2.5:3b"]


async def test_an_unusable_alternate_is_skipped(settings: Settings) -> None:
    s = chain_settings(settings, OPENROUTER_DAILY_LIMIT_USD="0")
    redis = FakeRedis()
    gem = FakeExternal("gemini", fail=ProviderError("down", 503))
    orc = FakeExternal("openrouter")
    ollama = FakeOllama()
    stream = stream_for(s, {"gemini": gem, "openrouter": orc}, redis, ollama, 0)
    await drain(stream)
    assert orc.calls == 0 and stream.turn.provider == "ollama"


# --- the research agent ------------------------------------------------------------------------


class _Db:
    def __init__(self) -> None:
        self.rows: list[Any] = []

    def add(self, row: Any) -> None:
        self.rows.append(row)

    @asynccontextmanager
    async def begin(self) -> Any:
        yield self


def _maker(db: _Db) -> Any:
    @asynccontextmanager
    async def maker() -> Any:
        yield db

    return maker


async def test_research_walks_the_chain(settings: Settings) -> None:
    s = chain_settings(settings, AI_RESEARCH_EXTERNAL_ALLOWED=True)
    redis = FakeRedis()
    gem = FakeExternal("gemini", fail=ProviderError("down", 503))
    orc = FakeExternal("openrouter")
    gates = gates_for(s, {"gemini": gem, "openrouter": orc}, redis)
    db = _Db()
    msgs = [{"role": "user", "content": '{"type": "backtest", "metrics": {"sharpe": 1.2}}'}]
    got = await research_ai._try_external(
        _maker(db), gates, s.external_chain("gemini"), msgs, "backtest", uuid.uuid4(), 5
    )
    assert got == ("External answer.", f"openrouter:{OR_MODEL}")
    assert gem.calls == 1 and orc.calls == 1
    outcomes = [(r.details["provider"], r.details["outcome"]) for r in db.rows]
    assert outcomes == [("gemini", "error:upstream_error"), ("openrouter", "ok")]


async def test_research_keeps_the_single_gate_form(settings: Settings) -> None:
    s = chain_settings(settings)
    gate = ExternalGate(s, FakeExternal("openrouter"), UsageTracker(FakeRedis()))
    assert set(research_ai._as_gates(gate)) == {"openrouter"}
    assert research_ai._as_gates(None) == {}


# --- the model router (agents) -----------------------------------------------------------------


async def test_router_moves_down_the_chain(settings: Settings) -> None:
    from app.services.model_router import ModelRouter

    s = chain_settings(settings, AGENT_NEWS_PROVIDER="auto")
    redis = FakeRedis()
    gem = FakeExternal("gemini", fail=ProviderError("down", 503))
    orc = FakeExternal("openrouter")
    ollama = FakeOllama()
    r = ModelRouter(
        s,
        ollama=ollama,  # type: ignore[arg-type]
        openrouter=orc,  # type: ignore[arg-type]
        usage=UsageTracker(redis),
        maker=None,
        limiter=GenerationLimiter(1, 2, 5),
        external={"gemini": gem, "openrouter": orc},  # type: ignore[dict-item]
    )
    a = await r.complete("news", PUBLIC, context="news")
    assert (a.provider, a.model) == ("openrouter", OR_MODEL) and ollama.calls == []
    p = await r.complete("news", PERSONAL, context="news")
    assert p.provider == "ollama" and p.local_only and orc.calls == 1
