"""AI_LOCAL_ENABLED=false: external providers only. Ollama is never called, readiness skips
it, and a request that must stay local is refused, never sent out instead. No network."""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.ollama.limiter import GenerationLimiter
from app.agents.openrouter.client import OpenRouterError
from app.agents.research import ai as research_ai
from app.core.config import Settings, set_effective_settings
from app.services import coding, repairs
from app.services.health import HealthService
from app.services.model_router import ModelRouter, ModelUnavailable
from tests.test_model_router import (
    PERSONAL,
    PUBLIC_NEWS,
    FakeOllama,
    FakeOpenRouter,
    MemRedis,
)
from tests.test_model_router import _settings as _router_settings


def _router(s: Settings, openrouter: FakeOpenRouter) -> tuple[ModelRouter, FakeOllama]:
    from app.services.ai_usage import UsageTracker

    ollama = FakeOllama()
    r = ModelRouter(
        s,
        ollama=ollama,  # type: ignore[arg-type]
        openrouter=openrouter,  # type: ignore[arg-type]
        usage=UsageTracker(MemRedis()),  # type: ignore[arg-type]
        maker=None,
        limiter=GenerationLimiter(1, 2, 5),
    )
    return r, ollama


def _off(settings: Settings, **kw: Any) -> Settings:
    return _router_settings(settings, AI_LOCAL_ENABLED=False, **kw)


async def test_public_prompt_still_goes_to_openrouter(settings: Settings) -> None:
    r, ollama = _router(_off(settings), FakeOpenRouter())
    a = await r.complete("news", PUBLIC_NEWS, context="news")
    assert (a.provider, a.text) == ("openrouter", "External answer.")
    assert ollama.calls == []


async def test_local_only_prompt_is_refused_not_sent_out(settings: Settings) -> None:
    openrouter = FakeOpenRouter()
    r, ollama = _router(_off(settings), openrouter)
    with pytest.raises(ModelUnavailable, match="local models are disabled"):
        await r.complete("voice", PERSONAL)
    assert openrouter.calls == 0  # the filter still keeps it in
    assert ollama.calls == []


async def test_external_failure_does_not_fall_back_to_local(settings: Settings) -> None:
    r, ollama = _router(_off(settings), FakeOpenRouter(fail=OpenRouterError("down")))
    with pytest.raises(ModelUnavailable):
        await r.complete("news", PUBLIC_NEWS, context="news")
    assert ollama.calls == []


async def test_agent_pinned_to_ollama_is_refused(settings: Settings) -> None:
    openrouter = FakeOpenRouter()
    r, ollama = _router(_off(settings, AGENT_NEWS_PROVIDER="ollama"), openrouter)
    with pytest.raises(ModelUnavailable, match="forced_local"):
        await r.complete("news", PUBLIC_NEWS, context="news")
    assert (openrouter.calls, ollama.calls) == (0, [])


async def test_local_on_keeps_the_fallback(settings: Settings) -> None:
    r, ollama = _router(_router_settings(settings), FakeOpenRouter())
    a = await r.complete("voice", PERSONAL)
    assert a.provider == "ollama" and ollama.calls


async def test_readiness_skips_ollama_only_when_local_is_off(settings: Settings) -> None:
    svc = HealthService(settings, None, None, None)  # type: ignore[arg-type]

    async def ok() -> str | None:
        return None

    svc._postgres = svc._redis = svc._ollama = svc._data_root = ok  # type: ignore[method-assign]
    try:
        set_effective_settings(settings.model_copy(update={"AI_LOCAL_ENABLED": False}))
        assert [c.name for c in await svc.readiness()] == ["postgres", "redis", "data_root"]
        set_effective_settings(settings)
        assert "ollama" in [c.name for c in await svc.readiness()]
    finally:
        set_effective_settings(None)


def test_research_coding_and_repairs_see_no_local_model(settings: Settings) -> None:
    s = settings.model_copy(
        update={
            "AI_LOCAL_ENABLED": False,
            "RESEARCH_AI_MODEL": "qwen2.5:3b",
            "CODE_MODEL": "qwen2.5-coder:7b",
            "REPAIR_MODEL": "qwen2.5-coder:7b",
        }
    )
    assert "local models are disabled" in (research_ai.unavailable_reason(s) or "")
    assert coding.CodingConfig.from_settings(s).model is None
    assert repairs.RepairConfig.from_settings(s).model is None
    assert "AI_LOCAL_ENABLED" in (repairs.disabled_reason(s) or "")
    on = s.model_copy(update={"AI_LOCAL_ENABLED": True})
    assert research_ai.unavailable_reason(on) is None
    assert coding.CodingConfig.from_settings(on).model == "qwen2.5-coder:7b"
