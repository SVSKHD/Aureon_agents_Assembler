"""Scheduled reports (scheduler jobs ai-daily-report, ai-weekly-review; docs/26).

Each run is one "report" agent turn with the read tools, charged to the reserve budget, and
stored as a conversation owned by the first active admin, so it shows up in the AI
conversation list. Read-only: the report agent has no write tools.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import chat
from app.ai.budget import IST
from app.ai.gateway import ClaudeGateway
from app.ai.tools import default_registry
from app.ai.tools.registry import ToolContext
from app.core.config import Settings
from app.models.auth import ROLE_ADMIN, Role, User

Kind = Literal["daily", "weekly"]

PROMPTS: dict[Kind, str] = {
    "daily": (
        "Write this morning's report: portfolio totals, open positions, the latest screener "
        "matches worth a look, the MT5 bot's state and last day's signals, and the business "
        "summary. End with up to three things to watch today."
    ),
    "weekly": (
        "Write the weekly review for the week that just ended: portfolio change, positions "
        "held, what the screener surfaced, the MT5 bot's signals over 7 days with outcomes, "
        "and the business summary. End with lessons and a short plan for next week."
    ),
}


async def report_owner(maker: async_sessionmaker[AsyncSession]) -> User | None:
    async with maker() as db:
        owner: User | None = await db.scalar(
            select(User)
            .join(Role, Role.id == User.role_id)
            .where(Role.name == ROLE_ADMIN, User.is_active.is_(True))
            .order_by(User.created_at)
            .limit(1)
        )
    return owner


async def run_report(
    gateway: ClaudeGateway,
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    kind: Kind,
) -> dict[str, Any]:
    if not gateway.configured:
        return {"skipped": "ANTHROPIC_API_KEY is not set"}
    owner = await report_owner(maker)
    if owner is None:
        return {"skipped": "no active admin to own the report"}
    now = datetime.now(UTC).astimezone(IST)
    title = f"{'Daily report' if kind == 'daily' else 'Weekly review'} {now:%d %b %Y}"
    conv, history = await chat.start(
        maker,
        user_id=owner.id,
        conversation_id=None,
        agent="report",
        title=title,
        model=settings.AI_MODEL_MAIN,
    )
    ctx = ToolContext(
        settings=settings,
        maker=maker,
        user_id=owner.id,
        is_admin=True,
        agent="report",
        conversation_id=conv.id,
    )
    res = await chat.turn(
        gateway,
        default_registry(),
        ctx,
        conversation=conv,
        history=history,
        prompt=PROMPTS[kind],
        agent="report",
    )
    return {
        "conversation_id": str(res.conversation_id),
        "cost_usd": str(res.cost_usd),
        "steps": res.steps,
        "stop_reason": res.stop_reason,
    }


__all__ = ["PROMPTS", "report_owner", "run_report"]
