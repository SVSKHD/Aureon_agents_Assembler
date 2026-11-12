"""Monthly and per-agent AI budgets (app/ai/budget.py), without a database."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.ai.budget import (
    BudgetExceeded,
    MemoryUsageStore,
    Settlement,
    Spend,
    month_start,
    next_month_start,
    refusal,
)
from app.core.config import Settings


def test_defaults_match_the_plan(settings: Settings) -> None:
    assert settings.AI_MONTHLY_BUDGET_USD == Decimal("100")
    assert settings.AI_AGENT_BUDGETS_USD == {
        "investments": Decimal("40"),
        "business": Decimal("30"),
        "chat": Decimal("20"),
        "reserve": Decimal("10"),
    }


def test_agent_budgets_validation() -> None:
    s = Settings.model_validate(
        {"DATABASE_URL": "postgresql+asyncpg://x:x@h/d", "SECRET_KEY": "s" * 40,
         "AI_AGENT_BUDGETS_USD": '{"chat": 5}'}
    )  # fmt: skip
    assert s.AI_AGENT_BUDGETS_USD["chat"] == Decimal("5")
    assert s.AI_AGENT_BUDGETS_USD["investments"] == Decimal("40")
    with pytest.raises(ValueError, match="unknown agent"):
        Settings.model_validate(
            {"DATABASE_URL": "postgresql+asyncpg://x:x@h/d", "SECRET_KEY": "s" * 40,
             "AI_AGENT_BUDGETS_USD": '{"trading": 5}'}
        )  # fmt: skip


def test_month_window_is_ist() -> None:
    # 30 Nov 19:00 UTC is already 1 Dec 00:30 IST: a new month.
    now = datetime(2026, 11, 30, 19, 0, tzinfo=UTC)
    assert month_start(now) == datetime(2026, 11, 30, 18, 30, tzinfo=UTC)
    assert next_month_start(now) == datetime(2026, 12, 31, 18, 30, tzinfo=UTC)
    mid = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)
    assert month_start(mid) == datetime(2026, 10, 31, 18, 30, tzinfo=UTC)


def test_refusal_names_the_cap(settings: Settings) -> None:
    d = Decimal
    spend = Spend(d("95"), {"investments": d("39.50"), "chat": d("10")})
    assert refusal(settings, spend, "chat", d("1")) is None
    assert refusal(settings, spend, "investments", d("0.60")) == (
        "investments",
        d("40"),
        d("39.50"),
    )
    assert refusal(settings, spend, "chat", d("5.01"))[0] == "monthly"  # type: ignore[index]
    assert refusal(settings, Spend(d(0), {}), "reserve", d("10.01"))[0] == "reserve"  # type: ignore[index]


async def test_reservations_count_until_settled(settings: Settings) -> None:
    s = settings.model_copy(update={"AI_AGENT_BUDGETS_USD": {"chat": Decimal("1")}})
    store = MemoryUsageStore()
    a = await store.reserve(s, agent="general", budget="chat", model="m",
                            estimate=Decimal("0.60"), conversation_id=None)  # fmt: skip
    with pytest.raises(BudgetExceeded):  # 0.60 reserved + 0.60 > 1.00
        await store.reserve(s, agent="general", budget="chat", model="m",
                            estimate=Decimal("0.60"), conversation_id=None)  # fmt: skip
    await store.settle(a, Settlement("ok", cost_usd=Decimal("0.05")))
    await store.reserve(s, agent="general", budget="chat", model="m",
                        estimate=Decimal("0.60"), conversation_id=None)  # fmt: skip
    assert (await store.spend()).by_budget["chat"] == Decimal("0.65")


async def test_concurrent_reservations_never_overspend(settings: Settings) -> None:
    s = settings.model_copy(update={"AI_AGENT_BUDGETS_USD": {"chat": Decimal("1")}})
    store = MemoryUsageStore()

    async def one() -> bool:
        try:
            await store.reserve(s, agent="general", budget="chat", model="m",
                                estimate=Decimal("0.30"), conversation_id=None)  # fmt: skip
            return True
        except BudgetExceeded:
            return False

    results = await asyncio.gather(*(one() for _ in range(10)))
    assert results.count(True) == 3  # 3 x 0.30 fits under 1.00, a 4th does not
    assert (await store.spend()).total == Decimal("0.90")


def test_message_says_when_it_resets() -> None:
    err = BudgetExceeded("monthly", Decimal("100"), Decimal("100.00"))
    assert "monthly AI budget is used up" in str(err) and "resets" in str(err)
