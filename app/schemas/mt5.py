"""MT5 push ingest + dashboard schemas (docs/12-mt5-adapter.md §3).

Decimals serialize as JSON strings ("2650.55"), never floats, as everywhere in the trading API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

BotState = Literal["online", "stale", "error"]
MarketSession = Literal["Asia", "London", "NY"]
FailureKind = Literal[
    "mt5_disconnected",
    "stale_ticks",
    "guardian_error",
    "discord_error",
    "exception",
    "stale_heartbeat",
]


class Mt5IngestAccepted(BaseModel):
    accepted: Literal[True] = True
    push_id: uuid.UUID
    events_new: int
    events_duplicate: int


class Mt5IngestDuplicate(BaseModel):
    duplicate: Literal[True] = True
    push_id: uuid.UUID


class Mt5BotStatusOut(BaseModel):
    """Status card: heartbeat = the time of the last accepted push (server clock)."""

    state: BotState
    reasons: list[str]
    last_heartbeat_at: datetime | None
    age_s: int | None
    stale_after_s: float
    push_id: uuid.UUID | None
    seq: int | None
    generated_at: datetime | None
    bot: dict[str, object]
    health: dict[str, object]
    halt: dict[str, object] | None
    open_failures: int
    server_time: datetime


class Mt5CandleOut(BaseModel):
    t: datetime  # UTC open time
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741
    c: Decimal
    v: Decimal | None
    ema: dict[str, Decimal | None]


class Mt5SymbolMarketOut(BaseModel):
    symbol: str
    open: bool | None
    session: MarketSession  # computed from UTC now (Asia 22-07, London 07-13, NY 13-22)
    bot_session: str | None  # what the bot reported, if anything
    tick_age_s: Decimal | None
    last_price: Decimal | None
    last_price_at: datetime | None
    timeframe: str
    ema_periods: list[str]
    candles: list[Mt5CandleOut]


class Mt5MarketOut(BaseModel):
    server_time: datetime
    session: MarketSession
    as_of: datetime | None
    symbols: list[Mt5SymbolMarketOut]


class Mt5GuardianOut(BaseModel):
    """phase: the guardian's word (pre | protect | secured | ride); leg_phase: the A/B leg."""

    phase: str | None
    leg_phase: str | None
    profile: str | None
    secured_pts: Decimal | None
    peak_pts: Decimal | None
    bars: int | None
    frozen: bool | None
    sl: Decimal | None
    secured_level: Decimal | None
    peak: Decimal | None
    direction: str | None
    entry: Decimal | None
    updated_at: str | None


class Mt5PositionOut(BaseModel):
    symbol: str
    ticket: int
    direction: str
    volume: Decimal
    price_open: Decimal
    current: Decimal | None
    sl: Decimal | None
    tp: Decimal | None
    points: Decimal | None
    opened_at: datetime | None  # UTC (converted from the MT5 server clock)
    guardian: Mt5GuardianOut | None


class Mt5PositionsOut(BaseModel):
    as_of: datetime | None
    stale: bool
    items: list[Mt5PositionOut]
    # Guarded tickets the bot reports that are not (or no longer) in its open positions.
    guardian_orphans: dict[str, list[Mt5GuardianOut]]


class Mt5EventOut(BaseModel):
    id: str
    t: datetime
    event: str
    kind: str
    symbol: str | None
    ticket: int | None
    payload: dict[str, object]


class Mt5SignalsOut(BaseModel):
    items: list[Mt5EventOut]
    total: int
    limit: int
    offset: int
    latest: dict[str, dict[str, object]]


class Mt5FailureOut(BaseModel):
    id: int | None  # None for the on-read stale_heartbeat before the watch job stores it
    kind: FailureKind
    symbol: str | None
    reason: str
    first_seen: datetime
    last_seen: datetime
    count: int
    resolved_at: datetime | None
    open: bool


class Mt5FailuresOut(BaseModel):
    open_count: int
    items: list[Mt5FailureOut]


class Mt5StrategyOut(BaseModel):
    as_of: datetime | None
    bot_version: str | None
    mode: str | None
    symbols: list[str]
    exec_tf: str | None
    timeframes: list[str]
    ema: dict[str, object]
    params: dict[str, object]


class Mt5DailyOut(BaseModel):
    date: str
    exits: int
    wins: int
    losses: int
    net_points: Decimal


class Mt5ReportOut(BaseModel):
    days: int
    performance: dict[str, object]
    daily: list[Mt5DailyOut]
    ema921e_trades: list[dict[str, object]]
    backtests: list[dict[str, object]]
