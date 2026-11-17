"""Portfolio signal map arithmetic (docs/37 §6): values, P&L, allocation, the advisory
classes and the rebalance preview. Pure Decimal arithmetic, checked by hand."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.agents.research import portfolio as pf

MAX_W, ADD_MIN = D(20), D(65)


def pos(
    sym: str,
    qty: str,
    avg: str,
    price: str | None,
    state: str | None = None,
    score: str | None = None,
    iid: int | None = None,
) -> pf.Position:
    return pf.Position(sym, D(qty), D(avg), D(price) if price else None, iid, state,
                       D(score) if score else None)  # fmt: skip


def test_values_pnl_and_allocation() -> None:
    rows, totals = pf.value(
        [
            pos("AAA", "10", "100", "120", "BULLISH_TREND", "70"),  # 1000 → 1200
            pos("BBB", "5", "200", "160", "BEARISH_TREND"),  # 1000 → 800
            pos("CCC", "2", "50", None, "MIXED"),  # no price
        ],
        max_weight_pct=D(100),
        add_min_score=ADD_MIN,
    )
    a, b, c = rows
    assert (a.invested, a.current, a.pnl, a.pnl_pct, a.allocation_pct) == (
        D("1000.00"),
        D("1200.00"),
        D("200.00"),
        D("20.00"),
        D("60.00"),
    )
    assert (b.pnl, b.pnl_pct, b.allocation_pct) == (D("-200.00"), D("-20.00"), D("40.00"))
    assert c.current is None and c.allocation_pct is None and "no valid market price" in c.reasons
    assert totals["invested"] == D("2100.00") and totals["current"] == D("2000.00")
    assert totals["unrealized_pnl"] == D("0.00") and totals["unpriced_holdings"] == 1


@pytest.mark.parametrize(
    ("state", "score", "alloc", "want"),
    [
        (None, None, D(5), "MONITOR"),
        ("UNAVAILABLE", None, D(5), "MONITOR"),
        ("BEARISH_CONFIRMED", None, D(5), "REVIEW_RISK"),
        ("BEARISH_TREND", None, D(5), "REVIEW_RISK"),
        ("MIXED", None, D(30), "REVIEW_RISK"),  # overweight and not bullish
        ("BEARISH_PENDING", None, D(5), "MONITOR"),
        ("MIXED", None, D(5), "MONITOR"),
        ("BULLISH_TREND", D(80), D(5), "POTENTIAL_ADD"),
        ("BULLISH_CONFIRMED", D(60), D(5), "HOLD_CANDIDATE"),  # score below the add bar
        ("BULLISH_TREND", D(90), D(30), "HOLD_CANDIDATE"),  # bullish but already overweight
    ],
)
def test_classification_rules(state: str | None, score: D | None, alloc: D, want: str) -> None:
    got, reasons = pf.classify(state, score, alloc, max_weight_pct=MAX_W, add_min_score=ADD_MIN)
    assert got == want and reasons


def flat_fee(side: str, turnover: D) -> D:
    return pf.money(turnover * D("0.001"))  # 0.1 % either way, for checking


def test_rebalance_trims_overweight_and_funds_alternatives() -> None:
    valued, _ = pf.value(
        [
            pos("BIG", "100", "100", "100", "BULLISH_TREND", "70", iid=1),  # 10,000
            pos("WEAK", "10", "100", "100", "BEARISH_CONFIRMED", iid=2),  # 1,000
        ],
        max_weight_pct=MAX_W,
        add_min_score=ADD_MIN,
    )
    cands = [
        pf.Candidate("NEW1", 10, D("250"), D("80"), "BULLISH_CONFIRMED"),
        pf.Candidate("BIG", 1, D("100"), D("90"), "BULLISH_TREND"),  # already held: skipped
    ]
    plan = pf.rebalance(
        valued,
        cands,
        cash=D("1000"),
        max_weight_pct=MAX_W,
        cash_buffer_pct=D(0),
        max_new=3,
        min_trade_value=D(100),
        charges=flat_fee,
    )
    # Portfolio 12,000 (holdings + cash) → a 2,400 cap: BIG (10,000) trims 7,600 = 76 shares.
    sell = next(t for t in plan.trades if t.side == "SELL")
    assert (sell.symbol, sell.quantity, sell.value, sell.charges) == (
        "BIG",
        D(76),
        D("7600.00"),
        D("7.60"),
    )
    # WEAK is flagged for review, never sold for being weak.
    assert [t.symbol for t in plan.trades if t.side == "SELL"] == ["BIG"]
    assert plan.review and plan.review[0].startswith("WEAK:")
    # The only new candidate gets at most the 2,400 cap: 9 shares of 250.
    buy = next(t for t in plan.trades if t.side == "BUY")
    assert (buy.symbol, buy.quantity, buy.value) == ("NEW1", D(9), D("2250.00"))
    assert plan.total_charges == D("7.60") + D("2.25")
    assert plan.cash_after == D("1000") + D("7600.00") - D("2250.00") - plan.total_charges
    assert plan.cash_required == D(0)
    assert plan.allocation_after["BIG"] == pf.pct(D(2400) / D(5650) * 100)
    assert any("no order is placed" in n for n in plan.notes)
    assert any("crossover alone never" in n for n in plan.notes)


def test_rebalance_without_alternatives_or_cash() -> None:
    valued, _ = pf.value([pos("ONE", "1", "100", "100", "BULLISH_TREND", iid=1)],
                         max_weight_pct=D(100), add_min_score=ADD_MIN)  # fmt: skip
    plan = pf.rebalance(valued, [], cash=D(0), max_weight_pct=D(100), cash_buffer_pct=D(5),
                        max_new=3, min_trade_value=D(100), charges=flat_fee)  # fmt: skip
    assert plan.trades == [] and "no alternative passed the filters" in plan.notes
    j = pf.plan_json(plan)
    assert j["cash_after"] == "0.00" and j["allocation_after"] == {"ONE": "100.00"}
