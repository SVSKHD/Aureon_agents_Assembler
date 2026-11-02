"""Backtests on stored candles. Strict no-lookahead; money in Decimal.

Execution model:
- A decision is made on bar i's CLOSE (strategies only see rows 0..i) and filled at bar
  i+1's OPEN. The last bar's decision is never filled.
- BUY when flat opens a long; BUY when short closes the short and opens a long. SELL is the
  mirror image. NO_TRADE holds.
- Fills are adverse by half the spread plus slippage (basis points of price); fees are
  basis points of notional, per side.
- A position still open after the last bar is closed at the last close and flagged.

Prices are the stored Decimal candles; only the strategy's indicator math uses floats.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from app.agents.research.indicators import frame
from app.agents.research.strategies import BUY, SELL, StrategyFn
from app.agents.trading.types import session_for

BPS = Decimal(10_000)
MONEY_Q = Decimal("0.0001")
PRICE_Q = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class Costs:
    spread_bps: Decimal = Decimal(0)
    slippage_bps: Decimal = Decimal(0)
    fee_bps: Decimal = Decimal(0)

    def as_dict(self) -> dict[str, str]:
        return {k: str(getattr(self, k)) for k in ("spread_bps", "slippage_bps", "fee_bps")}

    @property
    def adverse_bps(self) -> Decimal:
        return self.spread_bps / 2 + self.slippage_bps


@dataclass(slots=True)
class Trade:
    side: str
    quantity: Decimal
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime | None = None
    exit_price: Decimal | None = None
    pnl: Decimal | None = None
    fees: Decimal = Decimal(0)
    open_at_end: bool = False

    @property
    def session(self) -> str:
        return session_for(self.entry_time).value


@dataclass(slots=True)
class Result:
    trades: list[Trade] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def _fill(price: Decimal, side: str, costs: Costs) -> Decimal:
    adj = price * costs.adverse_bps / BPS
    return (price + adj if side == BUY else price - adj).quantize(PRICE_Q)


def _fee(price: Decimal, qty: Decimal, costs: Costs) -> Decimal:
    return (price * qty * costs.fee_bps / BPS).quantize(MONEY_Q, ROUND_HALF_EVEN)


def run(
    candles: Sequence[Any],
    strategy: StrategyFn,
    params: dict[str, Any],
    *,
    quantity: Decimal,
    costs: Costs,
) -> Result:
    """candles: ascending rows with ts/open/high/low/close/volume (Decimal)."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if len(candles) < 2:
        return Result(metrics=metrics([], costs))
    decisions = strategy(frame(candles), params).side.to_numpy()
    trades: list[Trade] = []
    pos: Trade | None = None

    def close(at: int, price: Decimal, *, at_end: bool = False) -> None:
        nonlocal pos
        assert pos is not None
        exit_side = SELL if pos.side == BUY else BUY
        px = price if at_end else _fill(price, exit_side, costs)
        sign = 1 if pos.side == BUY else -1
        fee = _fee(px, pos.quantity, costs)
        pos.exit_time = candles[at].ts
        pos.exit_price = px
        pos.fees += fee
        pos.pnl = ((px - pos.entry_price) * pos.quantity * sign - pos.fees).quantize(MONEY_Q)
        pos.open_at_end = at_end
        trades.append(pos)
        pos = None

    for i in range(len(candles) - 1):
        d = decisions[i]
        if d not in (BUY, SELL):
            continue
        nxt = i + 1  # fill on the next bar's open: never the decision bar itself
        if pos is not None and pos.side == d:
            continue
        if pos is not None:
            close(nxt, candles[nxt].open)
        px = _fill(candles[nxt].open, d, costs)
        pos = Trade(
            side=d,
            quantity=quantity,
            entry_time=candles[nxt].ts,
            entry_price=px,
            fees=_fee(px, quantity, costs),
        )
    if pos is not None:
        close(len(candles) - 1, candles[-1].close, at_end=True)
    return Result(trades=trades, metrics=metrics(trades, costs))


def metrics(trades: list[Trade], costs: Costs) -> dict[str, Any]:
    pnls = [t.pnl or Decimal(0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins, Decimal(0))
    gross_loss = -sum(losses, Decimal(0))
    net = sum(pnls, Decimal(0))
    fees = sum((t.fees for t in trades), Decimal(0))
    # Spread + slippage paid (≈ fill notional x adverse bps, per filled leg).
    slip = Decimal(0)
    for t in trades:
        slip += t.entry_price * t.quantity * costs.adverse_bps / BPS
        if t.exit_price is not None and not t.open_at_end:
            slip += t.exit_price * t.quantity * costs.adverse_bps / BPS
    equity, peak, max_dd = Decimal(0), Decimal(0), Decimal(0)
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    n = len(trades)

    def s(d: Decimal) -> str:
        return format(d.quantize(MONEY_Q).normalize(), "f")

    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": s(Decimal(len(wins)) / n) if n else None,
        "net_pnl": s(net),
        "gross_profit": s(gross_profit),
        "gross_loss": s(gross_loss),
        "expectancy": s(net / n) if n else None,
        "profit_factor": s(gross_profit / gross_loss) if gross_loss else None,
        "max_drawdown": s(max_dd),
        "fees": s(fees),
        "spread_slippage_cost": s(slip),
        "open_at_end": sum(1 for t in trades if t.open_at_end),
        "costs": costs.as_dict(),
        "fill": "next bar open",
    }
