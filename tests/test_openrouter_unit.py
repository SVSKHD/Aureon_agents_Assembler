"""OpenRouter: client streaming/errors, the daily cap, the gate, and the key never leaking."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from contextlib import redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.agents.openrouter.client import (
    OpenRouterClient,
    OpenRouterError,
    OpenRouterRateLimited,
    OpenRouterRejected,
    OpenRouterTimeout,
    OpenRouterUnavailable,
    parse_usage,
)
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger, scrub
from app.integrations.github.redact import redact
from app.services.ai_usage import UsageTracker, day_key, to_micro
from app.services.inference import ExternalGate, build_openrouter, local_fallback_model

BASE = "https://openrouter.test/api/v1"
KEY = "sk-or-v1-" + "9f8e7d6c5b4a3f2e1d0c" * 3
MODEL = "google/gemini-2.5-flash-lite"


class FakeRedis:
    def __init__(self, fail: bool = False) -> None:
        self.data: dict[str, dict[str, int]] = {}
        self.ttl: dict[str, int] = {}
        self.fail = fail

    async def hgetall(self, name: str) -> dict[bytes, bytes]:
        if self.fail:
            raise ConnectionError("redis down")
        return {k.encode(): str(v).encode() for k, v in self.data.get(name, {}).items()}

    async def hincrby(self, name: str, key: str, amount: int = 1) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        h = self.data.setdefault(name, {})
        h[key] = h.get(key, 0) + amount
        return h[key]

    async def expire(self, name: str, time: int) -> bool:
        self.ttl[name] = time
        return True


def sse(*chunks: dict[str, Any] | str) -> bytes:
    out = [": OPENROUTER PROCESSING\n\n"]
    for c in chunks:
        out.append(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n")
    return "".join(out).encode()


def delta(text: str) -> dict[str, Any]:
    return {"model": MODEL, "choices": [{"delta": {"content": text}, "finish_reason": None}]}


USAGE = {
    "model": MODEL,
    "choices": [{"delta": {"content": ""}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 2, "total_tokens": 13, "cost": 0.0000123},
}


def or_settings(settings: Settings, **over: Any) -> Settings:
    base = {
        **settings.model_dump(),
        "OPENROUTER_ENABLED": True,
        "OPENROUTER_API_KEY": KEY,
        "OPENROUTER_BASE_URL": BASE,
        "OPENROUTER_DEFAULT_MODEL": MODEL,
        "OPENROUTER_ALLOWED_MODELS": "openai/gpt-4.1-nano, qwen/qwen3.7-flash",
        "OPENROUTER_DAILY_LIMIT_USD": "0.50",
        "CHAT_DEFAULT_MODEL": "qwen2.5:3b",
    }
    base.update(over)
    return Settings.model_validate(base)


@pytest.fixture
def client() -> Iterator[OpenRouterClient]:
    yield OpenRouterClient(BASE, SecretStr(KEY), timeout=5)


async def collect(c: OpenRouterClient) -> list[dict[str, Any]]:
    return [x async for x in c.chat_stream(MODEL, [{"role": "user", "content": "hi"}])]


# --- client ---------------------------------------------------------------------------------


@respx.mock
async def test_stream_yields_text_then_usage_and_asks_for_usage(client: OpenRouterClient) -> None:
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, content=sse(delta("o"), delta("k"), USAGE, "[DONE]"))
    )
    out = await collect(client)
    assert [x["content"] for x in out if "content" in x] == ["o", "k"]
    done = out[-1]
    assert done["done"] and done["usage"]["prompt_tokens"] == 11
    assert done["usage"]["cost_usd"] == Decimal("0.0000123")
    sent = json.loads(route.calls[0].request.content)
    assert sent["usage"] == {"include": True} and sent["stream"] is True
    assert route.calls[0].request.headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in repr(client)


@pytest.mark.parametrize(
    ("status", "exc", "code"),
    [
        (429, OpenRouterRateLimited, "rate_limited"),
        (500, OpenRouterError, "upstream_error"),
        (503, OpenRouterError, "upstream_error"),
        (401, OpenRouterRejected, "rejected"),
        (402, OpenRouterRejected, "rejected"),
        (400, OpenRouterRejected, "rejected"),
    ],
)
@respx.mock
async def test_http_errors_map_to_codes(
    client: OpenRouterClient, status: int, exc: type[OpenRouterError], code: str
) -> None:
    body = {"error": {"message": f"bad key {KEY}", "code": status}}  # an echoing upstream
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(status, json=body))
    with pytest.raises(exc) as info:
        await collect(client)
    assert info.value.code == code
    assert KEY not in str(info.value) and KEY not in info.value.message


@respx.mock
async def test_mid_stream_error_chunk_raises(client: OpenRouterClient) -> None:
    err = {"error": {"code": 502, "message": "provider died"}}
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, content=sse(delta("par"), err))
    )
    with pytest.raises(OpenRouterError) as info:
        await collect(client)
    assert info.value.code == "upstream_error"


@respx.mock
async def test_timeout_and_transport_errors(client: OpenRouterClient) -> None:
    route = respx.post(f"{BASE}/chat/completions")
    route.side_effect = httpx.ReadTimeout("slow")
    with pytest.raises(OpenRouterTimeout):
        await collect(client)
    route.side_effect = httpx.ConnectError("down")
    with pytest.raises(OpenRouterUnavailable):
        await collect(client)


def test_parse_usage_tolerates_garbage() -> None:
    assert parse_usage(None) == {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": None}
    assert parse_usage({"cost": "nan"})["cost_usd"] is None
    assert parse_usage({"cost": -1})["cost_usd"] is None
    assert parse_usage({"cost": 0.5, "prompt_tokens": 3})["cost_usd"] == Decimal("0.5")


# --- settings and the daily cap --------------------------------------------------------------


def test_settings_defaults_keep_everything_local(settings: Settings) -> None:
    assert settings.OPENROUTER_ENABLED is False
    assert settings.OPENROUTER_DAILY_LIMIT_USD == 0
    assert settings.AI_RESEARCH_EXTERNAL_ALLOWED is False
    assert settings.OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"
    assert settings.openrouter_unusable_reason() == "disabled"
    assert build_openrouter(settings) is None


def test_unusable_reasons(settings: Settings) -> None:
    assert or_settings(settings).openrouter_unusable_reason() is None
    assert or_settings(settings, OPENROUTER_API_KEY="").openrouter_unusable_reason() == (
        "not_configured"
    )
    assert or_settings(settings, OPENROUTER_DEFAULT_MODEL="").openrouter_unusable_reason() == (
        "not_configured"
    )
    assert or_settings(settings, OPENROUTER_DAILY_LIMIT_USD="0").openrouter_unusable_reason() == (
        "no_budget"
    )
    s = or_settings(settings)
    assert s.openrouter_models == [MODEL, "openai/gpt-4.1-nano", "qwen/qwen3.7-flash"]
    assert KEY not in repr(s) and KEY not in str(s.model_dump())


async def test_usage_counter_and_cap() -> None:
    redis = FakeRedis()
    tracker = UsageTracker(redis)
    now = datetime(2026, 11, 3, 23, 59, tzinfo=UTC)
    assert await tracker.precheck(Decimal("0"), now) == "no_budget"
    assert await tracker.precheck(Decimal("0.01"), now) is None
    await tracker.record(
        cost_usd=Decimal("0.006"), prompt_tokens=100, completion_tokens=20, failed=False, now=now
    )
    await tracker.record(cost_usd=None, prompt_tokens=0, completion_tokens=0, failed=True, now=now)
    u = await tracker.today(now)
    assert (u.calls, u.failures, u.prompt_tokens, u.completion_tokens) == (2, 1, 100, 20)
    assert u.spent_usd == Decimal("0.006")
    assert redis.ttl[day_key(now.date())] > 24 * 3600
    assert await tracker.precheck(Decimal("0.01"), now) is None
    await tracker.record(
        cost_usd=Decimal("0.004"), prompt_tokens=1, completion_tokens=1, failed=False, now=now
    )
    assert await tracker.precheck(Decimal("0.01"), now) == "daily_cap_reached"
    # a new UTC day starts from zero
    tomorrow = datetime(2026, 11, 4, 0, 1, tzinfo=UTC)
    assert await tracker.precheck(Decimal("0.01"), tomorrow) is None
    assert to_micro(Decimal("0.0000005")) == 1


async def test_unreadable_counter_fails_closed() -> None:
    tracker = UsageTracker(FakeRedis(fail=True))
    assert await tracker.precheck(Decimal("1")) == "usage_unavailable"
    # recording never raises into the chat
    await tracker.record(cost_usd=Decimal("1"), prompt_tokens=1, completion_tokens=1, failed=False)


# --- the gate -------------------------------------------------------------------------------


async def test_gate_order_of_reasons(settings: Settings, client: OpenRouterClient) -> None:
    msgs = [{"role": "user", "content": "write a poem"}]
    redis = FakeRedis()
    gate = ExternalGate(or_settings(settings), client, UsageTracker(redis))
    assert (await gate.decide(MODEL, msgs)).external
    assert (await gate.decide("openai/gpt-4.1-nano", msgs)).external
    assert (await gate.decide("evil/model", msgs)).reason == "model_not_allowed"
    d = await gate.decide(MODEL, [{"role": "user", "content": "my P&L is down"}])
    assert (d.external, d.reason, d.categories, d.local_only) == (
        False,
        "filter",
        ["positions"],
        True,
    )
    d = await gate.decide(MODEL, msgs, context="finance")
    assert (d.reason, d.local_only) == ("context", True)
    redis.data[day_key(datetime.now(UTC).date())] = {"cost_micro": 500_000}
    d = await gate.decide(MODEL, msgs)
    assert (d.reason, d.local_only) == ("daily_cap_reached", False)

    off = ExternalGate(settings, None, UsageTracker(redis))
    assert (await off.decide(MODEL, msgs)).reason == "disabled"
    no_client = ExternalGate(or_settings(settings), None, UsageTracker(redis))
    assert (await no_client.decide(MODEL, msgs)).reason == "not_configured"
    no_budget = ExternalGate(
        or_settings(settings, OPENROUTER_DAILY_LIMIT_USD="0"), client, UsageTracker(redis)
    )
    assert (await no_budget.decide(MODEL, msgs)).reason == "no_budget"


async def test_research_external_needs_the_flag(
    settings: Settings, client: OpenRouterClient
) -> None:
    msgs = [{"role": "user", "content": "explain this backtest of the EMA strategy"}]
    gate = ExternalGate(or_settings(settings), client, UsageTracker(FakeRedis()))
    assert (await gate.decide(MODEL, msgs, context="research")).reason == "context"
    gate = ExternalGate(
        or_settings(settings, AI_RESEARCH_EXTERNAL_ALLOWED=True), client, UsageTracker(FakeRedis())
    )
    assert (await gate.decide(MODEL, msgs, context="research")).external


def test_local_fallback_model(settings: Settings) -> None:
    s = or_settings(settings)
    assert local_fallback_model(s, ["mistral:7b", "qwen2.5:3b"]) == "qwen2.5:3b"
    assert local_fallback_model(s, ["mistral:7b"]) == "mistral:7b"
    assert local_fallback_model(s, []) is None
    assert local_fallback_model(s, None) == "qwen2.5:3b"


# --- the key never leaks ----------------------------------------------------------------------


@respx.mock
async def test_key_never_reaches_logs_or_errors(settings: Settings) -> None:
    configure_logging("DEBUG")
    s = or_settings(settings)
    buf = io.StringIO()
    with redirect_stdout(buf):
        c = build_openrouter(s)
        assert c is not None
        log = get_logger("leak-test")
        respx.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}})
        )
        try:
            await collect(c)
        except OpenRouterError as exc:
            log.warning("openrouter_failed", error=str(exc), code=exc.code)
            log.exception("with traceback", exc_info=exc)
        # The mistakes a future change could make: all scrubbed.
        log.info("settings", settings=s.model_dump(), key=KEY)
        log.info(f"oops {KEY}")
        log.info("hdr", headers={"Authorization": f"Bearer {KEY}"})
        log.info("cfg", openrouter_api_key=KEY, client=repr(c))
        logging.getLogger("stdlib").warning("stdlib %s", KEY)
        await c.aclose()
    out = buf.getvalue()
    assert "openrouter_failed" in out
    assert KEY not in out
    assert KEY[9:] not in out  # not even the part after the prefix
    assert KEY not in json.dumps(scrub({"a": [KEY]}))
    assert KEY not in redact(f"discord text {KEY}")
