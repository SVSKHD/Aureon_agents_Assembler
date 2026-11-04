"""GET /api/v1/ai/usage: model calls per agent and provider (docs/model-routing.md).

Admin only. Totals from ai.model_calls (metadata only: no prompt or answer is stored) plus
today's OpenRouter spend against the shared OPENROUTER_DAILY_LIMIT_USD.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import case, func, select

from app.api.ui import ui_module
from app.auth.deps import AdminRequired, DbSession
from app.models.model_calls import ModelCall
from app.services.ai_usage import UsageTracker

UI_MODULE = ui_module("ai-usage", label="AI usage", group="ai", icon="gauge", order=46)
router = APIRouter(prefix="/v1/ai", tags=["ai"])


class AgentUsage(BaseModel):
    agent: str
    provider: str
    calls: int
    failures: int
    fallbacks: int
    local_only: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    avg_latency_ms: int


class UsageOut(BaseModel):
    since: datetime
    daily_limit_usd: Decimal
    spent_today_usd: Decimal | None
    items: list[AgentUsage]


@router.get("/usage", response_model=UsageOut)
async def usage(
    request: Request,
    _: AdminRequired,
    db: DbSession,
    days: Annotated[int, Query(ge=1, le=90)] = 1,
) -> UsageOut:
    """Per agent and provider over the last `days`: calls, failures, local fallbacks,
    local-only (kept local by the data filter), tokens, cost, average latency."""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                ModelCall.agent,
                ModelCall.provider,
                func.count(),
                func.sum(case((ModelCall.outcome != "ok", 1), else_=0)),
                func.sum(case((ModelCall.fallback, 1), else_=0)),
                func.sum(case((ModelCall.local_only, 1), else_=0)),
                func.coalesce(func.sum(ModelCall.prompt_tokens), 0),
                func.coalesce(func.sum(ModelCall.completion_tokens), 0),
                func.coalesce(func.sum(ModelCall.cost_usd), 0),
                func.coalesce(func.avg(ModelCall.latency_ms), 0),
            )
            .where(ModelCall.at >= since)
            .group_by(ModelCall.agent, ModelCall.provider)
            .order_by(ModelCall.agent, ModelCall.provider)
        )
    ).all()
    spent: Decimal | None
    try:
        tracker = getattr(request.app.state, "ai_usage", None) or UsageTracker(
            request.app.state.redis
        )
        spent = (await tracker.today()).spent_usd
    except Exception:
        spent = None
    return UsageOut(
        since=since,
        daily_limit_usd=request.app.state.settings.OPENROUTER_DAILY_LIMIT_USD,
        spent_today_usd=spent,
        items=[
            AgentUsage(
                agent=a,
                provider=p,
                calls=int(n),
                failures=int(f or 0),
                fallbacks=int(fb or 0),
                local_only=int(lo or 0),
                prompt_tokens=int(pt),
                completion_tokens=int(ct),
                cost_usd=Decimal(str(cost)),
                avg_latency_ms=int(lat),
            )
            for a, p, n, f, fb, lo, pt, ct, cost, lat in rows
        ],
    )
