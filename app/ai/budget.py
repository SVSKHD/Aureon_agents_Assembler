"""Monthly AI budgets (docs/26-ai-providers.md).

Two caps apply to every Claude call, month to date on the Asia/Kolkata calendar:
AI_MONTHLY_BUDGET_USD for everything, and AI_AGENT_BUDGETS_USD for the budget the call is
charged to (investments, business, chat, reserve). Before a call the gateway RESERVES the
call's worst-case cost (whole prompt at the cache-write rate + every output token); the call
is refused when the reservation would cross either cap. After the call the row is settled to
the real cost. Reservations are taken under one Postgres advisory lock, so two calls at once
can never both slip under a cap. A reservation a crash never settled keeps counting its
worst case: the error is always on the safe side.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.models.ai import Usage

IST = ZoneInfo("Asia/Kolkata")
LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext('ai_budget'))")
ZERO = Decimal(0)


def month_start(now: datetime | None = None) -> datetime:
    """The start of the current Asia/Kolkata calendar month, in UTC."""
    local = (now or datetime.now(UTC)).astimezone(IST)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def next_month_start(now: datetime | None = None) -> datetime:
    start = month_start(now).astimezone(IST)
    return (start + timedelta(days=32)).replace(day=1).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Spend:
    total: Decimal
    by_budget: Mapping[str, Decimal]


def refusal(
    settings: Settings, spend: Spend, budget: str, estimate: Decimal
) -> tuple[str, Decimal, Decimal] | None:
    """(which cap, its limit, already spent) when `estimate` more would cross a cap; else None."""
    total_cap = settings.AI_MONTHLY_BUDGET_USD
    if spend.total + estimate > total_cap:
        return "monthly", total_cap, spend.total
    agent_cap = settings.AI_AGENT_BUDGETS_USD.get(budget, ZERO)
    used = spend.by_budget.get(budget, ZERO)
    if used + estimate > agent_cap:
        return budget, agent_cap, used
    return None


class BudgetExceeded(Exception):
    code = "budget_exceeded"

    def __init__(self, cap: str, limit: Decimal, spent: Decimal) -> None:
        name = "the monthly AI budget" if cap == "monthly" else f"the {cap} AI budget"
        super().__init__(
            f"{name} is used up for this month: ${spent:.2f} of ${limit:.2f} spent "
            f"(resets {next_month_start():%d %b %Y} IST)"
        )
        self.cap, self.limit, self.spent = cap, limit, spent


@dataclass(slots=True)
class Settlement:
    outcome: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Decimal = ZERO
    latency_ms: int = 0
    request_id: str | None = None
    stop_reason: str | None = None
    error: str | None = None


class UsageStore(Protocol):
    async def spend(self) -> Spend: ...

    async def reserve(
        self,
        settings: Settings,
        *,
        agent: str,
        budget: str,
        model: str,
        estimate: Decimal,
        conversation_id: object | None,
    ) -> int: ...

    async def settle(self, reservation: int, s: Settlement) -> None: ...


class PgUsageStore:
    """ai.usage in Postgres: the real store."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self.maker = maker

    @staticmethod
    async def _spend(db: AsyncSession) -> Spend:
        rows = (
            await db.execute(
                select(Usage.budget, func.coalesce(func.sum(Usage.cost_usd), 0))
                .where(Usage.at >= month_start())
                .group_by(Usage.budget)
            )
        ).all()
        by = {b: Decimal(v) for b, v in rows}
        return Spend(sum(by.values(), ZERO), by)

    async def spend(self) -> Spend:
        async with self.maker() as db:
            return await self._spend(db)

    async def reserve(
        self,
        settings: Settings,
        *,
        agent: str,
        budget: str,
        model: str,
        estimate: Decimal,
        conversation_id: object | None,
    ) -> int:
        async with self.maker() as db, db.begin():
            await db.execute(LOCK_SQL)
            why = refusal(settings, await self._spend(db), budget, estimate)
            if why is not None:
                raise BudgetExceeded(*why)
            row = Usage(
                agent=agent,
                budget=budget,
                model=model,
                conversation_id=conversation_id,
                outcome="reserved",
                cost_usd=estimate,
            )
            db.add(row)
            await db.flush()
            return int(row.id)

    async def settle(self, reservation: int, s: Settlement) -> None:
        async with self.maker() as db, db.begin():
            await db.execute(
                update(Usage)
                .where(Usage.id == reservation)
                .values(
                    outcome=s.outcome,
                    input_tokens=s.input_tokens,
                    output_tokens=s.output_tokens,
                    cache_read_tokens=s.cache_read_tokens,
                    cache_write_tokens=s.cache_write_tokens,
                    cost_usd=s.cost_usd,
                    latency_ms=s.latency_ms,
                    request_id=s.request_id,
                    stop_reason=s.stop_reason,
                    error=s.error,
                )
            )


@dataclass
class MemoryUsageStore:
    """The same rules in memory (unit tests, and processes without a database)."""

    rows: dict[int, tuple[str, Decimal, Settlement | None]] = field(default_factory=dict)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def spend(self) -> Spend:
        by: dict[str, Decimal] = {}
        for budget, reserved, settled in self.rows.values():
            by[budget] = by.get(budget, ZERO) + (settled.cost_usd if settled else reserved)
        return Spend(sum(by.values(), ZERO), by)

    async def reserve(
        self,
        settings: Settings,
        *,
        agent: str,
        budget: str,
        model: str,
        estimate: Decimal,
        conversation_id: object | None,
    ) -> int:
        async with self._lock:
            why = refusal(settings, await self.spend(), budget, estimate)
            if why is not None:
                raise BudgetExceeded(*why)
            rid = next(self._ids)
            self.rows[rid] = (budget, estimate, None)
            return rid

    async def settle(self, reservation: int, s: Settlement) -> None:
        budget, reserved, _ = self.rows[reservation]
        self.rows[reservation] = (budget, reserved, s)


__all__ = [
    "BudgetExceeded",
    "MemoryUsageStore",
    "PgUsageStore",
    "Settlement",
    "Spend",
    "UsageStore",
    "month_start",
    "next_month_start",
    "refusal",
]
