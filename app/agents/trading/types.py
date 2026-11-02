"""Broker-agnostic trading types. Every price, quantity and money value is a Decimal; a
float raises TypeError at construction so precision bugs fail loudly, not silently."""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Broker(StrEnum):
    DHAN = "dhan"
    MT5 = "mt5"


class ExecutionMode(StrEnum):
    OBSERVE = "OBSERVE"  # read-only: data, signals, analysis; no order intents executed
    PAPER = "PAPER"  # simulated fills (Phase F)
    LIVE = "LIVE"  # real broker orders (Phase G, behind a feature flag)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SignalSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderStatus(StrEnum):
    """Normalized broker order status (the broker's raw status is kept alongside)."""

    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class Timeframe(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


class Session(StrEnum):
    """Same values as the existing Spasta trades table (Asia / London / NY)."""

    ASIA = "Asia"
    LONDON = "London"
    NY = "NY"


# Non-overlapping UTC partition of the day, so every timestamp gets exactly one tag.
# London 07:00-13:00, NY 13:00-22:00, Asia 22:00-07:00 (wraps midnight).
SESSION_BOUNDS_UTC: tuple[tuple[Session, time, time], ...] = (
    (Session.LONDON, time(7, 0), time(13, 0)),
    (Session.NY, time(13, 0), time(22, 0)),
)


def session_for(ts: datetime) -> Session:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (UTC)")
    t = ts.astimezone(UTC).time()
    for session, start, end in SESSION_BOUNDS_UTC:
        if start <= t < end:
            return session
    return Session.ASIA


def _check_decimals(obj: Any) -> None:
    """Reject floats in any Decimal-typed field; require tz-aware datetimes."""
    for f in fields(obj):
        v = getattr(obj, f.name)
        if isinstance(v, float):
            raise TypeError(f"{type(obj).__name__}.{f.name} must be Decimal, not float")
        if isinstance(v, datetime) and v.tzinfo is None:
            raise ValueError(f"{type(obj).__name__}.{f.name} must be timezone-aware")


class _Checked:
    def __post_init__(self) -> None:
        _check_decimals(self)


@dataclass(frozen=True, slots=True)
class AdapterHealth(_Checked):
    broker: str
    ok: bool
    checked_at: datetime
    detail: str | None = None
    # e.g. Dhan access-token expiry; None when the broker has no such concept.
    credentials_expire_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AccountInfo(_Checked):
    broker: str
    account_id: str
    label: str | None = None
    currency: str | None = None


@dataclass(frozen=True, slots=True)
class Funds(_Checked):
    account_id: str
    currency: str
    available: Decimal
    used_margin: Decimal
    equity: Decimal | None
    as_of: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Position(_Checked):
    account_id: str
    symbol: str
    broker_instrument_id: str | None
    quantity: Decimal  # signed: >0 long, <0 short
    avg_price: Decimal
    currency: str
    last_price: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None
    product: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BrokerOrder(_Checked):
    account_id: str
    order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    filled_quantity: Decimal
    status: OrderStatus
    raw_status: str
    placed_at: datetime
    price: Decimal | None = None
    avg_fill_price: Decimal | None = None
    updated_at: datetime | None = None
    correlation_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InstrumentInfo(_Checked):
    broker: str
    broker_instrument_id: str
    symbol: str
    instrument_type: str
    currency: str
    lot_size: Decimal
    tick_size: Decimal
    exchange: str | None = None
    segment: str | None = None
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: str | None = None
    underlying: str | None = None


@dataclass(frozen=True, slots=True)
class Quote(_Checked):
    symbol: str
    ts: datetime
    last: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Candle(_Checked):
    ts: datetime  # candle open time, UTC
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    oi: Decimal | None = None

    @property
    def session(self) -> Session:
        return session_for(self.ts)


@dataclass(frozen=True, slots=True)
class Tick(_Checked):
    symbol: str
    ts: datetime
    price: Decimal
    quantity: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)
