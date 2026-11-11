"""Gemini 3.5 Flash-Lite as Aureon's primary external model (docs/26-ai-providers.md).

Router-level, no network: fake Gemini / OpenRouter / Ollama clients. Covers the defaults, a
Gemini success that never starts Qwen, an unavailable (unlisted / unpriced) model, budget
exhaustion, privacy restrictions, fallback order, `allow_local=False`, the trading research
path, and that market data / screener code imports no model client."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.agents.ollama.limiter import GenerationLimiter
from app.agents.providers.base import ProviderError
from app.agents.research import ai as research_ai
from app.core.config import BUILTIN_MODEL_PRICES, GEMINI_PRIMARY_MODEL, Settings
from app.models.model_calls import ModelCall
from app.services.ai_usage import UsageTracker
from app.services.inference import ExternalGate
from app.services.model_router import ModelRouter, ModelUnavailable
from tests.test_ai_providers_unit import FakeExternal, FakeOllama, FakeRedis

GEM = GEMINI_PRIMARY_MODEL
PUBLIC_NEWS = [
    {"role": "system", "content": "Summarise the headline in one sentence."},
    {"role": "user", "content": "RBI keeps repo rate unchanged; Nifty steady."},
]
PERSONAL = [{"role": "user", "content": "What is my P&L today on my Dhan positions?"}]


class RecDb:
    """Collects ai.model_calls rows (and anything else added) without a database."""

    def __init__(self) -> None:
        self.rows: list[Any] = []

    def add(self, row: Any) -> None:
        self.rows.append(row)

    @asynccontextmanager
    async def begin(self) -> Any:
        yield self

    @property
    def calls(self) -> list[ModelCall]:
        return [r for r in self.rows if isinstance(r, ModelCall)]


def maker_for(db: RecDb) -> Any:
    @asynccontextmanager
    async def maker() -> Any:
        yield db

    return maker


def gem_settings(settings: Settings, **over: Any) -> Settings:
    """Production-like: Gemini enabled with a key and a budget, the model left to defaults."""
    base: dict[str, Any] = {
        **settings.model_dump(),
        "GEMINI_ENABLED": True,
        "GEMINI_API_KEY": "AIza" + "x" * 35,
        "GEMINI_DAILY_LIMIT_USD": "1.00",
        "OPENROUTER_ENABLED": False,
        "ANTHROPIC_ENABLED": False,
        "MODEL_ROUTER_EXTERNAL_TIMEOUT_SECONDS": 2,
    }
    for k in (
        "GEMINI_DEFAULT_MODEL",
        "GEMINI_ALLOWED_MODELS",
        "GEMINI_MODEL_PRICES",
        "AI_DEFAULT_EXTERNAL_PROVIDER",
        "AGENT_NEWS_PROVIDER",
        "AGENT_VOICE_PROVIDER",
        "AGENT_RESEARCH_PROVIDER",
    ):
        base.pop(k, None)  # the shipped defaults
    base.update(over)
    return Settings.model_validate(base)


def make_router(
    s: Settings, clients: dict[str, Any], *, redis: FakeRedis | None = None
) -> tuple[ModelRouter, FakeOllama, RecDb]:
    ollama, db = FakeOllama(), RecDb()
    r = ModelRouter(
        s,
        ollama=ollama,  # type: ignore[arg-type]
        openrouter=None,
        usage=UsageTracker(redis or FakeRedis()),  # type: ignore[arg-type]
        maker=maker_for(db),
        limiter=GenerationLimiter(1, 4, 5),
        external=clients,
    )
    return r, ollama, db


# --- defaults -----------------------------------------------------------------------------


def test_defaults_select_gemini_flash_lite(settings: Settings) -> None:
    s = gem_settings(settings)
    assert s.AI_DEFAULT_EXTERNAL_PROVIDER == "gemini"
    assert (s.AGENT_NEWS_PROVIDER, s.AGENT_VOICE_PROVIDER, s.AGENT_RESEARCH_PROVIDER) == (
        "gemini",
        "gemini",
        "gemini",
    )
    cfg = s.external("gemini")
    assert cfg.default_model == GEM == "gemini-3.5-flash-lite"
    assert GEM in cfg.models and cfg.priced(GEM)
    assert cfg.prices[GEM] == (Decimal("0.30"), Decimal("2.50"))
    assert s.external_unusable_reason("gemini") is None
    # Local models are unchanged (the fallback).
    assert s.AGENT_NEWS_LOCAL_MODEL == "qwen2.5:3b"


def test_blank_default_model_means_flash_lite(settings: Settings) -> None:
    assert gem_settings(settings, GEMINI_DEFAULT_MODEL="").GEMINI_DEFAULT_MODEL == GEM
    assert gem_settings(settings, GEMINI_DEFAULT_MODEL="  ").external("gemini").default_model == GEM


def test_operator_price_wins_over_builtin_and_caps_are_untouched(settings: Settings) -> None:
    s = gem_settings(settings, GEMINI_MODEL_PRICES=json.dumps({GEM: {"input": "9", "output": "9"}}))
    assert s.external("gemini").prices[GEM] == (Decimal("9"), Decimal("9"))
    assert BUILTIN_MODEL_PRICES["gemini"][GEM] == (Decimal("0.30"), Decimal("2.50"))
    # Shipped defaults keep external AI off: no key, no budget, not enabled.
    plain = Settings.model_validate(
        {
            k: v
            for k, v in settings.model_dump().items()
            if not k.startswith(("GEMINI_", "AI_DEFAULT"))
        }
    )
    assert plain.GEMINI_ENABLED is False and plain.GEMINI_DAILY_LIMIT_USD == 0
    assert plain.external_unusable_reason("gemini") == "disabled"


# --- routing ------------------------------------------------------------------------------


@pytest.mark.parametrize("agent", ["news", "voice", "research"])
async def test_gemini_success_never_starts_qwen(settings: Settings, agent: str) -> None:
    s = gem_settings(settings, AI_RESEARCH_EXTERNAL_ALLOWED=True)
    gem = FakeExternal("gemini")
    r, ollama, db = make_router(s, {"gemini": gem})
    ctx = {"news": "news", "voice": "general", "research": "research"}[agent]
    msgs = [{"role": "user", "content": "Write a haiku about the monsoon."}]
    ans = await r.complete(agent, msgs, context=ctx)  # type: ignore[arg-type]
    assert (ans.provider, ans.model, ans.fallback, ans.route_reason) == ("gemini", GEM, False, None)
    assert gem.calls == 1 and ollama.calls == []
    assert [(c.provider, c.model, c.outcome) for c in db.calls] == [("gemini", GEM, "ok")]
    assert db.calls[0].cost_usd is not None


async def test_unavailable_model_is_refused_and_falls_back(settings: Settings) -> None:
    # The agent asks for a Gemini model that is neither allowed nor priced.
    s = gem_settings(settings, AGENT_NEWS_MODEL="gemini-unlisted-model")
    gem = FakeExternal("gemini")
    r, ollama, _ = make_router(s, {"gemini": gem})
    ans = await r.complete("news", PUBLIC_NEWS, context="news")
    assert gem.calls == 0 and ans.provider == "ollama"
    assert ans.route_reason in ("model_not_allowed", "no_pricing")
    assert ollama.calls == ["qwen2.5:3b"]


async def test_provider_error_falls_back_once(settings: Settings) -> None:
    s = gem_settings(settings)
    gem = FakeExternal("gemini", fail=ProviderError("model not found", 404))
    r, ollama, db = make_router(s, {"gemini": gem})
    ans = await r.complete("news", PUBLIC_NEWS, context="news")
    assert ans.provider == "ollama" and ans.fallback is True
    assert [(c.provider, c.outcome, c.fallback) for c in db.calls] == [
        ("gemini", "error", False),
        ("ollama", "ok", True),
    ]
    assert len(ollama.calls) == 1


async def test_fallback_chain_prefers_another_external_before_qwen(settings: Settings) -> None:
    s = gem_settings(
        settings,
        OPENROUTER_ENABLED=True,
        OPENROUTER_API_KEY="sk-or-v1-" + "a" * 40,
        OPENROUTER_DEFAULT_MODEL="openai/test-model",
        OPENROUTER_DAILY_LIMIT_USD="1.00",
        AI_EXTERNAL_FALLBACK_PROVIDERS="openrouter",
    )
    gem = FakeExternal("gemini", fail=ProviderError("overloaded", 503))
    orc = FakeExternal("openrouter")
    r, ollama, _ = make_router(s, {"gemini": gem, "openrouter": orc})
    ans = await r.complete("news", PUBLIC_NEWS, context="news")
    assert ans.provider == "openrouter" and ollama.calls == []


async def test_budget_exhausted_stays_local_without_calling_gemini(settings: Settings) -> None:
    s = gem_settings(settings, GEMINI_DAILY_LIMIT_USD="0.000001")
    gem = FakeExternal("gemini")
    r, _, _ = make_router(s, {"gemini": gem})
    ans = await r.complete("news", PUBLIC_NEWS, context="news", max_tokens=400)
    assert gem.calls == 0 and ans.provider == "ollama"
    assert ans.route_reason and "cap" in ans.route_reason
    no_budget = gem_settings(settings, GEMINI_DAILY_LIMIT_USD="0")
    r2, _, _ = make_router(no_budget, {"gemini": FakeExternal("gemini")})
    ans2 = await r2.complete("news", PUBLIC_NEWS, context="news")
    assert ans2.provider == "ollama" and ans2.route_reason == "no_budget"


async def test_global_cap_also_applies(settings: Settings) -> None:
    s = gem_settings(settings, AI_EXTERNAL_DAILY_LIMIT_USD="0")
    gem = FakeExternal("gemini")
    r, _, _ = make_router(s, {"gemini": gem})
    ans = await r.complete("news", PUBLIC_NEWS, context="news")
    assert gem.calls == 0 and ans.provider == "ollama"


async def test_privacy_restrictions_keep_data_local(settings: Settings) -> None:
    s = gem_settings(settings, AI_RESEARCH_EXTERNAL_ALLOWED=True)
    gem = FakeExternal("gemini")
    r, _, _ = make_router(s, {"gemini": gem})
    ans = await r.complete("voice", PERSONAL, context="general")
    assert gem.calls == 0 and ans.local_only is True and ans.provider == "ollama"
    # A finance context never leaves, whatever the text.
    ans = await r.complete("research", [{"role": "user", "content": "haiku"}], context="finance")
    assert gem.calls == 0 and ans.provider == "ollama"
    # Research without AI_RESEARCH_EXTERNAL_ALLOWED stays local.
    r2, _, _ = make_router(gem_settings(settings), {"gemini": gem})
    ans = await r2.complete("research", [{"role": "user", "content": "haiku"}], context="research")
    assert gem.calls == 0 and ans.provider == "ollama"


async def test_explicitly_local_agent(settings: Settings) -> None:
    s = gem_settings(settings, AGENT_NEWS_PROVIDER="ollama")
    gem = FakeExternal("gemini")
    r, ollama, _ = make_router(s, {"gemini": gem})
    ans = await r.complete("news", PUBLIC_NEWS, context="news")
    assert gem.calls == 0 and ans.route_reason == "forced_local" and ollama.calls


async def test_no_local_fallback_means_no_qwen(settings: Settings) -> None:
    s = gem_settings(settings)
    gem = FakeExternal("gemini", fail=ProviderError("down", 503))
    r, ollama, db = make_router(s, {"gemini": gem})
    with pytest.raises(ModelUnavailable):
        await r.complete(
            "voice",
            [{"role": "user", "content": "Write a haiku about the monsoon."}],
            context="general",
            allow_local=False,
        )
    assert ollama.calls == []
    assert [(c.provider, c.outcome) for c in db.calls] == [
        ("gemini", "error"),
        ("ollama", "skipped"),
    ]


# --- trading research explanations ----------------------------------------------------------


def research_gates(s: Settings, clients: dict[str, Any]) -> dict[str, ExternalGate]:
    usage = UsageTracker(FakeRedis())  # type: ignore[arg-type]
    return {
        p: ExternalGate(s, clients.get(p), usage, provider=p)
        for p in ("openrouter", "anthropic", "gemini")
    }


async def test_research_explanation_uses_gemini_and_skips_ollama(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = gem_settings(settings, AI_RESEARCH_EXTERNAL_ALLOWED=True, RESEARCH_AI_MODEL=None)
    assert research_ai.unavailable_reason(s) is None  # no local model needed now
    gem = FakeExternal("gemini")
    db = RecDb()
    ctx = {"type": "backtest", "metrics": {"sharpe": 1.2}}

    async def fake_context(*a: Any) -> dict[str, Any]:
        return ctx

    monkeypatch.setattr(research_ai, "_context", fake_context)
    ollama = FakeOllama()
    await research_ai.explain(
        maker_for(db),
        ollama,  # type: ignore[arg-type]
        None,
        "backtest",
        uuid.uuid4(),
        limit_s=5,
        external=research_gates(s, {"gemini": gem}),
    )
    assert gem.calls == 1 and ollama.calls == []
    calls = db.calls
    assert [(c.agent, c.provider, c.model, c.outcome) for c in calls] == [
        ("trading-research", "gemini", GEM, "ok")
    ]
    analysis = next(r for r in db.rows if type(r).__name__ == "AiAnalysis")
    assert analysis.model == f"gemini:{GEM}"


async def test_research_explanation_falls_back_or_refuses_honestly(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_context(*a: Any) -> dict[str, Any]:
        return {"type": "backtest", "metrics": {"sharpe": 1.2}}

    monkeypatch.setattr(research_ai, "_context", fake_context)
    s = gem_settings(settings, AI_RESEARCH_EXTERNAL_ALLOWED=True)
    gem = FakeExternal("gemini", fail=ProviderError("down", 503))
    # With a local model: one Gemini failure, then exactly one local call, both recorded.
    db, ollama = RecDb(), FakeOllama()
    await research_ai.explain(
        maker_for(db), ollama, "qwen2.5:3b", "backtest", uuid.uuid4(),  # type: ignore[arg-type]
        limit_s=5, external=research_gates(s, {"gemini": gem}),
    )  # fmt: skip
    assert ollama.calls == ["qwen2.5:3b"]
    assert [(c.provider, c.outcome, c.fallback) for c in db.calls] == [
        ("gemini", "error", False),
        ("ollama", "ok", True),
    ]
    # Without one: an explicit error, no invented text, no local call.
    db2, ollama2 = RecDb(), FakeOllama()
    with pytest.raises(research_ai.NoModel):
        await research_ai.explain(
            maker_for(db2), ollama2, None, "backtest", uuid.uuid4(),  # type: ignore[arg-type]
            limit_s=5, external=research_gates(s, {"gemini": gem}),
        )  # fmt: skip
    assert ollama2.calls == [] and not [r for r in db2.rows if type(r).__name__ == "AiAnalysis"]
    # Research external not permitted and no local model: the API refuses up front.
    assert "AI_RESEARCH_EXTERNAL_ALLOWED" in (
        research_ai.unavailable_reason(gem_settings(settings, RESEARCH_AI_MODEL=None)) or ""
    )


async def test_research_provider_ollama_keeps_explanations_local(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_context(*a: Any) -> dict[str, Any]:
        return {"type": "backtest", "metrics": {}}

    monkeypatch.setattr(research_ai, "_context", fake_context)
    s = gem_settings(settings, AI_RESEARCH_EXTERNAL_ALLOWED=True, AGENT_RESEARCH_PROVIDER="ollama")
    gem, db, ollama = FakeExternal("gemini"), RecDb(), FakeOllama()
    await research_ai.explain(
        maker_for(db), ollama, "qwen2.5:3b", "backtest", uuid.uuid4(),  # type: ignore[arg-type]
        limit_s=5, external=research_gates(s, {"gemini": gem}),
    )  # fmt: skip
    assert gem.calls == 0 and ollama.calls == ["qwen2.5:3b"]
    assert db.calls[0].route_reason == "forced_local"


# --- deterministic market code --------------------------------------------------------------


def test_market_data_and_screener_never_import_a_model_client() -> None:
    root = Path(__file__).resolve().parents[1] / "app"
    files = [
        root / "agents/research/indicators.py",
        root / "agents/research/screener.py",
        root / "services/screener.py",
        root / "services/market_data.py",
        root / "services/dhan_sync.py",
        root / "agents/trading/dhan/adapter.py",
    ]
    banned = ("ollama", "gemini", "openrouter", "anthropic", "model_router", "inference")
    for f in files:
        imports = [ln for ln in f.read_text().splitlines() if ln.startswith(("import", "from"))]
        assert not [ln for ln in imports if any(b in ln for b in banned)], f
