"""Market terminal schemas (docs/35-market-data.md): screener, watchlists, instrument status,
candle backfill and on-demand indicators. Decimals serialize as JSON strings, never floats.

The screener request is ASTRA-FRONTEND `src/trading/screener.ts` / docs/market-terminal.md §6
field for field; anything the server adds to a response is additive.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.trading.types import Timeframe
from app.schemas.trading import InstrumentOut

Period = Annotated[int, Field(ge=1, le=500)]
Relation = Literal["above", "below"]
Direction = Literal["bullish", "bearish"]
Op = Literal["<", ">"]
CandleMode = Literal["confirmed", "live"]
Exchange = Literal["NSE", "BSE"]
Kind = Literal["equity", "index", "future"]
INTRADAY = frozenset(tf for tf in Timeframe if tf is not Timeframe.D1)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _FastSlow(_Strict):
    fast: Period
    slow: Period

    @model_validator(mode="after")
    def _order(self) -> _FastSlow:
        if self.fast >= self.slow:
            raise ValueError("fast period must be shorter than slow")
        return self


class EmaCrossFilter(_FastSlow):
    type: Literal["ema_cross"]
    direction: Direction
    within_bars: Annotated[int, Field(ge=1, le=100)] = 1


class EmaPositionFilter(_FastSlow):
    type: Literal["ema_position"]
    relation: Relation


class PriceVsEmaFilter(_Strict):
    type: Literal["price_vs_ema"]
    period: Period
    relation: Relation


class RsiFilter(_Strict):
    type: Literal["rsi"]
    period: Period
    op: Op
    value: Annotated[float, Field(ge=0, le=100)]


class MacdCrossFilter(_FastSlow):
    type: Literal["macd_cross"]
    signal: Period
    direction: Direction
    # Not in the §6 body (the cross on the evaluated bar); accepted as an extension.
    within_bars: Annotated[int, Field(ge=1, le=100)] = 1


class VolumeFilter(_Strict):
    type: Literal["volume"]
    op: Op
    multiple_of_avg: Annotated[float, Field(gt=0, le=1000)]
    avg_period: Period


class AdxFilter(_Strict):
    type: Literal["adx"]
    period: Period
    op: Op
    value: Annotated[float, Field(ge=0, le=100)]


class VwapPositionFilter(_Strict):
    type: Literal["vwap_position"]
    relation: Relation


ScreenerFilter = Annotated[
    EmaCrossFilter
    | EmaPositionFilter
    | PriceVsEmaFilter
    | RsiFilter
    | MacdCrossFilter
    | VolumeFilter
    | AdxFilter
    | VwapPositionFilter,
    Field(discriminator="type"),
]


class Universe(_Strict):
    exchange: list[Exchange] = Field(default_factory=list, max_length=2)
    kind: list[Kind] = Field(default_factory=list, max_length=3)
    # A server watchlist id (uuid). When set, the universe is that list and exchange/kind are
    # ignored. A device-only list id (not a uuid) is rejected with 422.
    watchlist_id: str | None = Field(default=None, max_length=80)

    @field_validator("exchange", "kind")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        return sorted(set(v))

    @model_validator(mode="after")
    def _non_empty(self) -> Universe:
        if self.watchlist_id is None and (not self.exchange or not self.kind):
            raise ValueError("pick an exchange and a kind, or a watchlist")
        if self.watchlist_id is not None:
            try:
                uuid.UUID(self.watchlist_id)
            except ValueError as exc:
                raise ValueError(
                    "watchlist_id is not a server watchlist (device-only lists cannot be "
                    "screened; save the list to the server first)"
                ) from exc
        return self


SortField = Literal["change_pct", "last", "symbol", "crossed_at"]


class ScreenerSort(_Strict):
    field: SortField = "change_pct"
    dir: Literal["asc", "desc"] = "desc"


class ScreenerRequest(_Strict):
    universe: Universe
    timeframe: Timeframe
    candle: CandleMode = "confirmed"
    filters: list[ScreenerFilter] = Field(min_length=1, max_length=12)
    sort: ScreenerSort = Field(default_factory=ScreenerSort)
    limit: Annotated[int, Field(ge=1, le=500)] = 50
    offset: Annotated[int, Field(ge=0, le=100_000)] = 0

    @model_validator(mode="after")
    def _timeframe_fits(self) -> ScreenerRequest:
        if self.timeframe not in INTRADAY and any(f.type == "vwap_position" for f in self.filters):
            raise ValueError("vwap_position needs an intraday timeframe (VWAP resets daily)")
        return self


ScreenerSignal = Literal["bullish_cross", "bearish_cross", "above", "below"]
RunStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class ScreenerItem(BaseModel):
    instrument: InstrumentOut
    last: Decimal | None
    change_pct: Decimal | None
    indicators: dict[str, str | None]
    signal: ScreenerSignal | None
    crossed_at: datetime | None
    # Open time of the candle the filters were evaluated on.
    data_as_of: datetime
    # Additive: False only in `live` mode when the evaluated candle is still forming.
    candle_closed: bool = True


class ScreenerCoverage(BaseModel):
    """Why instruments were not evaluated. Nothing here is a match or a non-match."""

    scanned: int = 0
    matched: int = 0
    no_data: int = 0
    insufficient_data: int = 0
    stale: int = 0


class ScreenerResponse(BaseModel):
    as_of: datetime
    candle: CandleMode
    timeframe: Timeframe
    universe_size: int
    total: int
    items: list[ScreenerItem]
    # --- additive job contract (docs/35-market-data.md "Screener jobs") ---
    run_id: uuid.UUID
    status: RunStatus
    coverage: ScreenerCoverage
    # Newest candle open time found in the universe for this timeframe (None: no candles).
    universe_data_as_of: datetime | None = None
    cached: bool = False
    detail: str | None = None


class ScreenerRunOut(BaseModel):
    id: uuid.UUID
    status: RunStatus
    timeframe: str
    universe_size: int
    scanned: int
    matched: int
    skipped: dict[str, int]
    data_as_of: datetime | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


WorkerState = Literal["ok", "stale", "down"]


class ScreenerStatus(BaseModel):
    last_run_at: datetime | None
    universe_size: int | None
    candles_as_of: dict[str, datetime | None]
    worker: WorkerState
    queued_runs: int
    queued_backfills: int


# --- watchlists (docs/market-terminal.md §5) ----------------------------------------------


class WatchlistIn(_Strict):
    name: str = Field(min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class WatchlistPatch(_Strict):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name must not be blank")
        return v


class WatchlistItemIn(_Strict):
    instrument_id: int = Field(ge=1)


class WatchlistItemOut(BaseModel):
    instrument_id: int
    added_at: datetime
    position: int
    # None: the instrument is inactive (expired, delisted or missing from the master).
    instrument: InstrumentOut | None


class WatchlistOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    items: list[WatchlistItemOut]
    created_at: datetime
    updated_at: datetime


# --- instruments ----------------------------------------------------------------------------


class InstrumentStatus(BaseModel):
    broker: Literal["dhan"] = "dhan"
    last_synced_at: datetime | None
    count: int
    by_segment: dict[str, int]
    last_error: str | None
    # Additive diagnostics.
    last_run_status: str | None = None
    last_run_started_at: datetime | None = None
    stale: bool = True
    max_age_hours: float


# --- candle history -------------------------------------------------------------------------


class BackfillIn(_Strict):
    instrument_ids: list[int] = Field(min_length=1, max_length=200)
    timeframe: Timeframe
    days: Annotated[int, Field(ge=1, le=7300)]


class BackfillRejected(BaseModel):
    instrument_id: int
    reason: str


class BackfillQueued(BaseModel):
    queued: int
    already_open: int
    rejected: list[BackfillRejected]


class BackfillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    instrument_id: int
    timeframe: str
    days: int
    status: str
    source: str
    attempts: int
    candles: int | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
