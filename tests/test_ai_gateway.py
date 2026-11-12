"""The Claude gateway (app/ai/gateway.py): the only door to api.anthropic.com. No network:
a scripted fake client (tests/fake_claude.py) stands in for the SDK."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from app.ai import pricing
from app.ai.budget import MemoryUsageStore
from app.ai.gateway import (
    BudgetRefused,
    ClaudeGateway,
    GatewayError,
    ModelNotPriced,
    NotConfigured,
    RateLimited,
    Refused,
)
from app.core.config import Settings
from tests.fake_claude import FakeClaude, message, text_reply

ROOT = Path(__file__).resolve().parents[1]
KEY = "sk-ant-api03-" + "k" * 40


def _s(settings: Settings, **kw: Any) -> Settings:
    return settings.model_copy(update={"ANTHROPIC_API_KEY": SecretStr(KEY), **kw})


def _gw(
    settings: Settings, fake: FakeClaude, store: MemoryUsageStore | None = None
) -> ClaudeGateway:
    return ClaudeGateway(settings, store or MemoryUsageStore(), client=fake)


async def _call(gw: ClaudeGateway, **kw: Any) -> Any:
    args: dict[str, Any] = {
        "agent": "general",
        "budget": "chat",
        "system": "You are a test.",
        "messages": [{"role": "user", "content": "hello"}],
    }
    return await gw.call(**{**args, **kw})


def test_only_the_gateway_imports_the_sdk_or_names_the_host() -> None:
    allowed = ROOT / "app" / "ai" / "gateway.py"
    for path in (ROOT / "app").rglob("*.py"):
        if path == allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] == "anthropic" for a in node.names), path
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "anthropic", path
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                assert "api.anthropic.com" not in node.value, path


async def test_caching_effort_and_no_sampling_parameters(settings: Settings) -> None:
    fake = FakeClaude([text_reply("hi")])
    tools = [
        {"name": "b_tool", "description": "b", "input_schema": {"type": "object"}},
        {"name": "a_tool", "description": "a", "input_schema": {"type": "object"}},
    ]
    res = await _call(_gw(_s(settings), fake), tools=tools)
    assert res.text == "hi"
    sent = fake.calls[0]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in sent["tools"][0]  # one breakpoint, on the last tool
    assert sent["cache_control"] == {"type": "ephemeral"}  # the conversation, auto-cached
    assert sent["output_config"] == {"effort": "medium"}
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(sent)


async def test_usage_is_settled_with_real_tokens_and_cost(settings: Settings) -> None:
    s = _s(settings)
    store = MemoryUsageStore()
    reply = text_reply(
        "ok", input_tokens=1000, output_tokens=500, cache_read=4000, cache_write=2000
    )
    res = await _call(_gw(s, FakeClaude([reply]), store))
    want = (
        Decimal(1000) * 4 + Decimal(500) * 20 + Decimal(4000) * Decimal("0.20")
        + Decimal(2000) * 4 * Decimal("1.25")
    ) / 1_000_000  # fmt: skip
    assert res.cost_usd == want.quantize(Decimal("0.000001"))
    (budget, reserved, settled) = store.rows[res.usage_id]
    assert budget == "chat" and reserved >= res.cost_usd
    assert settled is not None and settled.outcome == "ok"
    assert (settled.input_tokens, settled.output_tokens) == (1000, 500)
    assert (settled.cache_read_tokens, settled.cache_write_tokens) == (4000, 2000)
    assert (await store.spend()).by_budget["chat"] == res.cost_usd


def test_haiku_long_prompt_tier_and_unpriced_models(settings: Settings) -> None:
    short = pricing.TokenUsage(input_tokens=1000)
    long = pricing.TokenUsage(input_tokens=150_000)
    assert pricing.cost_usd(settings, "claude-haiku-5-5", short) == Decimal("0.000100")
    assert pricing.cost_usd(settings, "claude-haiku-5-5", long) == Decimal("0.075000")
    with pytest.raises(pricing.NotPriced):
        pricing.cost_usd(settings, "claude-unknown", short)
    own = settings.model_copy(
        update={"AI_MODEL_PRICES": {"claude-x": {"input": Decimal(1), "output": Decimal(2)}}}
    )
    assert own.claude_price("claude-x") == (Decimal(1), Decimal(2), Decimal("0.1"))


async def test_unpriced_model_is_never_called(settings: Settings) -> None:
    fake = FakeClaude([text_reply("no")])
    with pytest.raises(ModelNotPriced):
        await _call(_gw(_s(settings), fake), model="claude-unknown")
    assert fake.calls == []


async def test_not_configured_without_a_key(settings: Settings) -> None:
    gw = ClaudeGateway(settings, MemoryUsageStore())
    assert not gw.configured
    with pytest.raises(NotConfigured):
        await _call(gw)


async def test_budget_refusal_sends_nothing(settings: Settings) -> None:
    s = _s(settings, AI_AGENT_BUDGETS_USD={"chat": Decimal("0.01")})
    fake = FakeClaude([text_reply("never")])
    with pytest.raises(BudgetRefused, match="chat AI budget"):
        await _call(_gw(s, fake))
    assert fake.calls == []


async def test_redaction_reaches_the_request(settings: Settings) -> None:
    fake = FakeClaude([text_reply("ok")])
    msgs = [
        {"role": "user", "content": "Customer: Ravi Kumar, ravi@example.com, +91 98480 22338"},
        {"role": "assistant", "content": [{"type": "text", "text": "kept as-is: a@b.co"}]},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "pan ABCDE1234F"}],
        },
    ]
    await _call(_gw(_s(settings), fake), messages=msgs)
    sent = fake.calls[0]["messages"]
    assert sent[0]["content"] == "Customer: [NAME], [EMAIL], [PHONE]"
    assert sent[1]["content"][0]["text"] == "kept as-is: a@b.co"  # Claude's own turn: untouched
    assert sent[2]["content"][0]["content"] == "pan [ACCOUNT_ID]"


async def test_rate_limit_is_translated_and_settled_as_error(settings: Settings) -> None:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.RateLimitError(
        "slow down", response=httpx2.Response(429, request=req), body=None
    )
    store = MemoryUsageStore()
    with pytest.raises(RateLimited):
        await _call(_gw(_s(settings), FakeClaude([err]), store))
    ((_, _, settled),) = store.rows.values()
    assert settled is not None and settled.outcome == "error" and settled.error == "rate_limited"
    assert (await store.spend()).total == 0  # a failed call costs nothing


async def test_server_error_is_translated(settings: Settings) -> None:
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.InternalServerError(
        "boom", response=httpx2.Response(500, request=req), body=None
    )
    with pytest.raises(GatewayError) as info:
        await _call(_gw(_s(settings), FakeClaude([err])))
    assert info.value.code == "upstream_error"


async def test_refusal_is_recorded_and_raised(settings: Settings) -> None:
    store = MemoryUsageStore()
    fake = FakeClaude([message([], stop_reason="refusal", output_tokens=5)])
    with pytest.raises(Refused):
        await _call(_gw(_s(settings), fake, store))
    ((_, _, settled),) = store.rows.values()
    assert settled is not None and settled.outcome == "refused" and settled.cost_usd > 0


def test_real_client_gets_retries_and_timeout(settings: Settings) -> None:
    gw = ClaudeGateway(_s(settings, AI_MAX_RETRIES=4, AI_TIMEOUT_SECONDS=33.0), MemoryUsageStore())
    client: Any = gw._client
    assert client.max_retries == 4
    assert client.timeout == 33.0
    assert KEY not in repr(gw)


async def test_stream_text_is_forwarded(settings: Settings) -> None:
    got: list[str] = []

    async def on_text(t: str) -> None:
        got.append(t)

    await _call(_gw(_s(settings), FakeClaude([text_reply("streamed")])), on_text=on_text)
    assert got == ["streamed"]
