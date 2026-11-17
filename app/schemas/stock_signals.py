"""Stock Signals API schemas (docs/37). Prices are Decimal (JSON strings), times are UTC."""

from __future__ import annotations

from datetime import datetime
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
