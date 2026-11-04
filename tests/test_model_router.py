"""Agent model routing (app/services/model_router.py, docs/model-routing.md). No network:
fake OpenRouter / Ollama clients and an in-memory usage counter."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from app.agents.ollama.limiter import GenerationLimiter
from app.agents.openrouter.client import OpenRouterError
from app.core.config import Settings
from app.services.ai_usage import UsageTracker
from app.services.model_router import AgentDisabled, LocalBusy, ModelRouter

PUBLIC_NEWS = [
    {"role": "system", "content": "Summarise the headline in one sentence."},
    {"role": "user", "content": "RBI keeps repo rate unchanged at 6.5%; Nifty and gold steady."},
]
PERSONAL = [{"role": "user", "content": "What is my P&L today on my Dhan positions?"}]


class FakeOpenRouter:
    def __init__(self, *, delay: float = 0, fail: OpenRouterError | None = None) -> None:
        self.delay, self.fail, self.calls = delay, fail, 0

    async def chat_stream(
        self, model: str, messages: Any, **kw: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        yield {"content": "External "}
        yield {"content": "answer."}
        yield {"done": True, "usage": {"prompt_tokens": 30, "completion_tokens": 4, "cost": 0.0002}}


class FakeOllama:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat_stream(
        self, model: str, messages: Any, options: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(model)
        yield {"message": {"content": "Local answer."}, "done": False}
        yield {"done": True, "prompt_eval_count": 25, "eval_count": 3}


class MemRedis:
    def __init__(self) -> None:
        self.h: dict[str, dict[str, int]] = {}

    async def hgetall(self, name: str) -> dict[str, int]:
        return dict(self.h.get(name, {}))

    async def hincrby(self, name: str, key: str, amount: int = 1) -> int:
        d = self.h.setdefault(name, {})
        d[key] = d.get(key, 0) + amount
        return d[key]

    async def expire(self, name: str, time: int) -> bool:
        return True


def _settings(settings: Settings, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "OPENROUTER_ENABLED": True,
        "OPENROUTER_API_KEY": SecretStr("sk-or-" + "x" * 40),
        "OPENROUTER_DEFAULT_MODEL": "openai/gpt-4o-mini",
        "OPENROUTER_DAILY_LIMIT_USD": Decimal("1"),
        "MODEL_ROUTER_EXTERNAL_TIMEOUT_SECONDS": 0.2,
    }
    return settings.model_copy(update={**base, **kw})


def _router(
    s: Settings,
    openrouter: FakeOpenRouter | None = None,
    *,
    redis: MemRedis | None = None,
    limiter: GenerationLimiter | None = None,
) -> tuple[ModelRouter, FakeOllama]:
    ollama = FakeOllama()
    r = ModelRouter(
        s,
        ollama=ollama,  # type: ignore[arg-type]
        openrouter=openrouter or FakeOpenRouter(),  # type: ignore[arg-type]
        usage=UsageTracker(redis or MemRedis()),
        maker=None,
        limiter=limiter or GenerationLimiter(1, 2, 5),
    )
    return r, ollama


async def test_public_news_goes_to_openrouter_and_counts_against_the_cap(
    settings: Settings,
) -> None:
    redis = MemRedis()
    r, ollama = _router(_settings(settings), redis=redis)
    a = await r.complete("news", PUBLIC_NEWS, context="news")
    assert (a.provider, a.text, a.fallback, a.local_only) == (
        "openrouter", "External answer.", False, False,
    )  # fmt: skip
    assert ollama.calls == []
    assert (await UsageTracker(redis).today()).spent_usd == Decimal("0.0002")


async def test_personal_finance_is_forced_local_and_flagged(settings: Settings) -> None:
    ext = FakeOpenRouter()
    r, ollama = _router(_settings(settings), ext)
    a = await r.complete("voice", PERSONAL, context="general")
    assert a.provider == "ollama" and a.local_only and a.route_reason == "filter"
    assert ext.calls == 0 and ollama.calls == ["qwen2.5:3b"]


@pytest.mark.parametrize(
    ("ext", "reason"),
    [
        (FakeOpenRouter(delay=1.0), "timeout"),
        (FakeOpenRouter(fail=OpenRouterError("boom", 502)), "upstream_error"),
    ],
)
async def test_external_error_or_timeout_falls_back_to_local(
    settings: Settings, ext: FakeOpenRouter, reason: str
) -> None:
    r, ollama = _router(_settings(settings), ext)
    a = await r.complete("news", PUBLIC_NEWS, context="news")
    assert a.provider == "ollama" and a.fallback and a.route_reason == reason
    assert a.text == "Local answer." and ollama.calls == ["qwen2.5:3b"]


async def test_cap_reached_stays_local(settings: Settings) -> None:
    redis = MemRedis()
    await UsageTracker(redis).record(
        cost_usd=Decimal("1.5"), prompt_tokens=1, completion_tokens=1, failed=False
    )
    ext = FakeOpenRouter()
    r, _ = _router(_settings(settings), ext, redis=redis)
    a = await r.complete("news", PUBLIC_NEWS, context="news")
    assert a.provider == "ollama" and a.route_reason == "daily_cap_reached" and ext.calls == 0


async def test_research_stays_local_unless_allowed(settings: Settings) -> None:
    ext = FakeOpenRouter()
    r, _ = _router(_settings(settings), ext)
    a = await r.complete("research", PUBLIC_NEWS, context="research")
    assert a.provider == "ollama" and a.route_reason == "context" and ext.calls == 0
    r2, _ = _router(_settings(settings, AI_RESEARCH_EXTERNAL_ALLOWED=True), ext)
    assert (await r2.complete("research", PUBLIC_NEWS, context="research")).provider == "openrouter"


async def test_per_agent_provider_model_and_enabled(settings: Settings) -> None:
    ext = FakeOpenRouter()
    r, ollama = _router(
        _settings(settings, AGENT_NEWS_PROVIDER="ollama", AGENT_NEWS_LOCAL_MODEL="qwen2.5:1.5b"),
        ext,
    )
    a = await r.complete("news", PUBLIC_NEWS, context="news")
    assert a.route_reason == "forced_local" and ollama.calls == ["qwen2.5:1.5b"] and ext.calls == 0
    off, _ = _router(_settings(settings, AGENT_VOICE_ENABLED=False))
    with pytest.raises(AgentDisabled):
        await off.complete("voice", PUBLIC_NEWS)


async def test_openrouter_off_means_local_without_a_call(settings: Settings) -> None:
    ext = FakeOpenRouter()
    r, _ = _router(settings.model_copy(update={"OPENROUTER_ENABLED": False}), ext)
    a = await r.complete("news", PUBLIC_NEWS, context="news")
    assert a.provider == "ollama" and a.route_reason == "disabled" and ext.calls == 0


async def test_local_queue_is_bounded(settings: Settings) -> None:
    limiter = GenerationLimiter(1, 0, 5)  # one running, nobody may wait
    hold = limiter.admit()
    await hold.acquire_slot()
    r, _ = _router(settings.model_copy(update={"OPENROUTER_ENABLED": False}), limiter=limiter)
    with pytest.raises(LocalBusy):
        await r.complete("news", PUBLIC_NEWS, context="news")
    hold.release()


def test_news_context_lets_public_market_words_out_but_not_personal_data() -> None:
    from app.services.chat_filter import classify_chat

    assert classify_chat(PUBLIC_NEWS, context="news").external_ok
    assert not classify_chat(PUBLIC_NEWS, context="general").external_ok  # market words
    for text in ("my Dhan client id 1100003626", "password: hunter2", "my P&L is +12,000 INR"):
        assert not classify_chat([{"role": "user", "content": text}], context="news").external_ok
