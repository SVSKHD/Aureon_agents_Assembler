"""Hard limits on a proposed trade, in Python, before an approval row can exist.

Not in the prompt: Claude can be talked into anything, this code cannot. A proposal must
pass every check:

- shape: side BUY/SELL, a positive whole quantity, positive prices; for a BUY the stop is
  below the entry and the target above it, for a SELL the reverse;
- max loss per trade:  quantity x |entry - stop| <= AI_TRADE_MAX_LOSS_INR
- max order size:      quantity x entry          <= AI_TRADE_MAX_ORDER_INR
- max trades per day:  proposals filed today (Asia/Kolkata), not rejected or expired,
                       < AI_TRADE_MAX_PER_DAY

An approved proposal becomes a PAPER order intent only (app/ai/approvals.py), which then
also goes through the existing paper risk check and paper approval. Nothing here or there
can reach a live broker order: LIVE trading is disabled and Dhan is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.core.config import Settings


class RiskLimitBreached(ValueError):
    code = "risk_limit"


@dataclass(frozen=True, slots=True)
class TradeProposal:
    symbol: str
    side: str
    quantity: Decimal
    entry: Decimal
    stop: Decimal
    target: Decimal
    reason: str

    @property
    def risk_inr(self) -> Decimal:
        return self.quantity * abs(self.entry - self.stop)

    @property
    def notional_inr(self) -> Decimal:
        return self.quantity * self.entry

    @property
    def reward_inr(self) -> Decimal:
        return self.quantity * abs(self.target - self.entry)


def _dec(value: object, name: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RiskLimitBreached(f"{name} is not a number") from None
    if not d.is_finite():
        raise RiskLimitBreached(f"{name} is not a number")
    return d


def parse(raw: dict[str, object]) -> TradeProposal:
    side = str(raw.get("side", "")).upper()
    if side not in ("BUY", "SELL"):
        raise RiskLimitBreached("side must be BUY or SELL")
    qty = _dec(raw.get("qty"), "qty")
    if qty <= 0 or qty != qty.to_integral_value():
        raise RiskLimitBreached("qty must be a positive whole number")
    entry, stop, target = (_dec(raw.get(k), k) for k in ("entry", "stop", "target"))
    if min(entry, stop, target) <= 0:
        raise RiskLimitBreached("entry, stop and target must be positive prices")
    if side == "BUY" and not stop < entry < target:
        raise RiskLimitBreached("a BUY needs stop < entry < target")
    if side == "SELL" and not target < entry < stop:
        raise RiskLimitBreached("a SELL needs target < entry < stop")
    symbol = str(raw.get("symbol", "")).strip().upper()
    if not symbol:
        raise RiskLimitBreached("symbol is required")
    reason = str(raw.get("reason", "")).strip()
    return TradeProposal(symbol, side, qty, entry, stop, target, reason)


def check(settings: Settings, p: TradeProposal, proposals_today: int) -> None:
    """RiskLimitBreached with the rule and the numbers when any limit is broken."""
    if p.risk_inr > settings.AI_TRADE_MAX_LOSS_INR:
        raise RiskLimitBreached(
            f"max loss per trade: {p.risk_inr:.2f} INR at the stop is above the "
            f"{settings.AI_TRADE_MAX_LOSS_INR:.2f} INR limit"
        )
    if p.notional_inr > settings.AI_TRADE_MAX_ORDER_INR:
        raise RiskLimitBreached(
            f"max order size: {p.notional_inr:.2f} INR is above the "
            f"{settings.AI_TRADE_MAX_ORDER_INR:.2f} INR limit"
        )
    if proposals_today >= settings.AI_TRADE_MAX_PER_DAY:
        raise RiskLimitBreached(
            f"max trades per day: {proposals_today} already proposed today "
            f"(limit {settings.AI_TRADE_MAX_PER_DAY})"
        )


__all__ = ["RiskLimitBreached", "TradeProposal", "check", "parse"]
