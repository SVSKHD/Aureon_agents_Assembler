"""Paper-trading accounting: fills, charges and positions. Pure functions, Decimal only.

Units
- **Dhan:** `quantity` is units (shares, or contracts x lot size), so it must be a whole
  multiple of the instrument's `lot_size` (1 for cash equity, e.g. 75 for a NIFTY option).
  Price x quantity is the notional in INR.
- **MT5:** `quantity` is lots, a multiple of the volume step (stored as the instrument's
  `lot_size`, e.g. 0.01). Notional and P&L use the symbol's `contract_size`
  (e.g. 100 oz per XAUUSD lot): P&L = Δprice x lots x contract_size, in the profit currency.

Positions use the average-cost method. Selling more than the long position flips it short
(and vice versa); the closed part realizes P&L at the average cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

BPS = Decimal(10_000)
PCT = Decimal(100)
MONEY = Decimal("0.01")
PRICE = Decimal("0.00000001")


class AccountingError(ValueError):
    """The order cannot be accounted for safely (unknown segment, missing spec, ...)."""


def money(d: Decimal) -> Decimal:
    return d.quantize(MONEY, ROUND_HALF_UP)


# --- instruments ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Spec:
    """What the accounting needs to know about an instrument."""

    broker: str  # dhan | mt5
    kind: str  # equity | futures | options | cfd
    lot_step: Decimal  # quantity must be a multiple of this
    multiplier: Decimal  # notional per unit of quantity per unit of price
    currency: str
    tick_size: Decimal
    digits: int | None = None  # MT5 price digits, for pip value


def dhan_kind(segment: str | None, instrument_type: str) -> str:
    t = (instrument_type or "").upper()
    seg = (segment or "").upper()
    if seg.endswith("_EQ") and t in ("EQUITY", "ES", "EQ", ""):
        return "equity"
    if seg.endswith("_FNO") and t.startswith("FUT"):
        return "futures"
    if seg.endswith("_FNO") and t.startswith("OPT"):
        return "options"
    raise AccountingError(f"paper trading does not model {seg or '?'} {t or '?'} instruments")


def check_quantity(spec: Spec, quantity: Decimal) -> None:
    if quantity <= 0:
        raise AccountingError("quantity must be positive")
    if spec.lot_step <= 0 or (quantity % spec.lot_step) != 0:
        unit = "lots" if spec.broker == "mt5" else "units"
        step = format(spec.lot_step.normalize(), "f")
        raise AccountingError(f"quantity must be a multiple of {step} {unit}")


def pip_value(spec: Spec, lots: Decimal = Decimal(1)) -> Decimal | None:
    """MT5: value of one pip for `lots`, in the profit currency. A pip is 10 points on
    3/5-digit quotes and 1 point otherwise."""
    if spec.broker != "mt5" or spec.digits is None:
        return None
    point = Decimal(1).scaleb(-spec.digits)
    pip = point * 10 if spec.digits in (3, 5) else point
    return pip * spec.multiplier * lots


# --- fill price -------------------------------------------------------------------------------


def fill_price(
    reference: Decimal, side: str, spread_bps: Decimal, slippage_bps: Decimal, tick: Decimal
) -> Decimal:
    """Adverse fill: half the spread plus slippage away from the reference price, rounded
    to the tick against the trader (BUY up, SELL down)."""
    adverse = reference * (spread_bps / 2 + slippage_bps) / BPS
    raw = reference + adverse if side == "BUY" else reference - adverse
    if tick > 0:  # against the trader: BUY rounds up to the tick, SELL down
        steps = (raw / tick).to_integral_value(ROUND_CEILING if side == "BUY" else ROUND_FLOOR)
        raw = steps * tick
    if raw <= 0:
        raise AccountingError("computed fill price is not positive")
    return raw.quantize(PRICE)


# --- charges -------------------------------------------------------------------------------


# Approximate Indian charges (rates in % of turnover unless noted). Configurable through
# PAPER_DHAN_CHARGES; check them against Dhan's and the exchanges' current schedules.
DEFAULT_DHAN_CHARGES: dict[str, Any] = {
    "brokerage_flat": "20",  # INR per executed order ...
    "brokerage_pct": "0.03",  # ... or this % of turnover, whichever is lower
    "brokerage_delivery_free": True,  # equity delivery (CNC)
    "stt_pct": {
        "equity_delivery": {"BUY": "0.1", "SELL": "0.1"},
        "equity_intraday": {"BUY": "0", "SELL": "0.025"},
        "futures": {"BUY": "0", "SELL": "0.02"},
        "options": {"BUY": "0", "SELL": "0.1"},  # on premium
    },
    "exchange_pct": {"equity": "0.00297", "futures": "0.00173", "options": "0.03503"},
    "sebi_pct": "0.0001",  # INR 10 per crore
    "stamp_pct": {  # buyer only
        "equity_delivery": "0.015",
        "equity_intraday": "0.003",
        "futures": "0.002",
        "options": "0.003",
    },
    "gst_pct": "18",  # on brokerage + exchange + SEBI
}


@dataclass(frozen=True, slots=True)
class Charges:
    brokerage: Decimal = Decimal(0)
    stt: Decimal = Decimal(0)
    exchange: Decimal = Decimal(0)
    sebi: Decimal = Decimal(0)
    stamp: Decimal = Decimal(0)
    gst: Decimal = Decimal(0)
    commission: Decimal = Decimal(0)

    @property
    def total(self) -> Decimal:
        return money(
            self.brokerage
            + self.stt
            + self.exchange
            + self.sebi
            + self.stamp
            + self.gst
            + self.commission
        )

    def as_dict(self) -> dict[str, str]:
        d = {k: str(money(getattr(self, k))) for k in (
            "brokerage", "stt", "exchange", "sebi", "stamp", "gst", "commission"
        )}  # fmt: skip
        d["total"] = str(self.total)
        return d


def _d(v: Any) -> Decimal:
    return Decimal(str(v))


def dhan_charges(
    kind: str, product: str | None, side: str, turnover: Decimal, schedule: dict[str, Any]
) -> Charges:
    """Charges for one executed order. `turnover` = price x quantity (premium for options)."""
    delivery = kind == "equity" and (product or "").upper() in ("CNC", "DELIVERY")
    bucket = ("equity_delivery" if delivery else "equity_intraday") if kind == "equity" else kind
    pct = Decimal("0.01")
    if delivery and schedule.get("brokerage_delivery_free", True):
        brokerage = Decimal(0)
    else:
        brokerage = min(
            _d(schedule["brokerage_flat"]), turnover * _d(schedule["brokerage_pct"]) * pct
        )
    stt = turnover * _d(schedule["stt_pct"][bucket][side]) * pct
    exchange = turnover * _d(schedule["exchange_pct"][kind]) * pct
    sebi = turnover * _d(schedule["sebi_pct"]) * pct
    stamp = turnover * _d(schedule["stamp_pct"][bucket]) * pct if side == "BUY" else Decimal(0)
    gst = (brokerage + exchange + sebi) * _d(schedule["gst_pct"]) * pct
    return Charges(
        brokerage=money(brokerage),
        stt=money(stt),
        exchange=money(exchange),
        sebi=money(sebi),
        stamp=money(stamp),
        gst=money(gst),
    )


def mt5_charges(lots: Decimal, commission_per_lot: Decimal) -> Charges:
    """MT5: the spread is already in the fill price; commission is per lot per side."""
    return Charges(commission=money(lots * commission_per_lot))


# --- positions -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fill:
    instrument_id: int
    side: str
    quantity: Decimal
    price: Decimal
    fees: Decimal
    multiplier: Decimal
    at: datetime | None = None
    ref: str | None = None


@dataclass(slots=True)
class PositionState:
    instrument_id: int
    multiplier: Decimal
    quantity: Decimal = Decimal(0)  # signed
    avg_price: Decimal = Decimal(0)
    realized: Decimal = Decimal(0)  # before fees
    fees: Decimal = Decimal(0)

    def unrealized(self, last: Decimal | None) -> Decimal | None:
        if last is None or self.quantity == 0:
            return None if last is None else Decimal(0)
        return money((last - self.avg_price) * self.quantity * self.multiplier)

    def notional(self, last: Decimal | None) -> Decimal:
        price = last if last is not None else self.avg_price
        return abs(self.quantity) * price * self.multiplier


def apply_fill(pos: PositionState, f: Fill) -> Decimal:
    """Update `pos` with one fill; returns the P&L realized by it (before fees)."""
    signed = f.quantity if f.side == "BUY" else -f.quantity
    realized = Decimal(0)
    q = pos.quantity
    if q == 0 or (q > 0) == (signed > 0):  # open or add
        total = q + signed
        pos.avg_price = ((abs(q) * pos.avg_price + abs(signed) * f.price) / abs(total)).quantize(
            PRICE
        )
        pos.quantity = total
    else:  # reduce, close or flip
        closing = min(abs(q), abs(signed))
        direction = Decimal(1) if q > 0 else Decimal(-1)
        realized = money((f.price - pos.avg_price) * closing * direction * pos.multiplier)
        remaining = q + signed
        if remaining == 0:
            pos.avg_price = Decimal(0)
        elif (remaining > 0) != (q > 0):  # flipped: the rest opens at the fill price
            pos.avg_price = f.price
        pos.quantity = remaining
    pos.realized = money(pos.realized + realized)
    pos.fees = money(pos.fees + f.fees)
    return realized


@dataclass(slots=True)
class Book:
    positions: dict[int, PositionState] = field(default_factory=dict)

    def apply(self, f: Fill) -> Decimal:
        pos = self.positions.setdefault(
            f.instrument_id, PositionState(f.instrument_id, f.multiplier)
        )
        return apply_fill(pos, f)

    @property
    def realized(self) -> Decimal:
        return money(sum((p.realized for p in self.positions.values()), Decimal(0)))

    @property
    def fees(self) -> Decimal:
        return money(sum((p.fees for p in self.positions.values()), Decimal(0)))


def replay(fills: list[Fill]) -> Book:
    """Positions from scratch, in fill order: the ledger the reconciliation compares with."""
    book = Book()
    for f in fills:
        book.apply(f)
    return book
