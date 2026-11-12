"""AI Research Agent: explains a stored signal or backtest, through the Claude gateway.

Explanation only. It reads stored rows, sends them to Claude (app/ai/gateway.py: redacted,
budgeted under "investments", logged in ai.usage) and writes trading.ai_analyses. It has no
code path to order intents, risk decisions or signals: its output can never create or approve
anything. The numbers it explains are the deterministic ones already stored; the prompt tells
the model not to invent figures.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.agents import BUDGET_OF
from app.ai.gateway import ClaudeGateway
from app.core.logging import get_logger
from app.models.trading import (
    AiAnalysis,
    BacktestRun,
    Instrument,
    RiskDecision,
    StrategyVersion,
    TradingSignal,
)

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


def unavailable_reason(settings: Any) -> str | None:
    """Why explanations cannot run at all, or None."""
    if not settings.anthropic_key:
        return "ANTHROPIC_API_KEY is not set (explanations use Claude)"
    return None


async def explain(
    maker: async_sessionmaker[AsyncSession],
    gateway: ClaudeGateway,
    kind: Kind,
    subject_id: uuid.UUID,
) -> uuid.UUID | None:
    """One explanation from Claude, stored in trading.ai_analyses (its id), or None when the
    subject no longer exists. Gateway errors (budget, upstream) propagate to the worker."""
    async with maker() as db:
        ctx = await _context(db, kind, subject_id)
    if ctx is None:
        return None
    res = await gateway.call(
        agent="research",
        budget=BUDGET_OF["research"],
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps(ctx, default=str, indent=1)}],
        max_tokens=4096,
    )
    content = res.text
    if not content:
        raise RuntimeError("model returned an empty explanation")
    summary = content.splitlines()[0].strip()[:300]
    async with maker() as db, db.begin():
        row = AiAnalysis(
            signal_id=subject_id if kind == "signal" else None,
            backtest_run_id=subject_id if kind == "backtest" else None,
            model=gateway.settings.AI_MODEL_MAIN[:200],
            summary=summary,
            content=content,
        )
        db.add(row)
    log.info("research_explained", kind=kind, cost_usd=str(res.cost_usd))
    return row.id


__all__ = ["QUEUE_KEY", "SYSTEM", "Kind", "QueueFull", "enqueue", "explain", "unavailable_reason"]
