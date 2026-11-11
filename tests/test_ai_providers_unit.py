"""Direct Anthropic and Gemini providers (docs/26-ai-providers.md), without a database.

Covers the adapters against mocked HTTP (streaming, usage and cost, error mapping, refusals,
timeouts), the provider-neutral gate (order of reasons, pricing, per-provider and global
caps, the local rate limit, atomic reservations under concurrency, fail-closed Redis), the
chat stream's local-only fallback, the model router, settings/secrets, and the key never
appearing in a repr, an error, a log line or an outbound payload.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import redirect_stdout
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import httpx2
import pytest
from pydantic import SecretStr

from app.agents.anthropic.client import AnthropicClient
from app.agents.gemini.client import GeminiClient, normalize_model
from app.agents.ollama.limiter import GenerationLimiter
from app.agents.providers.base import ModelPrice, ProviderError, ProviderRejected
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger, scrub
from app.integrations.github.redact import redact
from app.services import app_settings as svc
from app.services.ai_usage import UsageTracker, day_key, reserve_key, utc_today
from app.services.chat import ChatJob, ChatStream
from app.services.chat_filter import classify_chat
from app.services.inference import ExternalGate, build_external, estimate_cost
from app.services.model_router import ModelRouter

ANT_KEY = "sk-ant-api03-" + "Zq9Xw8Vu7Ts6Rq5Po4Nm3Lk2" * 2
GEM_KEY = "AIza" + "SyD3x4mpl3K3yV4lu3F0rT3st1ngOnly"
ANT_MODEL = "claude-test-model"
GEM_MODEL = "gemini-test-model"
PRICE = ModelPrice(Decimal("3"), Decimal("15"))  # USD per million tokens (test values)
ECHO = "ECHOED-USER-SECRET-123"
PUBLIC = [{"role": "user", "content": "Write a haiku about the monsoon."}]


# --- shared fakes ---------------------------------------------------------------------------


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.data: dict[str, dict[str, int]] = {}
        self.fail = fail

    async def hgetall(self, name: str) -> dict[str, int]:
        if self.fail:
            raise ConnectionError("redis down")
        return dict(self.data.get(name, {}))

    async def hincrby(self, name: str, key: str, amount: int = 1) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        await asyncio.sleep(0)  # let concurrent callers interleave
        h = self.data.setdefault(name, {})
        h[key] = h.get(key, 0) + amount
        return h[key]

    async def expire(self, name: str, time: int) -> bool:
        return True


class FakeExternal:
    """Any provider's client: yields text, then parsed usage (`cost_usd`)."""

    def __init__(
        self, provider: str, fail: ProviderError | None = None, cost: str = "0.001"
    ) -> None:
        self.provider, self.fail, self.cost, self.calls = provider, fail, Decimal(cost), 0

    async def chat_stream(
        self, model: str, messages: Any, **kw: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls += 1
        if self.fail:
            raise self.fail
        yield {"content": "External "}
        yield {"content": "answer."}
        yield {
            "done": True,
            "usage": {"prompt_tokens": 20, "completion_tokens": 4, "cost_usd": self.cost},
            "model": model,
        }

    async def list_models(self) -> list[str]:
        return [ANT_MODEL, GEM_MODEL]

    async def aclose(self) -> None:
        return None


class FakeOllama:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat_stream(
        self, model: str, messages: Any, options: Any
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append(model)
        yield {"message": {"content": "Local answer."}, "done": False}
        yield {"done": True, "prompt_eval_count": 25, "eval_count": 3}


def prov_settings(settings: Settings, **over: Any) -> Settings:
    base: dict[str, Any] = {
        **settings.model_dump(),
        "ANTHROPIC_ENABLED": True,
        "ANTHROPIC_API_KEY": ANT_KEY,
        "ANTHROPIC_DEFAULT_MODEL": ANT_MODEL,
        "ANTHROPIC_ALLOWED_MODELS": "claude-unpriced-model",
        "ANTHROPIC_DAILY_LIMIT_USD": "1.00",
        "ANTHROPIC_MODEL_PRICES": json.dumps({ANT_MODEL: {"input": "3", "output": "15"}}),
        "GEMINI_ENABLED": True,
        "GEMINI_API_KEY": GEM_KEY,
        "GEMINI_DEFAULT_MODEL": GEM_MODEL,
        "GEMINI_DAILY_LIMIT_USD": "1.00",
        "GEMINI_MODEL_PRICES": json.dumps({GEM_MODEL: {"input": "0.5", "output": "2"}}),
        "AI_EXTERNAL_MAX_OUTPUT_TOKENS": 1000,
        "CHAT_DEFAULT_MODEL": "qwen2.5:3b",
        "MODEL_ROUTER_EXTERNAL_TIMEOUT_SECONDS": 2.0,
    }
    base.update(over)
    return Settings.model_validate(base)


# --- Anthropic adapter (official SDK over a mocked httpx2 transport) ---------------------------


def ant_sse(*texts: str, stop: str = "end_turn", out_tokens: int = 5) -> bytes:
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": ANT_MODEL,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 12, "output_tokens": 1},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    for t in texts:
        events.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": t},
                },
            )
        )
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": out_tokens},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def anthropic(handler: Callable[[httpx2.Request], httpx2.Response]) -> AnthropicClient:
    return AnthropicClient(
        ANT_KEY,
        base_url="https://anthropic.test",
        timeout=5,
        max_retries=0,
        default_max_tokens=777,
        prices={ANT_MODEL: PRICE},
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


async def collect(stream: AsyncIterator[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c async for c in stream]


async def test_anthropic_streams_text_then_usage_and_configured_cost() -> None:
    seen: list[httpx2.Request] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return httpx2.Response(
            200, content=ant_sse("Hello", " world"), headers={"content-type": "text/event-stream"}
        )

    c = anthropic(handler)
    msgs = [{"role": "system", "content": "Be brief."}, *PUBLIC]
    out = await collect(c.chat_stream(ANT_MODEL, msgs, temperature=0.3))
    assert [o["content"] for o in out if "content" in o] == ["Hello", " world"]
    done = out[-1]
    assert done["done"] and done["model"] == ANT_MODEL
    assert done["usage"]["prompt_tokens"] == 12 and done["usage"]["completion_tokens"] == 5
    assert done["usage"]["cost_usd"] == (12 * Decimal(3) + 5 * Decimal(15)) / Decimal(1_000_000)
    req = seen[0]
    assert req.url.path == "/v1/messages"
    # SDK 2.x sends the key as a bearer token, with API version 2026-10-21.
    assert req.headers["authorization"] == f"Bearer {ANT_KEY}"
    assert req.headers["anthropic-version"] == "2026-10-21"
    body = json.loads(req.content)
    assert body["system"] == "Be brief." and body["messages"] == PUBLIC
    assert body["max_tokens"] == 777 and body["stream"] is True
    # API version 2026-10-21 rejects sampling parameters: never sent.
    assert "temperature" not in body and "top_p" not in body
    await c.aclose()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "rejected"),
        (401, "rejected"),
        (403, "rejected"),
        (404, "rejected"),
        (429, "rate_limited"),
        (500, "upstream_error"),
        (529, "unavailable"),
    ],
)
async def test_anthropic_status_errors_map_without_echoing_the_body(status: int, code: str) -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        body = {"type": "error", "error": {"type": "x", "message": f"{ECHO} {ANT_KEY}"}}
        return httpx2.Response(status, json=body)

    with pytest.raises(ProviderError) as info:
        await collect(anthropic(handler).chat_stream(ANT_MODEL, PUBLIC))
    assert info.value.code == code and info.value.status == status
    text = f"{info.value} {info.value!r} {info.value.__cause__!r}"
    assert ECHO not in text and ANT_KEY not in text


async def test_anthropic_timeout_connection_and_refusal() -> None:
    def timeout(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow", request=req)

    def down(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=req)

    with pytest.raises(ProviderError) as t:
        await collect(anthropic(timeout).chat_stream(ANT_MODEL, PUBLIC))
    assert t.value.code == "timeout"
    with pytest.raises(ProviderError) as d:
        await collect(anthropic(down).chat_stream(ANT_MODEL, PUBLIC))
    assert d.value.code == "unavailable"

    def refusal(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=ant_sse("partial", stop="refusal"),
            headers={"content-type": "text/event-stream"},
        )

    with pytest.raises(ProviderRejected) as r:
        await collect(anthropic(refusal).chat_stream(ANT_MODEL, PUBLIC))
    assert r.value.code == "refused"


async def test_anthropic_lists_models_and_never_shows_the_key() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        assert req.url.path == "/v1/models"
        data = [{"id": "b-model", "type": "model"}, {"id": "a-model", "type": "model"}]
        return httpx2.Response(
            200, json={"data": data, "has_more": False, "first_id": None, "last_id": None}
        )

    c = anthropic(handler)
    assert await c.list_models() == ["a-model", "b-model"]
    assert ANT_KEY not in repr(c)


# --- Gemini adapter (official SDK over a mocked httpx transport) -------------------------------


def gem_sse(*chunks: dict[str, Any]) -> bytes:
    return "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks).encode()


def gem_chunk(
    text: str = "",
    *,
    thought: bool = False,
    finish: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    cand: dict[str, Any] = {
        "content": {"role": "model", "parts": [{"text": text, "thought": thought}] if text else []},
        "index": 0,
    }
    if finish:
        cand["finishReason"] = finish
    out: dict[str, Any] = {"candidates": [cand], "modelVersion": GEM_MODEL}
    if usage:
        out["usageMetadata"] = usage
    return out


def gemini(handler: Callable[[httpx.Request], httpx.Response]) -> GeminiClient:
    return GeminiClient(
        GEM_KEY,
        base_url="https://gemini.test",
        timeout=5,
        max_retries=0,
        default_max_tokens=555,
        prices={GEM_MODEL: ModelPrice(Decimal("0.5"), Decimal("2"))},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_gemini_streams_text_skips_thoughts_and_counts_thinking_tokens() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            content=gem_sse(
                gem_chunk("(thinking…)", thought=True),
                gem_chunk("Rain "),
                gem_chunk(
                    "falls.",
                    finish="STOP",
                    usage={
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 4,
                        "thoughtsTokenCount": 6,
                    },
                ),
            ),
            headers={"content-type": "text/event-stream"},
        )

    c = gemini(handler)
    msgs = [
        {"role": "system", "content": "Be brief."},
        *PUBLIC,
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "again"},
    ]
    out = await collect(c.chat_stream(GEM_MODEL, msgs, temperature=0.2))
    assert [o["content"] for o in out if "content" in o] == ["Rain ", "falls."]
    usage = out[-1]["usage"]
    assert usage["prompt_tokens"] == 10 and usage["completion_tokens"] == 10
    assert usage["cost_usd"] == (10 * Decimal("0.5") + 10 * Decimal(2)) / Decimal(1_000_000)
    req = seen[0]
    assert f"models/{GEM_MODEL}:streamGenerateContent" in req.url.path
    assert req.headers["x-goog-api-key"] == GEM_KEY
    assert GEM_KEY not in str(req.url)  # never in the URL (access logs)
    body = json.loads(req.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "Be brief."
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["generationConfig"]["maxOutputTokens"] == 555
    await c.aclose()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "rejected"),
        (401, "rejected"),
        (403, "rejected"),
        (429, "rate_limited"),
        (500, "upstream_error"),
    ],
)
async def test_gemini_status_errors_map_without_echoing_the_body(status: int, code: str) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json={"error": {"code": status, "message": f"{ECHO} {GEM_KEY}", "status": "X"}}
        )

    with pytest.raises(ProviderError) as info:
        await collect(gemini(handler).chat_stream(GEM_MODEL, PUBLIC))
    assert info.value.code == code
    text = f"{info.value} {info.value!r}"
    assert ECHO not in text and GEM_KEY not in text


async def test_gemini_safety_blocks_and_timeouts() -> None:
    def blocked(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gem_sse({"promptFeedback": {"blockReason": "SAFETY"}}))

    def safety_finish(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gem_sse(gem_chunk("x", finish="SAFETY")))

    def slow(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    for handler, code in ((blocked, "refused"), (safety_finish, "refused"), (slow, "timeout")):
        with pytest.raises(ProviderError) as info:
            await collect(gemini(handler).chat_stream(GEM_MODEL, PUBLIC))
        assert info.value.code == code


async def test_gemini_model_id_is_validated_before_any_request() -> None:
    calls: list[httpx.Request] = []
    c = gemini(lambda req: calls.append(req) or httpx.Response(500))  # type: ignore[func-returns-value]
    for bad in ("../v1/files", "gemini/../../x", "a b", ""):
        with pytest.raises(ProviderRejected):
            await collect(c.chat_stream(bad, PUBLIC))
    assert not calls
    assert normalize_model("models/gemini-x") == "gemini-x"


async def test_gemini_lists_generate_content_models() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "models/gemini-b", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/embed-x", "supportedGenerationMethods": ["embedContent"]},
                    {"name": "models/gemini-a", "supportedGenerationMethods": ["generateContent"]},
                ]
            },
        )

    assert await gemini(handler).list_models() == ["gemini-a", "gemini-b"]


# --- settings --------------------------------------------------------------------------------


def test_defaults_keep_every_external_provider_off(settings: Settings) -> None:
    for p in ("openrouter", "anthropic", "gemini"):
        assert settings.external_unusable_reason(p) == "disabled"
        assert build_external(settings, p) is None
    # Gemini 3.5 Flash-Lite is the default external provider/model, still off until enabled.
    assert settings.AI_DEFAULT_EXTERNAL_PROVIDER == "gemini"
    assert settings.GEMINI_DEFAULT_MODEL == "gemini-3.5-flash-lite"
    assert settings.AI_EXTERNAL_DAILY_LIMIT_USD is None


def test_prices_parse_from_json_and_reject_garbage(settings: Settings) -> None:
    s = prov_settings(settings)
    cfg = s.external("anthropic")
    assert cfg.prices == {ANT_MODEL: (Decimal(3), Decimal(15))}
    assert cfg.priced(ANT_MODEL) and not cfg.priced("claude-unpriced-model")
    assert cfg.models == (ANT_MODEL, "claude-unpriced-model")
    assert ANT_KEY not in repr(cfg)  # the key is not in the dataclass repr
    for bad in (
        '{"m": {"input": "x", "output": "1"}}',
        '{"m": {"input": "-1", "output": "1"}}',
        "[1]",
        "{",
    ):
        with pytest.raises(ValueError):
            prov_settings(settings, ANTHROPIC_MODEL_PRICES=bad)
    assert s.external("openrouter").reports_cost and s.external("openrouter").priced("any/x")


def test_unusable_reasons(settings: Settings) -> None:
    assert prov_settings(settings).external_unusable_reason("anthropic") is None
    assert (
        prov_settings(settings, ANTHROPIC_API_KEY=None).external_unusable_reason("anthropic")
        == "not_configured"
    )
    # A blank Gemini model is the primary model (gemini-3.5-flash-lite), not "not configured".
    blank = prov_settings(settings, GEMINI_DEFAULT_MODEL="")
    assert blank.external("gemini").default_model == "gemini-3.5-flash-lite"
    assert blank.external_unusable_reason("gemini") is None
    assert (
        prov_settings(settings, GEMINI_API_KEY=None).external_unusable_reason("gemini")
        == "not_configured"
    )
    assert (
        prov_settings(settings, GEMINI_DAILY_LIMIT_USD="0").external_unusable_reason("gemini")
        == "no_budget"
    )
    zero_global = prov_settings(settings, AI_EXTERNAL_DAILY_LIMIT_USD="0")
    assert zero_global.external_unusable_reason("anthropic") == "no_budget"


def test_settings_registry_secrets_reauth_and_overlay(settings: Settings) -> None:
    for key in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        spec = svc.REGISTRY[key]
        assert spec.secret and spec.reauth
    for key in (
        "ANTHROPIC_ENABLED", "ANTHROPIC_DEFAULT_MODEL", "ANTHROPIC_ALLOWED_MODELS",
        "ANTHROPIC_DAILY_LIMIT_USD", "ANTHROPIC_MODEL_PRICES", "GEMINI_ENABLED",
        "GEMINI_DEFAULT_MODEL", "GEMINI_ALLOWED_MODELS", "GEMINI_DAILY_LIMIT_USD",
        "GEMINI_MODEL_PRICES", "AI_DEFAULT_EXTERNAL_PROVIDER", "AI_EXTERNAL_DAILY_LIMIT_USD",
    ):  # fmt: skip
        assert key in svc.REGISTRY, key
    assert {"anthropic", "gemini", "ai"} <= set(svc.CATEGORIES)
    assert set(svc.CATEGORIES) <= set(svc._TESTS)
    prices = svc.normalize(svc.REGISTRY["GEMINI_MODEL_PRICES"], '{"g": {"input": 1, "output": 2}}')
    with pytest.raises(svc.SettingsError):
        svc.normalize(svc.REGISTRY["GEMINI_MODEL_PRICES"], "[1, 2]")
    s = svc.overlay(
        settings,
        {"GEMINI_API_KEY": GEM_KEY, "GEMINI_MODEL_PRICES": prices, "AGENT_NEWS_PROVIDER": "gemini"},
    )
    # The operator's map, on top of the one built-in verified price.
    assert s.external("gemini").prices == {
        "g": (Decimal(1), Decimal(2)),
        "gemini-3.5-flash-lite": (Decimal("0.30"), Decimal("2.50")),
    }
    assert json.loads(svc.current_value(s, "GEMINI_MODEL_PRICES") or "{}")["g"]["output"] == "2"
    assert GEM_KEY in svc.secret_values(s)  # registered with the log scrubber
    masked = svc.mask(svc.current_value(s, "GEMINI_API_KEY"), True)
    assert masked is not None and GEM_KEY not in masked


async def test_settings_connection_tests(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.inference as inference

    class Lister(FakeExternal):
        def __init__(self, fail: ProviderError | None = None) -> None:
            super().__init__("anthropic")
            self.fail = fail

        async def list_models(self) -> list[str]:
            if self.fail:
                raise self.fail
            return [ANT_MODEL, "claude-unpriced-model"]

    holder: dict[str, Any] = {"client": Lister()}
    monkeypatch.setattr(inference, "build_external", lambda s, p, **kw: holder["client"])
    http = httpx.AsyncClient()
    s = prov_settings(settings)
    assert (await svc.run_test("anthropic", s, http)).ok
    unpriced = prov_settings(settings, ANTHROPIC_DEFAULT_MODEL="claude-unpriced-model")
    out = await svc.run_test("anthropic", unpriced, http)
    assert not out.ok and "MODEL_PRICES" in out.detail
    holder["client"] = Lister(ProviderRejected("Anthropic refused the credentials (HTTP 401)", 401))
    out = await svc.run_test("anthropic", s, http)
    assert not out.ok and "401" in out.detail and ANT_KEY not in out.detail
    nokey = prov_settings(settings, ANTHROPIC_API_KEY=None)
    assert not (await svc.run_test("anthropic", nokey, http)).ok
    assert (
        await svc.run_test(
            "ai", prov_settings(settings, AI_DEFAULT_EXTERNAL_PROVIDER="gemini"), http
        )
    ).ok
    await http.aclose()


# --- the gate --------------------------------------------------------------------------------


def gate(
    s: Settings, provider: str, redis: FakeRedis | None = None, client: Any = "x"
) -> ExternalGate:
    c = FakeExternal(provider) if client == "x" else client
    return ExternalGate(s, c, UsageTracker(redis or FakeRedis()), provider=provider)


async def test_gate_reasons_in_order(settings: Settings) -> None:
    s = prov_settings(settings)
    assert (
        await gate(prov_settings(settings, ANTHROPIC_ENABLED=False), "anthropic").decide(
            ANT_MODEL, PUBLIC
        )
    ).reason == "disabled"
    assert (
        await gate(s, "anthropic", client=None).decide(ANT_MODEL, PUBLIC)
    ).reason == "not_configured"
    assert (await gate(s, "anthropic").decide("claude-other", PUBLIC)).reason == "model_not_allowed"
    assert (
        await gate(s, "anthropic").decide("claude-unpriced-model", PUBLIC)
    ).reason == "no_pricing"
    secret = [{"role": "user", "content": f"my key is {GEM_KEY}"}]
    d = await gate(s, "gemini").decide(GEM_MODEL, secret)
    assert d.reason == "filter" and "secrets" in d.categories and d.local_only
    finance = [{"role": "user", "content": "summarise my bank statement and salary"}]
    assert (await gate(s, "anthropic").decide(ANT_MODEL, finance)).reason == "filter"
    assert (
        await gate(s, "anthropic").decide(ANT_MODEL, PUBLIC, context="trading")
    ).reason == "context"
    assert (
        await gate(s, "anthropic", FakeRedis(fail=True)).decide(ANT_MODEL, PUBLIC)
    ).reason == "usage_unavailable"
    ok = await gate(s, "anthropic").decide(ANT_MODEL, PUBLIC)
    assert ok.external and ok.provider == "anthropic" and ok.reserved_micro > 0


async def test_per_provider_cap_and_local_rate_limit(settings: Settings) -> None:
    redis = FakeRedis()
    s = prov_settings(settings, AI_EXTERNAL_REQUESTS_PER_MINUTE=2)
    redis.data[day_key(utc_today(), "anthropic")] = {"cost_micro": 1_000_000}
    assert (
        await gate(s, "anthropic", redis).decide(ANT_MODEL, PUBLIC)
    ).reason == "daily_cap_reached"
    # Gemini's own counter is untouched by Anthropic's spend.
    g = gate(s, "gemini", redis)
    assert (await g.decide(GEM_MODEL, PUBLIC)).external
    assert (await g.decide(GEM_MODEL, PUBLIC)).external
    assert (await g.decide(GEM_MODEL, PUBLIC)).reason == "rate_limited_local"


async def test_global_cap_counts_every_provider(settings: Settings) -> None:
    redis = FakeRedis()
    s = prov_settings(settings, AI_EXTERNAL_DAILY_LIMIT_USD="0.50")
    redis.data[day_key(utc_today(), "openrouter")] = {"cost_micro": 500_000}
    d = await gate(s, "gemini", redis).decide(GEM_MODEL, PUBLIC)
    assert d.reason == "global_cap_reached"
    assert not redis.data.get(reserve_key(utc_today(), "gemini"), {}).get("micro")  # released


async def test_concurrent_calls_cannot_jointly_pass_the_cap(settings: Settings) -> None:
    redis = FakeRedis()
    s = prov_settings(settings, ANTHROPIC_DAILY_LIMIT_USD="0.10", AI_EXTERNAL_REQUESTS_PER_MINUTE=0)
    est = estimate_cost(s, "anthropic", ANT_MODEL, PUBLIC, None)
    assert est is not None and est > 0
    allowed = int(Decimal("0.10") / est)
    g = gate(s, "anthropic", redis)
    decisions = await asyncio.gather(*(g.decide(ANT_MODEL, PUBLIC) for _ in range(allowed + 5)))
    passed = [d for d in decisions if d.external]
    assert len(passed) == allowed
    assert {d.reason for d in decisions if not d.external} == {"daily_cap_reached"}
    # Recording the actual (smaller) cost releases each reservation.
    usage = UsageTracker(redis, "anthropic")
    for d in passed:
        await usage.record(
            cost_usd=Decimal("0.0001"),
            prompt_tokens=1,
            completion_tokens=1,
            failed=False,
            reserved_micro=d.reserved_micro,
        )
    assert await usage.reserved_micro() == 0
    assert (await usage.today()).cost_micro == 100 * len(passed)


async def test_openrouter_gate_is_backward_compatible(settings: Settings) -> None:
    s = prov_settings(
        settings,
        OPENROUTER_ENABLED=True,
        OPENROUTER_API_KEY="sk-or-v1-" + "a" * 40,
        OPENROUTER_DEFAULT_MODEL="google/gemini-2.5-flash-lite",
        OPENROUTER_DAILY_LIMIT_USD="0.5",
    )
    # The original three-argument constructor still means OpenRouter, priced by OpenRouter.
    g = ExternalGate(s, FakeExternal("openrouter"), UsageTracker(FakeRedis()))
    d = await g.decide("google/gemini-2.5-flash-lite", PUBLIC)
    assert d.external and d.provider == "openrouter" and d.reserved_micro == 0


# --- the chat stream: local is the only fallback --------------------------------------------


def job(provider: str, model: str, **kw: Any) -> ChatJob:
    import uuid

    return ChatJob(
        conversation_id=uuid.uuid4(), user_message_ids=[1], model=model, prompt=list(PUBLIC),
        options={}, provider=provider, requested_provider=provider, requested_model=model,
        fallback_model="qwen2.5:3b", **kw,
    )  # fmt: skip


async def drain(stream: ChatStream) -> list[tuple[str, Any]]:
    await stream._produce()
    out = []
    while not stream._queue.empty():
        out.append(stream._queue.get_nowait())
    return out


async def test_chat_falls_back_to_local_never_to_another_provider() -> None:
    redis = FakeRedis()
    failing = FakeExternal("anthropic", fail=ProviderError("Anthropic returned HTTP 500", 500))
    other = FakeExternal("gemini")
    ollama = FakeOllama()
    stream = ChatStream(
        job("anthropic", ANT_MODEL, reserved_micro=5000),
        ollama=ollama,  # type: ignore[arg-type]
        maker=None,  # type: ignore[arg-type]
        ticket=None,
        timeout=5,
        heartbeat=1,
        limiter=GenerationLimiter(1, 2, 5),
        external=failing,  # type: ignore[arg-type]
        usage=UsageTracker(redis),
    )
    redis.data[reserve_key(utc_today(), "anthropic")] = {"micro": 5000}
    events = await drain(stream)
    fallback = [d for k, d in events if k == "fallback"]
    assert fallback and fallback[0]["provider"] == "ollama"
    assert stream.turn.provider == "ollama" and stream.turn.fallback_reason == "upstream_error"
    assert "".join(stream.turn.parts) == "Local answer."
    assert failing.calls == 1 and other.calls == 0 and ollama.calls == ["qwen2.5:3b"]
    usage = UsageTracker(redis, "anthropic")
    assert (await usage.today()).failures == 1 and await usage.reserved_micro() == 0


async def test_chat_streams_from_gemini_and_charges_its_counter() -> None:
    redis = FakeRedis()
    client = FakeExternal("gemini", cost="0.0025")
    stream = ChatStream(
        job("gemini", GEM_MODEL),
        ollama=FakeOllama(),  # type: ignore[arg-type]
        maker=None,  # type: ignore[arg-type]
        ticket=None,
        timeout=5,
        heartbeat=1,
        external=client,  # type: ignore[arg-type]
        usage=UsageTracker(redis),
    )
    await drain(stream)
    assert stream.turn.status == "complete" and stream.turn.provider == "gemini"
    assert stream.turn.cost_usd == Decimal("0.0025") and stream.external is not None
    assert stream.external.provider == "gemini"
    assert (await UsageTracker(redis, "gemini").today()).cost_micro == 2500
    assert (await UsageTracker(redis, "openrouter").today()).calls == 0


# --- the model router -------------------------------------------------------------------------


def router(
    s: Settings, clients: dict[str, Any], redis: FakeRedis
) -> tuple[ModelRouter, FakeOllama]:
    ollama = FakeOllama()
    r = ModelRouter(
        s,
        ollama=ollama,  # type: ignore[arg-type]
        openrouter=clients.get("openrouter"),
        usage=UsageTracker(redis),
        maker=None,
        limiter=GenerationLimiter(1, 2, 5),
        external=clients,
    )
    return r, ollama


async def test_router_uses_the_agents_provider_and_falls_back_locally(settings: Settings) -> None:
    redis = FakeRedis()
    ant, gem = FakeExternal("anthropic"), FakeExternal("gemini")
    s = prov_settings(
        settings,
        AGENT_NEWS_PROVIDER="anthropic",
        AGENT_VOICE_PROVIDER="auto",
        AI_DEFAULT_EXTERNAL_PROVIDER="gemini",
    )
    r, ollama = router(s, {"anthropic": ant, "gemini": gem}, redis)
    a = await r.complete("news", PUBLIC, context="news")
    assert (a.provider, a.model) == ("anthropic", ANT_MODEL) and ant.calls == 1
    v = await r.complete("voice", PUBLIC)
    assert (v.provider, v.model) == ("gemini", GEM_MODEL) and gem.calls == 1
    assert (await UsageTracker(redis, "anthropic").today()).cost_micro == 1000
    # A provider error goes to local Ollama, never to the other external provider.
    ant.fail = ProviderError("down", 503)
    b = await r.complete("news", PUBLIC, context="news")
    assert b.provider == "ollama" and b.fallback and b.route_reason == "upstream_error"
    assert gem.calls == 1 and ollama.calls
    # Personal data never leaves, whatever the provider.
    p = await r.complete(
        "news", [{"role": "user", "content": "my Dhan positions and P&L"}], context="news"
    )
    assert p.provider == "ollama" and p.local_only


async def test_router_charges_real_openrouter_cost_regression(settings: Settings) -> None:
    """Before docs/26 the router re-parsed the client's already-parsed usage and lost
    `cost_usd`, so agent calls were never charged against the OpenRouter cap."""
    redis = FakeRedis()
    s = prov_settings(
        settings,
        OPENROUTER_ENABLED=True,
        OPENROUTER_API_KEY="sk-or-v1-" + "b" * 40,
        OPENROUTER_DEFAULT_MODEL="google/gemini-2.5-flash-lite",
        OPENROUTER_DAILY_LIMIT_USD="1",
        AGENT_NEWS_PROVIDER="openrouter",
    )
    r, _ = router(s, {"openrouter": FakeExternal("openrouter", cost="0.0042")}, redis)
    a = await r.complete("news", PUBLIC, context="news")
    assert a.provider == "openrouter"
    assert (await UsageTracker(redis).today()).cost_micro == 4200


# --- secrets ----------------------------------------------------------------------------------


def test_keys_are_scrubbed_from_logs_redaction_and_outbound_payloads() -> None:
    for key in (ANT_KEY, GEM_KEY):
        assert key not in str(scrub({"msg": f"oops {key}"}))
        assert key not in redact(f"token={key}")
        verdict = classify_chat([{"role": "user", "content": f"use {key} please"}])
        assert not verdict.external_ok and "secrets" in verdict.categories


async def test_runtime_reload_rebuilds_only_the_changed_provider(settings: Settings) -> None:
    from app.services import app_settings_runtime as rt

    old = prov_settings(settings)
    state = SimpleNamespace(
        settings=old, openrouter=None, github=None, anthropic=build_external(old, "anthropic"),
        gemini=build_external(old, "gemini"), trading_adapters={}, redis=None, sessionmaker=None,
    )  # fmt: skip
    app = SimpleNamespace(state=state)
    gem_before, ant_before = state.gemini, state.anthropic
    new = prov_settings(settings, ANTHROPIC_DAILY_LIMIT_USD="2")
    changed = rt.apply(app, new)  # type: ignore[arg-type]
    assert changed == ["ANTHROPIC_DAILY_LIMIT_USD"]
    assert state.anthropic is not ant_before and state.gemini is gem_before
    off = prov_settings(settings, ANTHROPIC_ENABLED=False)
    rt.apply(app, off)  # type: ignore[arg-type]
    assert state.anthropic is None
    for task in list(getattr(state, "settings_retiring", ())):
        task.cancel()


def test_logged_errors_never_carry_the_key(settings: Settings) -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        configure_logging("INFO")
        get_logger("t").warning("external_fallback", detail=f"x {ANT_KEY} {GEM_KEY}")
    logging.getLogger().handlers.clear()
    out = buf.getvalue()
    assert ANT_KEY not in out and GEM_KEY not in out


async def test_integrations_card_reports_providers_without_calling_them(settings: Settings) -> None:
    from app.services.integrations import IntegrationsSummary

    card = IntegrationsSummary(prov_settings(settings), None, None, None)  # type: ignore[arg-type]
    sec = await card.ai_providers()
    assert sec["providers"]["anthropic"]["usable"] and sec["status"] == "ok"
    bad = IntegrationsSummary(
        prov_settings(settings, GEMINI_DEFAULT_MODEL="gemini-unpriced"), None, None, None
    )  # type: ignore[arg-type]
    sec = await bad.ai_providers()
    assert sec["status"] == "degraded" and sec["providers"]["gemini"]["reason"] == "no_pricing"
    assert ANT_KEY not in json.dumps(sec, default=str) and GEM_KEY not in json.dumps(
        sec, default=str
    )
    assert SecretStr  # imported for parity with the OpenRouter tests
