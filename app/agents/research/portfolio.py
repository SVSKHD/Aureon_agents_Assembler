"""Portfolio signal map and rebalance preview (docs/37 §6): deterministic arithmetic only.

Pure: holdings, prices and signal states in, numbers out. No database, no broker, no model.
Claude may explain these results; it never computes or overrides them. Nothing here is an
order or an instruction: the classifications are advisory labels, and a bullish crossover on
its own is never a reason to replace a holding.

Classification of a holding (first matching rule):

    no usable signal (missing / UNAVAILABLE)               MONITOR
    bearish (BEARISH_CONFIRMED or BEARISH_TREND)           REVIEW_RISK
    overweight (allocation > max weight), not bullish      REVIEW_RISK
    BEARISH_PENDING or MIXED                               MONITOR
    bullish, score >= add_min_score, below max weight      POTENTIAL_ADD
    bullish otherwise                                      HOLD_CANDIDATE
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

ZERO = Decimal(0)
HUNDRED = Decimal(100)
BULLISH = {"BULLISH_CONFIRMED", "BULLISH_PENDING", "BULLISH_TREND"}
BEARISH_STRONG = {"BEARISH_CONFIRMED", "BEARISH_TREND"}
NEUTRAL = {"BEARISH_PENDING", "MIXED"}
CLASSES = ("HOLD_CANDIDATE", "MONITOR", "REVIEW_RISK", "POTENTIAL_ADD")


def money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"))


def pct(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"))


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    quantity: Decimal
    avg_cost: Decimal
    price: Decimal | None  # the latest valid market price, or None
    instrument_id: int | None = None
    state: str | None = None  # stock signal state on the map's timeframe
    score: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Valued:
    position: Position
    invested: Decimal
    current: Decimal | None
    pnl: Decimal | None
    pnl_pct: Decimal | None
    allocation_pct: Decimal | None
    classification: str
    reasons: tuple[str, ...]


def classify(
    state: str | None,
    score: Decimal | None,
    allocation_pct: Decimal | None,
    *,
    max_weight_pct: Decimal,
    add_min_score: Decimal,
) -> tuple[str, tuple[str, ...]]:
    over = allocation_pct is not None and allocation_pct > max_weight_pct
    if state is None or state == "UNAVAILABLE":
        return "MONITOR", ("no current signal (missing or stale candles)",)
    if state in BEARISH_STRONG:
        why = ["bearish EMA/VWAP state " + state]
        if over:
            why.append(f"overweight: {allocation_pct}% > {max_weight_pct}%")
        return "REVIEW_RISK", tuple(why)
    if over and state not in BULLISH:
        return "REVIEW_RISK", (f"overweight: {allocation_pct}% > {max_weight_pct}%", state)
    if state in NEUTRAL:
        return "MONITOR", (f"signal state {state}",)
    # Bullish.
    if over:
        return "HOLD_CANDIDATE", (f"bullish ({state}) but already {allocation_pct}% of value",)
    if score is not None and score >= add_min_score:
        return "POTENTIAL_ADD", (f"bullish ({state}), trend score {score} >= {add_min_score}",)
    return "HOLD_CANDIDATE", (f"bullish ({state})",)


def value(
    positions: Sequence[Position], *, max_weight_pct: Decimal, add_min_score: Decimal
) -> tuple[list[Valued], dict[str, Decimal]]:
    """Per-holding value, P&L, allocation and class, plus portfolio totals. A holding with no
    valid price has no current value and is left out of the allocation base (and says so)."""
    invested_total = ZERO
    current_total = ZERO
    priced = [p for p in positions if p.price is not None and p.price > 0]
    for p in positions:
        invested_total += p.quantity * p.avg_cost
    for p in priced:
        current_total += p.quantity * p.price  # type: ignore[operator]
    out = []
    for p in positions:
        invested = money(p.quantity * p.avg_cost)
        if p.price is None or p.price <= 0:
            cls, why = classify(
                p.state, p.score, None, max_weight_pct=max_weight_pct, add_min_score=add_min_score
            )
            out.append(
                Valued(p, invested, None, None, None, None, cls, ("no valid market price", *why))
            )
            continue
        current = money(p.quantity * p.price)
        pnl = money(current - invested)
        pnl_pct = pct(pnl / invested * HUNDRED) if invested else None
        alloc = pct(current / current_total * HUNDRED) if current_total else None
        cls, why = classify(
            p.state, p.score, alloc, max_weight_pct=max_weight_pct, add_min_score=add_min_score
        )
        out.append(Valued(p, invested, current, pnl, pnl_pct, alloc, cls, why))
    pnl_total = money(current_total - sum((v.invested for v in out if v.current is not None), ZERO))
    totals = {
        "invested": money(invested_total),
        "current": money(current_total),
        "unrealized_pnl": pnl_total,
        "unpriced_holdings": Decimal(len(positions) - len(priced)),
    }
    return out, totals


# --- rebalance preview ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    symbol: str
    instrument_id: int
    price: Decimal
    score: Decimal
    state: str


@dataclass
class Trade:
    side: str  # "SELL" (trim) or "BUY" (add)
    symbol: str
    instrument_id: int | None
    quantity: Decimal
    price: Decimal
    value: Decimal
    charges: Decimal
    reason: str


@dataclass
class Plan:
    trades: list[Trade] = field(default_factory=list)
    cash_before: Decimal = ZERO
    cash_after: Decimal = ZERO
    cash_required: Decimal = ZERO  # buys + charges not covered by sales and usable cash
    total_charges: Decimal = ZERO
    allocation_before: dict[str, Decimal] = field(default_factory=dict)
    allocation_after: dict[str, Decimal] = field(default_factory=dict)
    review: list[str] = field(default_factory=list)  # weak holdings, flagged, not traded
    notes: list[str] = field(default_factory=list)


ChargeFn = Callable[[str, Decimal], Decimal]  # (side, turnover) -> total charges


def _whole(x: Decimal) -> Decimal:
    return x.to_integral_value(rounding=ROUND_DOWN)


def rebalance(
    valued: Sequence[Valued],
    candidates: Sequence[Candidate],
    *,
    cash: Decimal,
    max_weight_pct: Decimal,
    cash_buffer_pct: Decimal,
    max_new: int,
    min_trade_value: Decimal,
    charges: ChargeFn,
) -> Plan:
    """A preview, never an order: trim overweight holdings to `max_weight_pct`, then spread
    what that frees (plus cash above the buffer) over up to `max_new` candidates, in whole
    shares, each capped at `max_weight_pct` of the projected portfolio. Weak holdings are
    listed for review, not sold: a weak trend alone does not decide a sale here."""
    plan = Plan(cash_before=money(cash))
    priced = [v for v in valued if v.current is not None and v.position.price]
    total = sum((v.current for v in priced if v.current is not None), ZERO)
    portfolio = total + cash
    plan.allocation_before = {
        v.position.symbol: pct(v.current / total * HUNDRED)  # type: ignore[operator]
        for v in priced
        if total
    }
    holding_value = {v.position.symbol: v.current or ZERO for v in priced}
    freed = ZERO
    for v in priced:
        p = v.position
        if v.classification == "REVIEW_RISK":
            plan.review.append(f"{p.symbol}: " + "; ".join(v.reasons))
        cap = portfolio * max_weight_pct / HUNDRED
        excess = (v.current or ZERO) - cap
        if excess <= min_trade_value or not p.price:
            continue
        qty = min(_whole(excess / p.price), p.quantity)
        if qty <= 0:
            continue
        val = money(qty * p.price)
        fee = charges("SELL", val)
        plan.trades.append(
            Trade(
                "SELL",
                p.symbol,
                p.instrument_id,
                qty,
                p.price,
                val,
                fee,
                f"trim to {max_weight_pct}% of the portfolio",
            )
        )
        holding_value[p.symbol] -= val
        freed += val - fee
    usable = cash - portfolio * cash_buffer_pct / HUNDRED
    budget = freed + max(usable, ZERO)
    held = {v.position.instrument_id for v in valued}
    picks = [c for c in candidates if c.instrument_id not in held][:max_new]
    if picks and budget > min_trade_value:
        each = budget / len(picks)
        for c in picks:
            cap = portfolio * max_weight_pct / HUNDRED
            spend = min(each, cap)
            qty = _whole(spend / c.price)
            if qty <= 0:
                plan.notes.append(f"{c.symbol}: one share ({c.price}) exceeds its share of cash")
                continue
            val = money(qty * c.price)
            fee = charges("BUY", val)
            if val < min_trade_value:
                continue
            plan.trades.append(
                Trade(
                    "BUY",
                    c.symbol,
                    c.instrument_id,
                    qty,
                    c.price,
                    val,
                    fee,
                    f"alternative: {c.state}, trend score {c.score}",
                )
            )
            holding_value[c.symbol] = holding_value.get(c.symbol, ZERO) + val
    elif not picks:
        plan.notes.append("no alternative passed the filters")

    sells = sum((t.value for t in plan.trades if t.side == "SELL"), ZERO)
    buys = sum((t.value for t in plan.trades if t.side == "BUY"), ZERO)
    plan.total_charges = money(sum((t.charges for t in plan.trades), ZERO))
    plan.cash_after = money(cash + sells - buys - plan.total_charges)
    plan.cash_required = money(max(ZERO, -plan.cash_after))
    after_total = sum(holding_value.values(), ZERO)
    plan.allocation_after = {
        s: pct(v / after_total * HUNDRED) for s, v in holding_value.items() if after_total and v
    }
    plan.notes.append(
        "Preview only: no order is placed. Classifications are advisory; a bullish crossover "
        "alone never justifies replacing a holding."
    )
    return plan


def plan_json(plan: Plan) -> dict[str, Any]:
    def s(d: Decimal) -> str:
        return format(d, "f")

    return {
        "trades": [
            {
                "side": t.side,
                "symbol": t.symbol,
                "instrument_id": t.instrument_id,
                "quantity": s(t.quantity),
                "price": s(t.price),
                "value": s(t.value),
                "charges": s(t.charges),
                "reason": t.reason,
            }
            for t in plan.trades
        ],
        "cash_before": s(plan.cash_before),
        "cash_after": s(plan.cash_after),
        "cash_required": s(plan.cash_required),
        "total_charges": s(plan.total_charges),
        "allocation_before": {k: s(v) for k, v in plan.allocation_before.items()},
        "allocation_after": {k: s(v) for k, v in plan.allocation_after.items()},
        "review": plan.review,
        "notes": plan.notes,
    }
