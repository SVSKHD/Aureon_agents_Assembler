"""AI Research Agent: explains a stored signal or backtest (Gemini first where permitted).

Explanation only. It reads stored rows, sends them to a model, and writes
trading.ai_analyses. The external providers (the research agent's provider,
AGENT_RESEARCH_PROVIDER, default gemini = Gemini 3.5 Flash-Lite; then
AI_EXTERNAL_FALLBACK_PROVIDERS; docs/30) are tried first, in order, ONLY when
AI_RESEARCH_EXTERNAL_ALLOWED=true and each gate allows the payload (the content filter still
blocks broker data, positions, finance records, secrets and logs); local Ollama answers when
every one refuses or fails, never after a successful external answer.
AGENT_RESEARCH_PROVIDER=ollama keeps explanations local. Every attempt is a row in
ai.model_calls (agent "trading-research": provider, model, outcome, route reason, fallback;
never content). It has no code path to order intents, risk decisions or signals: its
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
from app.models.model_calls import ModelCall
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
AGENT = "trading-research"  # ai.model_calls agent name
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


def unavailable_reason(settings: Any) -> str | None:
    """Why explanations cannot run at all, or None. They can run with a local model
    (RESEARCH_AI_MODEL), or without one when external research is permitted
    (AI_RESEARCH_EXTERNAL_ALLOWED, provider not "ollama") and a provider in the chain is
    usable (enabled, key, model, budget). The content filter still decides per request."""
    local = bool(settings.RESEARCH_AI_MODEL) and settings.AI_LOCAL_ENABLED
    if local:
        return None
    no_local = (
        "RESEARCH_AI_MODEL is not set"
        if settings.AI_LOCAL_ENABLED
        else "local models are disabled (AI_LOCAL_ENABLED)"
    )
    if not settings.AI_RESEARCH_EXTERNAL_ALLOWED:
        return f"{no_local} and AI_RESEARCH_EXTERNAL_ALLOWED is false"
    provider = str(settings.AGENT_RESEARCH_PROVIDER)
    if provider == "ollama":
        return f"{no_local} and AGENT_RESEARCH_PROVIDER is ollama"
    first = provider if provider in _PROVIDERS else str(settings.AI_DEFAULT_EXTERNAL_PROVIDER)
    reasons = {p: settings.external_unusable_reason(p) for p in settings.external_chain(first)}
    if all(reasons.values()):
        detail = ", ".join(f"{p}: {r}" for p, r in reasons.items())
        return f"{no_local} and no external provider is usable ({detail})"
    return None


class NoModel(RuntimeError):
    """No external provider answered and no local model is configured."""


async def explain(
    maker: async_sessionmaker[AsyncSession],
    ollama: OllamaClient,
    model: str | None,
    kind: Kind,
    subject_id: uuid.UUID,
    *,
    limit_s: float,
    external: Mapping[str, ExternalGate] | ExternalGate | None = None,
) -> uuid.UUID | None:
    """`model` is the local Ollama model (RESEARCH_AI_MODEL); None/"" = no local fallback
    (the worker passes None when AI_LOCAL_ENABLED is off)."""
    async with maker() as db:
        ctx = await _context(db, kind, subject_id)
    if ctx is None:
        return None
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(ctx, default=str, indent=1)},
    ]
    content = None
    trace: dict[str, Any] = {"reason": None, "fallback": False, "local_only": False}
    gates = _as_gates(external)
    if gates:
        settings = next(iter(gates.values())).settings
        provider = str(settings.AGENT_RESEARCH_PROVIDER)
        if not settings.AI_RESEARCH_EXTERNAL_ALLOWED:
            trace["reason"] = "research_external_not_allowed"
        elif provider == "ollama":
            trace["reason"] = "forced_local"
        else:
            first = (
                provider if provider in _PROVIDERS else str(settings.AI_DEFAULT_EXTERNAL_PROVIDER)
            )
            chain = settings.external_chain(first)
            agent_model = str(settings.AGENT_RESEARCH_MODEL or "").strip() or None
            got = await _try_external(
                maker,
                gates,
                chain,
                messages,
                kind,
                subject_id,
                limit_s,
                first_model=agent_model,
                trace=trace,
            )
            if got is not None:
                content, model = got
    if content is None:
        if not model:
            await _record(maker, "ollama", "?", "error", trace, route_reason="no_local_model")
            raise NoModel(
                "no external provider answered and RESEARCH_AI_MODEL (local) is not set"
                + (f" ({trace['reason']})" if trace["reason"] else "")
            )
        t0 = time.monotonic()
        try:
            content = await _local(ollama, model, messages, limit_s)
        except Exception:
            await _record(maker, "ollama", model, "error", trace, t0=t0)
            raise
        await _record(maker, "ollama", model, "ok", trace, t0=t0)
    if not content:
        raise RuntimeError("model returned an empty explanation")
    summary = content.splitlines()[0].strip()[:300]
    async with maker() as db, db.begin():
        row = AiAnalysis(
            signal_id=subject_id if kind == "signal" else None,
            backtest_run_id=subject_id if kind == "backtest" else None,
            model=(model or "?")[:200],
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


_PROVIDERS = ("openrouter", "anthropic", "gemini")


async def _try_external(
    maker: async_sessionmaker[AsyncSession],
    gates: Mapping[str, ExternalGate],
    chain: list[str],
    messages: list[dict[str, str]],
    kind: Kind,
    subject_id: uuid.UUID,
    limit_s: float,
    *,
    first_model: str | None = None,
    trace: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """(explanation, "provider:model") from the first provider in the chain that answers,
    or None (refused or failed everywhere: use Ollama). docs/30. `first_model` overrides the
    first provider's default model (AGENT_RESEARCH_MODEL). `trace` collects why the answer
    is not external (route reason, fallback, local_only) for the ai.model_calls record."""
    trace = trace if trace is not None else {}
    for i, provider in enumerate(chain):
        gate = gates.get(provider)
        if gate is None:
            continue
        model = (first_model if i == 0 else None) or gate.default_model
        decision = await gate.decide(model, messages, context="research")
        if not decision.external or gate.client is None or gate.usage is None:
            log.info(
                "research_ai_skip",
                provider=provider,
                reason=decision.reason,
                categories=decision.categories,
            )
            trace["reason"] = trace.get("reason") or decision.reason or "unavailable"
            await gate.release(decision)
            if decision.local_only:
                trace["local_only"] = True
                return None
            continue
        text = await _call(maker, gate, model, decision, messages, kind, subject_id, limit_s)
        if text is not None:
            return text, f"{provider}:{model}"
        trace["fallback"] = True
        trace["reason"] = trace.get("reason") or "external_failed"
    return None


async def _record(
    maker: async_sessionmaker[AsyncSession],
    provider: str,
    model: str,
    outcome: str,
    trace: Mapping[str, Any],
    *,
    t0: float | None = None,
    route_reason: str | None = None,
) -> None:
    """The local attempt's ai.model_calls row (best effort, metadata only)."""
    reason = route_reason or trace.get("reason")
    try:
        async with maker() as db, db.begin():
            db.add(
                ModelCall(
                    agent=AGENT,
                    provider=provider,
                    model=model[:200],
                    outcome=outcome,
                    route_reason=str(reason)[:40] if reason else None,
                    fallback=bool(trace.get("fallback")),
                    local_only=bool(trace.get("local_only")),
                    latency_ms=int((time.monotonic() - t0) * 1000) if t0 else 0,
                )
            )
    except Exception as exc:
        log.warning("model_call_not_recorded", error=type(exc).__name__)


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
        db.add(
            ModelCall(
                agent=AGENT,
                provider=gate.provider,
                model=model[:200],
                outcome="ok" if outcome == "ok" else outcome.split(":", 1)[0],
                route_reason=outcome.split(":", 1)[1][:40] if ":" in outcome else None,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                latency_ms=int((time.monotonic() - t0) * 1000),
                cost_usd=cost,
                fallback=False,
                local_only=False,
            )
        )
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
