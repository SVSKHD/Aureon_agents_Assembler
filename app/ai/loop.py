"""The agent loop: Claude asks for tools, the server runs them, results go back, repeat.

At most AI_MAX_STEPS model calls per turn. Each step:

1. gateway.call with the agent's tools (fixed order, cached) and the conversation so far;
2. Claude's whole content (thinking blocks included, unchanged) is appended;
3. if it asked for tools: read tools run in parallel, write tools one at a time (each files
   an approval, see app/ai/approvals.py); every result is a short, redacted summary capped at
   AI_TOOL_RESULT_MAX_TOKENS, and ALL results go back in one user message;
4. otherwise the turn is done.

When the last allowed step still asks for tools, each call gets an error result ("step limit
reached") so the history stays valid for the next turn, and the turn ends.
Every tool call is an ai.tool_calls row (input + summary, never a raw table).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.ai import approvals
from app.ai.gateway import ClaudeGateway
from app.ai.tools.registry import (
    Draft,
    Registry,
    Suggestion,
    Tool,
    ToolContext,
    ToolInputError,
    ToolRefused,
    cap,
    validate,
)
from app.core.logging import get_logger
from app.models.ai import ToolCall

log = get_logger("ai.loop")

OnEvent = Callable[[str, dict[str, Any]], Awaitable[None]]
STEP_LIMIT = "step limit reached: no more tools this turn; answer with what you have"


@dataclass(slots=True)
class LoopResult:
    text: str
    messages: list[dict[str, Any]]  # the API messages this turn added (as sent / received)
    steps: int
    cost_usd: Decimal
    stop_reason: str
    approvals: list[uuid.UUID] = field(default_factory=list)


async def _record(ctx: ToolContext, row: ToolCall) -> int | None:
    try:
        async with ctx.maker() as db, db.begin():
            db.add(row)
            await db.flush()
            return int(row.id)
    except Exception as exc:  # the answer matters more than the audit row
        log.warning("ai_tool_call_not_recorded", error=type(exc).__name__)
        return None


def _result(tool_use_id: str, content: str, *, error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if error:
        block["is_error"] = True
    return block


async def run_tool(
    ctx: ToolContext,
    tools: dict[str, Tool],
    use: Any,
    on_event: OnEvent | None,
    filed: list[uuid.UUID],
) -> dict[str, Any]:
    name, args, tool_use_id = use.name, use.input, use.id
    tool = tools.get(name)
    t0 = time.monotonic()
    status, error = "ok", False
    if tool is None:
        return _result(tool_use_id, f"unknown tool {name!r}", error=True)
    try:
        if not isinstance(args, dict):
            raise ToolInputError("input must be an object")
        validate(tool.schema, args)
        out = await tool.handler(ctx, args)
        if isinstance(out, Draft):
            if tool.kind != "write":
                raise TypeError("a read tool returned a draft")
            status = "awaiting_approval"
            summary = out.summary
        elif isinstance(out, Suggestion):
            summary = out.summary
        else:
            summary = ctx.redactor.text(cap(str(out), ctx.settings))
    except ToolInputError as exc:
        status, error, summary, out = "error", True, f"invalid input: {exc}", None
    except ToolRefused as exc:
        status, error, summary, out = "refused", True, str(exc), None
    except Exception as exc:
        log.warning("ai_tool_failed", tool=name, error=type(exc).__name__)
        status, error, summary, out = "error", True, f"{name} failed ({type(exc).__name__})", None
    row = ToolCall(
        conversation_id=ctx.conversation_id,
        agent=ctx.agent,
        tool=name,
        kind=tool.kind,
        tool_use_id=tool_use_id,
        input=args if isinstance(args, dict) else {"value": str(args)[:200]},
        status=status,
        summary=summary[:4000],
        latency_ms=int((time.monotonic() - t0) * 1000),
    )
    call_id = await _record(ctx, row)
    if isinstance(out, Draft):
        try:
            approval = await approvals.file(ctx, out, call_id)
        except ToolRefused as exc:
            status, error, summary = "refused", True, str(exc)
        else:
            filed.append(approval.id)
            summary = (
                f"Awaiting user approval (request {approval.id}). Nothing has been done yet. "
                f"{out.summary}"
            )
            if on_event is not None:
                await on_event(
                    "approval",
                    {"id": str(approval.id), "action": out.action, "summary": out.summary},
                )
    if isinstance(out, Suggestion) and on_event is not None:
        # A card for the user to confirm; the app saves it, never the server or Claude.
        fields = {
            k: ctx.redactor.text(v) if isinstance(v, str) else v for k, v in out.fields.items()
        }
        await on_event("suggestion", {"kind": out.kind, "fields": fields})
    if on_event is not None:
        await on_event("tool", {"name": name, "status": status})
    return _result(tool_use_id, summary, error=error)


async def run(
    gateway: ClaudeGateway,
    registry: Registry,
    ctx: ToolContext,
    *,
    system: str,
    budget: str,
    history: list[dict[str, Any]],
    model: str | None = None,
    on_event: OnEvent | None = None,
) -> LoopResult:
    settings = ctx.settings
    offered = registry.for_agent(ctx.agent, settings)
    by_name = {t.name: t for t in offered}
    api_tools = [t.api() for t in offered]
    msgs = list(history)
    added: list[dict[str, Any]] = []
    filed: list[uuid.UUID] = []
    cost = Decimal(0)
    text, stop = "", "end_turn"

    async def on_text(piece: str) -> None:
        if on_event is not None:
            await on_event("text", {"delta": piece})

    steps = 0
    while steps < settings.AI_MAX_STEPS:
        steps += 1
        res = await gateway.call(
            agent=ctx.agent,
            budget=budget,
            system=system,
            messages=msgs,
            tools=api_tools,
            model=model,
            on_text=on_text,
            conversation_id=ctx.conversation_id,
            redactor=ctx.redactor,
        )
        cost += res.cost_usd
        assistant = {"role": "assistant", "content": res.blocks()}
        msgs.append(assistant)
        added.append(assistant)
        text = res.text or text
        stop = res.message.stop_reason or "end_turn"
        uses = [b for b in res.message.content if b.type == "tool_use"]
        if stop != "tool_use" or not uses:
            break
        if steps >= settings.AI_MAX_STEPS:
            results = [_result(u.id, STEP_LIMIT, error=True) for u in uses]
            stop = "max_steps"
        else:
            reads = [u for u in uses if by_name.get(u.name) and by_name[u.name].kind == "read"]
            done = dict(
                zip(
                    (u.id for u in reads),
                    await asyncio.gather(
                        *(run_tool(ctx, by_name, u, on_event, filed) for u in reads)
                    ),
                    strict=True,
                )
            )
            for u in uses:  # writes (and unknown tools) one at a time, in Claude's order
                if u.id not in done:
                    done[u.id] = await run_tool(ctx, by_name, u, on_event, filed)
            results = [done[u.id] for u in uses]
        user = {"role": "user", "content": results}
        msgs.append(user)
        added.append(user)
    if stop == "max_steps":
        text = (text + "\n\n" if text else "") + (
            f"(I stopped after {settings.AI_MAX_STEPS} steps; ask a narrower question for more.)"
        )
    return LoopResult(text, added, steps, cost, stop, filed)


__all__ = ["STEP_LIMIT", "LoopResult", "OnEvent", "run", "run_tool"]
