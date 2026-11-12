"""Write requests from Claude (ai.approvals). Claude never executes anything.

- file():   a write tool's Draft becomes a `pending` row (expires after AI_APPROVAL_TTL_HOURS).
            Filing runs under one advisory lock, and a paper trade re-checks the per-day limit
            there, so two proposals at once cannot both slip under it.
- decide(): an admin approves or rejects (the API also requires CSRF and their password).
            On approve the BACKEND runs the action through the executor registered for it,
            and the row ends `executed` or `failed` with the result. Single use: a decided
            row is never decided again (row lock + status check).

Executors:
- paper_trade: a PAPER order intent through the existing paper engine (app/services/paper),
  which runs its own risk check and leaves the intent for the normal paper approval. The
  account must be in PAPER mode; LIVE is unreachable there by construction.
Actions with no executor (systems not connected yet) fail on approval with that reason.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.budget import IST
from app.ai.tools.registry import Draft, ToolContext, ToolRefused
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.ai import Approval
from app.models.trading import BrokerAccount

log = get_logger("ai.approvals")

LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext('ai_approvals'))")


class ApprovalError(Exception):
    code = "approval_error"
    status = 409


class NotFound(ApprovalError):
    code = "not_found"
    status = 404


class NotPending(ApprovalError):
    code = "not_pending"


@dataclass(slots=True)
class ExecDeps:
    settings: Settings
    redis: Redis | None = None
    adapters: Mapping[str, Any] = field(default_factory=dict)


Executor = Callable[[AsyncSession, ExecDeps, Approval, uuid.UUID], Awaitable[dict[str, Any]]]


def _today_start() -> datetime:
    local = datetime.now(UTC).astimezone(IST)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


async def file(ctx: ToolContext, draft: Draft, tool_call_id: int | None) -> Approval:
    async with ctx.maker() as db, db.begin():
        await db.execute(LOCK_SQL)
        if draft.action == "paper_trade":
            n = await db.scalar(
                select(func.count())
                .select_from(Approval)
                .where(
                    Approval.action == "paper_trade",
                    Approval.created_at >= _today_start(),
                    Approval.status.not_in(("rejected", "expired")),
                )
            )
            if int(n or 0) >= ctx.settings.AI_TRADE_MAX_PER_DAY:
                raise ToolRefused(
                    f"rejected by the risk limits: max trades per day "
                    f"({ctx.settings.AI_TRADE_MAX_PER_DAY}) reached"
                )
        row = Approval(
            conversation_id=ctx.conversation_id,
            tool_call_id=tool_call_id,
            requested_by=ctx.user_id,
            agent=ctx.agent,
            action=draft.action,
            payload=draft.payload,
            summary=draft.summary,
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(hours=ctx.settings.AI_APPROVAL_TTL_HOURS),
        )
        db.add(row)
        await db.flush()
        log.info("ai_approval_filed", id=str(row.id), action=draft.action, agent=ctx.agent)
        return row


async def expire(db: AsyncSession) -> int:
    res = await db.execute(
        update(Approval)
        .where(Approval.status == "pending", Approval.expires_at < func.now())
        .values(status="expired")
    )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


async def paper_trade(
    db: AsyncSession, deps: ExecDeps, row: Approval, admin_id: uuid.UUID
) -> dict[str, Any]:
    from app.services import paper

    p = row.payload
    q = select(BrokerAccount).where(
        BrokerAccount.broker == "dhan",
        BrokerAccount.mode == "PAPER",
        BrokerAccount.is_active.is_(True),
    )
    if row.requested_by is not None:
        q = q.where(BrokerAccount.owner_user_id == row.requested_by)
    account = await db.scalar(q.order_by(BrokerAccount.created_at).limit(1))
    if account is None:
        raise ApprovalError("no active Dhan account in PAPER mode to place the paper trade in")
    req = paper.IntentRequest(
        account_id=account.id,
        instrument_id=int(p["instrument_id"]),
        side=str(p["side"]),
        order_type="LIMIT",
        quantity=Decimal(str(p["qty"])),
        limit_price=Decimal(str(p["entry"])),
        product=None,
        signal_id=None,
        correlation_id=f"ai-approval-{row.id}",
    )
    try:
        intent, _ = await paper.create_intent(
            db, deps.redis, dict(deps.adapters), deps.settings, account, req, admin_id
        )
    except paper.IntentRefused as exc:
        raise ApprovalError(f"paper engine refused it: {exc}") from None
    return {
        "intent_id": str(intent.id),
        "intent_status": intent.status,
        "account_id": str(account.id),
        "note": "a PAPER intent; it still goes through the paper approval",
    }


EXECUTORS: dict[str, Executor] = {"paper_trade": paper_trade}


async def decide(
    maker: async_sessionmaker[AsyncSession],
    deps: ExecDeps,
    approval_id: uuid.UUID,
    admin_id: uuid.UUID,
    action: str,
) -> Approval:
    """Approve (and execute) or reject one pending request."""
    async with maker() as db, db.begin():
        await expire(db)
        row = await db.scalar(select(Approval).where(Approval.id == approval_id).with_for_update())
        if row is None:
            raise NotFound("approval not found")
        if row.status != "pending":
            raise NotPending(f"this request is already {row.status}")
        row.decided_by, row.decided_at = admin_id, datetime.now(UTC)
        if action == "reject":
            row.status = "rejected"
            return row
        row.status = "approved"
        executor = EXECUTORS.get(row.action)
        if executor is None:
            row.status, row.error = "failed", f"{row.action} has no executor (not connected)"
            return row
        try:
            async with db.begin_nested():  # a failed action rolls back alone
                row.result = await executor(db, deps, row, admin_id)
            row.status = "executed"
        except ApprovalError as exc:
            row.status, row.error = "failed", str(exc)
        except Exception as exc:
            log.error("ai_approval_failed", id=str(row.id), error=type(exc).__name__)
            row.status, row.error = "failed", f"execution failed ({type(exc).__name__})"
        log.info("ai_approval_decided", id=str(row.id), status=row.status, action=row.action)
        return row


__all__ = [
    "EXECUTORS",
    "ApprovalError",
    "ExecDeps",
    "NotFound",
    "NotPending",
    "decide",
    "expire",
    "file",
]
