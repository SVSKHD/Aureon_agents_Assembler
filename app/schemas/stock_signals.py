"""Stock Signals API schemas (docs/37). Prices are Decimal (JSON strings), times are UTC."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

NotificationKind = Literal[
    "bullish_cross",
    "bearish_cross",
    "vwap_confirmed",
    "holding_reversal",
    "portfolio_trend_change",
    "replacement_candidate",
]
SignalTimeframe = Literal["5m", "15m", "1h", "1d"]


class StockSignalNotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: str
    event_id: int | None
    instrument_id: int | None
    timeframe: str | None
    title: str
    body: str
    payload: dict[str, Any] = Field(
        description="instrument, signal_id, timeframe, prices, timestamps and deep_link"
    )
    created_at: datetime
    read_at: datetime | None


class StockSignalNotificationsOut(BaseModel):
    items: list[StockSignalNotificationOut]
    unread_count: int
    # Pass as before_id for the next (older) page; null when there is none.
    next_before_id: int | None = None
    # With after_id (the stream's polling fallback): the newest id returned, to pass back.
    last_id: int | None = None


class StockSignalUnreadOut(BaseModel):
    unread_count: int


class StockSignalReadAllIn(BaseModel):
    up_to_id: int | None = Field(default=None, ge=1, description="Only up to this id")


class StockSignalPrefsIn(BaseModel):
    enabled: bool = True
    kinds: list[NotificationKind] = Field(default_factory=list, max_length=6)
    timeframes: list[SignalTimeframe] = Field(default_factory=list, max_length=4)
    all_market: bool = False
    confirmed_only: bool = False


class StockSignalPrefsOut(StockSignalPrefsIn):
    pass


# --- portfolio signal map (docs/37 §6) ---------------------------------------------------------

Classification = Literal["HOLD_CANDIDATE", "MONITOR", "REVIEW_RISK", "POTENTIAL_ADD"]


class PortfolioHoldingOut(BaseModel):
    instrument_id: int | None
    symbol: str
    isin: str | None
    exchange: str | None
    quantity: Decimal
    avg_cost: Decimal
    last_price: Decimal | None
    price_source: Literal["dhan_holdings", "candle_close"] | None
    price_as_of: datetime | None
    invested: Decimal
    current_value: Decimal | None
    unrealized_pnl: Decimal | None
    unrealized_pnl_pct: Decimal | None
    allocation_pct: Decimal | None
    signal_state: str | None
    ema_state: Literal["above", "below", "equal"] | None
    vwap_state: Literal["above", "below", "at"] | None
    trend_score: Decimal | None
    signal_as_of: datetime | None
    classification: Classification
    reasons: list[str]
    deep_link: str


class PortfolioTotalsOut(BaseModel):
    invested: Decimal
    current: Decimal
    unrealized_pnl: Decimal
    unpriced_holdings: Decimal


class SectorExposureOut(BaseModel):
    available: bool
    reason: str | None = None


class PortfolioFreshnessOut(BaseModel):
    holdings_as_of: datetime | None
    cash_as_of: datetime | None
    signals_as_of: datetime | None


class PortfolioMapOut(BaseModel):
    timeframe: str
    accounts: int
    holdings: list[PortfolioHoldingOut]
    totals: PortfolioTotalsOut
    available_cash: Decimal | None
    sector_exposure: SectorExposureOut
    freshness: PortfolioFreshnessOut
    classification_counts: dict[str, int]
    disclaimer: str


class RebalanceIn(BaseModel):
    timeframe: SignalTimeframe = "15m"
    max_weight_pct: Decimal | None = Field(default=None, gt=0, le=100)
    cash_buffer_pct: Decimal | None = Field(default=None, ge=0, le=100)
    max_new: int | None = Field(default=None, ge=0, le=20)
    min_trade_value: Decimal | None = Field(default=None, ge=0)


class RebalanceTradeOut(BaseModel):
    side: Literal["BUY", "SELL"]
    symbol: str
    instrument_id: int | None
    quantity: Decimal
    price: Decimal
    value: Decimal
    charges: Decimal
    reason: str


class RebalancePreviewOut(BaseModel):
    timeframe: str
    trades: list[RebalanceTradeOut]
    cash_before: Decimal
    cash_after: Decimal
    cash_required: Decimal
    cash_known: bool
    total_charges: Decimal
    allocation_before: dict[str, Decimal]
    allocation_after: dict[str, Decimal]
    review: list[str]
    notes: list[str]
    inputs: dict[str, Any]
    charges_model: str
    freshness: PortfolioFreshnessOut
    generated_at: datetime


# --- signals, states and rankings (docs/37 §2-3) ---------------------------------------------

SignalState = Literal[
    "BULLISH_CONFIRMED",
    "BULLISH_PENDING",
    "BEARISH_CONFIRMED",
    "BEARISH_PENDING",
    "BULLISH_TREND",
    "BEARISH_TREND",
    "MIXED",
    "UNAVAILABLE",
]


class StockSignalEventOut(BaseModel):
    id: int
    instrument_id: int
    symbol: str
    name: str | None
    exchange: str
    timeframe: str
    direction: Literal["bullish", "bearish"]
    state: SignalState
    crossed_at: datetime = Field(description="Close time of the candle where EMA 8 crossed EMA 32")
    confirmed_at: datetime | None = Field(
        description="Close time of the first candle that closed beyond the session VWAP"
    )
    ema_fast: Decimal | None
    ema_slow: Decimal | None
    vwap: Decimal | None
    last_price: Decimal | None = Field(description="Close of the cross candle")
    confirm_price: Decimal | None
    confirm_vwap: Decimal | None
    trend_score: Decimal | None = Field(description="0-100 ranking score; not a probability")
    in_holdings: bool
    data_source: str
    data_as_of: datetime | None
    metrics: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class StockSignalStateOut(BaseModel):
    instrument_id: int
    symbol: str
    name: str | None
    exchange: str | None
    segment: str | None
    timeframe: str
    state: SignalState
    last_price: Decimal | None
    change_pct: Decimal | None = Field(description="vs the previous session's last close")
    trend_score: Decimal | None
    rvol: Decimal | None
    rsi: Decimal | None
    adx: Decimal | None
    ema_fast: Decimal | None
    ema_slow: Decimal | None
    vwap: Decimal | None
    vwap_distance_pct: Decimal | None
    ema_spread_pct: Decimal | None
    relative_pct: Decimal | None = Field(description="change_pct minus Nifty 50's")
    missing_features: list[str]
    in_holdings: bool
    event_id: int | None
    candle_close_at: datetime | None
    reason: str | None = None


class StockSignalsPage(BaseModel):
    items: list[StockSignalEventOut]
    total: int
    limit: int
    offset: int


class StockSignalStatesPage(BaseModel):
    items: list[StockSignalStateOut]
    total: int
    limit: int
    offset: int


class StockSignalDetailOut(BaseModel):
    event: StockSignalEventOut
    current: StockSignalStateOut | None


class RankingCoverageOut(BaseModel):
    timeframe: str
    run_id: uuid.UUID | None
    boundary: datetime | None = None
    data_as_of: datetime | None
    universe_size: int
    evaluated: int = 0
    unavailable: int = 0
    illiquid: int = 0
    history_wait_timed_out: bool = False
    finished_at: datetime | None = None


class RankingOut(BaseModel):
    kind: str
    coverage: RankingCoverageOut
    items: list[StockSignalStateOut]
    note: str
