"""AI Research Agent: explains a stored signal or backtest with the local Ollama model.

Explanation only. It reads stored rows, sends them to a model, and writes
trading.ai_analyses. The external providers (AI_DEFAULT_EXTERNAL_PROVIDER, then
AI_EXTERNAL_FALLBACK_PROVIDERS; docs/30) are tried first, in order, ONLY when
AI_RESEARCH_EXTERNAL_ALLOWED=true and each gate allows the payload (the content filter still
blocks broker data, positions, finance records, secrets and logs); local Ollama answers when
every one refuses or fails. It has no code path to order intents, risk decisions or signals: its
output can never create or approve anything. The numbers it explains are the deterministic
ones already stored; the prompt tells the model not to invent figures.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Mapping
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.ollama.client import OllamaClient
from app.agents.providers.base import ProviderError
from app.core.logging import get_logger
from app.models.trading import (
    AiAnalysis,
    BacktestRun,
    Instrument,
    RiskDecision,
    StrategyVersion,
    TradingSignal,
)
from app.services import audit
from app.services.inference import Decision, ExternalGate

log = get_logger(__name__)

QUEUE_KEY = "research:ai:queue"
Kind = Literal["signal", "backtest"]

SYSTEM = (
    "You are a trading research assistant. Explain the deterministic analysis you are given "
    "in plain language for a trader. Use only the numbers provided; never invent prices, "
    "statistics or news. Do not recommend placing, sizing or approving any order. Mention the "
    "main risks and what would invalidate the setup. Start with a one-sentence summary line."
)


class QueueFull(Exception):
    pass


async def enqueue(redis: Redis, kind: Kind, subject_id: uuid.UUID, limit: int) -> int:
    """Bounded queue: refuses when `limit` requests are already waiting."""
    if int(await redis.llen(QUEUE_KEY)) >= limit:
        raise QueueFull
    return int(await redis.lpush(QUEUE_KEY, json.dumps({"kind": kind, "id": str(subject_id)})))


async def _context(db: AsyncSession, kind: Kind, subject_id: uuid.UUID) -> dict[str, Any] | None:
    if kind == "signal":
        sig = await db.get(TradingSignal, subject_id)
        if sig is None:
            return None
        inst = await db.get(Instrument, sig.instrument_id)
        version = await db.get(StrategyVersion, sig.strategy_version_id)
        risks = (
            await db.scalars(select(RiskDecision).where(RiskDecision.signal_id == sig.id))
        ).all()
        return {
            "type": "signal",
            "instrument": inst.symbol if inst else None,
            "timeframe": sig.timeframe,
            "candle_time_utc": sig.candle_time.isoformat(),
            "session": sig.session,
            "side": sig.side,
            "strategy": version.code_ref if version else None,
            "params": version.params if version else None,
            "inputs": sig.inputs,
            "risk_decisions": [
                {"approved": r.approved, "reason": r.reason, "checks": r.checks} for r in risks
            ],
        }
    run = await db.get(BacktestRun, subject_id)
    if run is None or run.status != "done":
        return None
    inst = await db.get(Instrument, run.instrument_id)
    version = await db.get(StrategyVersion, run.strategy_version_id)
    return {
        "type": "backtest",
        "instrument": inst.symbol if inst else None,
        "timeframe": run.timeframe,
        "period": [run.period_start.isoformat(), run.period_end.isoformat()],
        "strategy": version.code_ref if version else None,
        "params": run.params,
        "costs": run.costs,
        "metrics": run.metrics,
    }


async def explain(
    maker: async_sessionmaker[AsyncSession],
    ollama: OllamaClient,
    model: str,
    kind: Kind,
    subject_id: uuid.UUID,
    *,
    limit_s: float,
    external: Mapping[str, ExternalGate] | ExternalGate | None = None,
) -> uuid.UUID | None:
    async with maker() as db:
        ctx = await _context(db, kind, subject_id)
    if ctx is None:
        return None
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(ctx, default=str, indent=1)},
    ]
    content = None
    gates = _as_gates(external)
    if gates:
        settings = next(iter(gates.values())).settings
        if settings.AI_RESEARCH_EXTERNAL_ALLOWED:
            chain = settings.external_chain(settings.AI_DEFAULT_EXTERNAL_PROVIDER)
            got = await _try_external(maker, gates, chain, messages, kind, subject_id, limit_s)
            if got is not None:
                content, model = got
    if content is None:
        content = await _local(ollama, model, messages, limit_s)
    if not content:
        raise RuntimeError("model returned an empty explanation")
    summary = content.splitlines()[0].strip()[:300]
    async with maker() as db, db.begin():
        row = AiAnalysis(
            signal_id=subject_id if kind == "signal" else None,
            backtest_run_id=subject_id if kind == "backtest" else None,
            model=model[:200],
            summary=summary,
            content=content,
        )
        db.add(row)
    return row.id


def _as_gates(
    external: Mapping[str, ExternalGate] | ExternalGate | None,
) -> dict[str, ExternalGate]:
    """One gate (the original OpenRouter-only form) or the gates by provider."""
    if external is None:
        return {}
    if isinstance(external, ExternalGate):
        return {external.provider: external}
    return dict(external)


async def _try_external(
    maker: async_sessionmaker[AsyncSession],
    gates: Mapping[str, ExternalGate],
    chain: list[str],
    messages: list[dict[str, str]],
    kind: Kind,
    subject_id: uuid.UUID,
    limit_s: float,
) -> tuple[str, str] | None:
    """(explanation, "provider:model") from the first provider in the chain that answers,
    or None (refused or failed everywhere: use Ollama). docs/30."""
    for provider in chain:
        gate = gates.get(provider)
        if gate is None:
            continue
        model = gate.default_model
        decision = await gate.decide(model, messages, context="research")
        if not decision.external or gate.client is None or gate.usage is None:
            log.info(
                "research_ai_skip",
                provider=provider,
                reason=decision.reason,
                categories=decision.categories,
            )
            await gate.release(decision)
            if decision.local_only:
                return None
            continue
        text = await _call(maker, gate, model, decision, messages, kind, subject_id, limit_s)
        if text is not None:
            return text, f"{provider}:{model}"
    return None


async def _call(
    maker: async_sessionmaker[AsyncSession],
    gate: ExternalGate,
    model: str,
    decision: Decision,
    messages: list[dict[str, str]],
    kind: Kind,
    subject_id: uuid.UUID,
    limit_s: float,
) -> str | None:
    assert gate.client is not None and gate.usage is not None
    parts: list[str] = []
    usage: dict[str, Any] = {}
    outcome = "ok"
    t0 = time.monotonic()
    try:
        async with asyncio.timeout(limit_s):
            async for chunk in gate.client.chat_stream(model, messages, temperature=0.2):
                if chunk.get("done"):
                    usage = chunk.get("usage") or {}
                    break
                parts.append(str(chunk.get("content") or ""))
    except ProviderError as exc:
        outcome = f"error:{exc.code}"
    except TimeoutError:
        outcome = "timeout"
    cost = usage.get("cost_usd")
    await gate.usage.record(
        cost_usd=cost,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        failed=outcome != "ok",
        reserved_micro=decision.reserved_micro,
    )
    async with maker() as db, db.begin():  # metadata only, never the content
        audit.record(
            db,
            "ai.external_call",
            audit.ClientInfo(),
            details={
                "provider": gate.provider,
                "model": model,
                "purpose": f"research:{kind}",
                "subject_id": str(subject_id),
                "outcome": outcome,
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "cost_usd": str(cost) if cost is not None else None,
                "latency_ms": int((time.monotonic() - t0) * 1000),
            },
        )
    text = "".join(parts).strip()
    return text if outcome == "ok" and text else None


async def _local(
    ollama: OllamaClient, model: str, messages: list[dict[str, str]], limit_s: float
) -> str:
    parts: list[str] = []

    async def collect() -> None:
        async for chunk in ollama.chat_stream(model, messages, {"temperature": 0.2}):
            parts.append(str((chunk.get("message") or {}).get("content") or ""))
            if chunk.get("done"):
                break

    async with asyncio.timeout(limit_s):
        await collect()
    return "".join(parts).strip()
