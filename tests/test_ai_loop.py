"""The agent loop and tool registry (app/ai/loop.py, app/ai/tools/registry.py), without a
database: tool-call rows fail to record (logged, never fatal) and approvals are faked."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from pydantic import SecretStr

from app.ai import loop
from app.ai.budget import MemoryUsageStore
from app.ai.gateway import ClaudeGateway
from app.ai.tools import default_registry
from app.ai.tools.registry import (
    Draft,
    Registry,
    Tool,
    ToolContext,
    ToolInputError,
    ToolRefused,
    obj,
    validate,
)
from app.core.config import Settings
from tests.fake_claude import FakeClaude, text_reply, tool_reply


class NoDb:
    """A sessionmaker whose sessions fail: tool-call audit rows are best effort."""

    def __call__(self) -> Any:
        raise RuntimeError("no database in unit tests")


def _ctx(settings: Settings, agent: str = "investments") -> ToolContext:
    return ToolContext(
        settings=settings,
        maker=NoDb(),  # type: ignore[arg-type]
        user_id=uuid.uuid4(),
        is_admin=True,
        agent=agent,
    )


def _gw(settings: Settings, fake: FakeClaude) -> ClaudeGateway:
    s = settings.model_copy(update={"ANTHROPIC_API_KEY": SecretStr("sk-ant-" + "k" * 30)})
    return ClaudeGateway(s, MemoryUsageStore(), client=fake)


calls: list[str] = []


async def _read(ctx: ToolContext, args: dict[str, Any]) -> str:
    calls.append(f"read:{args.get('n')}")
    await asyncio.sleep(0)
    return f"result {args.get('n')} for customer@example.com"


async def _long(ctx: ToolContext, args: dict[str, Any]) -> str:
    return "x" * 100_000


async def _refuse(ctx: ToolContext, args: dict[str, Any]) -> str:
    raise ToolRefused("not connected")


async def _write(ctx: ToolContext, args: dict[str, Any]) -> Draft:
    calls.append("write")
    return Draft("do_thing", {"n": args["n"]}, f"do thing {args['n']}")


AGENTS = frozenset({"investments"})
REG = Registry(
    [
        Tool("read_n", "read", obj({"n": {"type": "integer"}}, ["n"]), _read, "read", AGENTS),
        Tool("long", "long", obj({}), _long, "read", AGENTS),
        Tool("refuse", "refuse", obj({}), _refuse, "read", AGENTS),
        Tool("write_n", "write", obj({"n": {"type": "integer"}}, ["n"]), _write, "write", AGENTS),
        Tool("hidden", "off", obj({}), _read, "read", AGENTS, lambda s: False),
        Tool("other", "other agent", obj({}), _read, "read", frozenset({"business"})),
    ]
)


@pytest.fixture(autouse=True)
def _reset() -> None:
    calls.clear()


async def _run(settings: Settings, fake: FakeClaude, **kw: Any) -> loop.LoopResult:
    events: list[tuple[str, dict[str, Any]]] = kw.pop("events", [])

    async def on_event(kind: str, data: dict[str, Any]) -> None:
        events.append((kind, data))

    return await loop.run(
        _gw(settings, fake),
        REG,
        _ctx(settings),
        system="sys",
        budget="investments",
        history=[{"role": "user", "content": "go"}],
        on_event=on_event,
        **kw,
    )


async def test_reads_run_and_all_results_return_in_one_message(settings: Settings) -> None:
    fake = FakeClaude(
        [tool_reply([("read_n", {"n": 1}), ("read_n", {"n": 2})]), text_reply("done")]
    )
    res = await _run(settings, fake)
    assert res.text == "done" and res.steps == 2 and res.stop_reason == "end_turn"
    assert sorted(calls) == ["read:1", "read:2"]
    second = fake.calls[1]["messages"]
    results = second[-1]["content"]
    assert [r["type"] for r in results] == ["tool_result", "tool_result"]
    assert "[EMAIL]" in results[0]["content"] and "example.com" not in results[0]["content"]
    # Only this agent's available tools are offered, in name order.
    assert [t["name"] for t in fake.calls[0]["tools"]] == ["long", "read_n", "refuse", "write_n"]


async def test_write_tool_only_files_an_approval(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    filed: list[Draft] = []

    class Row:
        id = uuid.uuid4()

    async def fake_file(ctx: ToolContext, draft: Draft, call_id: int | None) -> Row:
        filed.append(draft)
        return Row()

    monkeypatch.setattr(loop.approvals, "file", fake_file)
    events: list[tuple[str, dict[str, Any]]] = []
    fake = FakeClaude([tool_reply([("write_n", {"n": 7})]), text_reply("asked")])
    res = await _run(settings, fake, events=events)
    assert [d.action for d in filed] == ["do_thing"] and res.approvals == [Row.id]
    content = fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert content.startswith("Awaiting user approval") and "Nothing has been done" in content
    assert (
        "approval",
        {"id": str(Row.id), "action": "do_thing", "summary": "do thing 7"},
    ) in events


async def test_bad_input_and_refusals_are_errors_not_crashes(settings: Settings) -> None:
    fake = FakeClaude(
        [
            tool_reply([("read_n", {"n": "x"}), ("refuse", {}), ("nope", {})]),
            text_reply("sorry"),
        ]
    )
    res = await _run(settings, fake)
    results = fake.calls[1]["messages"][-1]["content"]
    assert all(r.get("is_error") for r in results)
    assert "invalid input" in results[0]["content"]
    assert results[1]["content"] == "not connected"
    assert "unknown tool" in results[2]["content"]
    assert calls == [] and res.text == "sorry"


async def test_tool_results_are_capped(settings: Settings) -> None:
    s = settings.model_copy(update={"AI_TOOL_RESULT_MAX_TOKENS": 100})
    fake = FakeClaude([tool_reply([("long", {})]), text_reply("ok")])
    await _run(s, fake)
    content = fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert len(content) <= 100 * 4 and content.endswith("for more]")


async def test_step_limit_closes_every_tool_call(settings: Settings) -> None:
    s = settings.model_copy(update={"AI_MAX_STEPS": 3})
    fake = FakeClaude([tool_reply([("read_n", {"n": i})]) for i in range(5)])
    res = await _run(s, fake)
    assert res.steps == 3 and len(fake.calls) == 3 and res.stop_reason == "max_steps"
    last = res.messages[-1]
    assert last["role"] == "user" and last["content"][0]["content"] == loop.STEP_LIMIT
    assert "stopped after 3 steps" in res.text
    # Every tool_use has its tool_result, so the next turn is a valid request.
    uses = sum(1 for m in res.messages if m["role"] == "assistant" for b in m["content"]
               if b["type"] == "tool_use")  # fmt: skip
    results = sum(1 for m in res.messages if m["role"] == "user" for _ in m["content"])
    assert uses == results


def test_validator() -> None:
    schema = obj(
        {
            "side": {"type": "string", "enum": ["BUY", "SELL"]},
            "qty": {"type": "integer", "minimum": 1},
            "price": {"type": "number", "exclusiveMinimum": 0},
        },
        ["side", "qty"],
    )
    validate(schema, {"side": "BUY", "qty": 2, "price": 1.5})
    for bad in (
        {"side": "BUY"},
        {"side": "HOLD", "qty": 1},
        {"side": "BUY", "qty": 0},
        {"side": "BUY", "qty": True},
        {"side": "BUY", "qty": 1, "price": 0},
        {"side": "BUY", "qty": 1, "extra": 1},
    ):
        with pytest.raises(ToolInputError):
            validate(schema, bad)


def test_default_tools_match_the_plan(settings: Settings) -> None:
    reg = default_registry()
    names = {t.name: t for t in reg.all()}
    assert {n for n, t in names.items() if t.kind == "write"} == {
        "propose_trade",
        "draft_aquakart_blog",
        "add_todo",
    }
    offered = {t.name for t in reg.for_agent("investments", settings)}
    assert offered == {
        "get_portfolio_summary",
        "get_positions",
        "get_dhan_screener_results",
        "get_mt5_bot_status",
        "get_mt5_recent_signals",
        "propose_trade",
    }
    # Not connected yet: never offered, so no tokens are spent on them.
    assert reg.for_agent("business", settings) == []
    assert "propose_trade" not in {t.name for t in reg.for_agent("report", settings)}
