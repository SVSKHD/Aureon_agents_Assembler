"""Live market API schemas (docs/38): quotes, chart, status and the WebSocket protocol.

Prices are decimal strings in JSON. Every live value says where it came from and how fresh it
is: `status` (live / stale / closed / no_data), `age_seconds`, `received_at`, and for the
change, `prev_close_source` (feed / daily_candle / null).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

QuoteStatus = Literal["live", "stale", "closed", "no_data"]
PrevCloseSource = Literal["feed", "daily_candle"]
LiveTimeframe = Literal["1m", "5m", "15m", "1h", "1d"]


class MarketOut(BaseModel):
    open: bool
    session: str = Field(description="open, pre_open, closed, weekend or holiday (IST)")
    now: datetime


class LiveQuoteOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    instrument_id: int | None
    ref: str | None = Field(default=None, description="Dhan SEGMENT:SECURITY_ID")
    symbol: str | None = None
    name: str | None = None
    exchange: str | None = None
    segment: str | None = None
    ltp: Decimal | None
    ltt: datetime | None = Field(description="exchange time of the last trade")
    session_date: date | None = Field(description="IST session the last trade belongs to")
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = Field(default=None, description="cumulative volume of the session")
    atp: Decimal | None = Field(default=None, description="exchange average traded price")
    turnover: Decimal | None = None
    oi: int | None = None
    prev_close: Decimal | None = Field(description="close of the previous trading session")
    prev_close_source: PrevCloseSource | None
    prev_close_date: date | None = None
    change: Decimal | None
    change_pct: Decimal | None
    status: QuoteStatus
    age_seconds: float | None
    received_at: datetime | None
    source: str = "dhan_feed"
    computed_by: Literal["market_live", "feed_hash"] | None = None


class LiveQuotesOut(BaseModel):
    items: list[LiveQuoteOut]
    missing: list[int] = Field(
        description="requested instruments with no live data (not subscribed yet, or no trade)"
    )
    market: MarketOut
    as_of: datetime


class ChartCandleOut(BaseModel):
    ts: datetime = Field(description="bar open time (UTC)")
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    is_closed: bool
    source: Literal["broker", "ticks"]
    partial: bool = Field(description="a live bar the server did not watch from its start")
    ema_fast: Decimal | None
    ema_slow: Decimal | None
    vwap: Decimal | None


class ChartMarkerOut(BaseModel):
    ts: datetime
    kind: Literal["bullish_cross", "bearish_cross"]
    price: Decimal
    ema_fast: Decimal | None
    ema_slow: Decimal | None
    confirmed: bool = Field(description="false while the bar is still forming")


class SessionOut(BaseModel):
    date: date
    open: datetime
    close: datetime


class ChartLiveOut(BaseModel):
    included: bool
    bars: int
    partial_bars: int
    error: str | None


class ChartOut(BaseModel):
    instrument_id: int
    ref: str
    symbol: str
    timeframe: LiveTimeframe
    ema_fast_period: int
    ema_slow_period: int
    vwap_applicable: bool
    candles: list[ChartCandleOut]
    markers: list[ChartMarkerOut]
    sessions: list[SessionOut]
    market: MarketOut
    live: ChartLiveOut
    data_as_of: datetime | None
    step_seconds: int
    computed_at: datetime


class LiveStatusOut(BaseModel):
    market: MarketOut
    feed: dict[str, Any] = Field(description="Dhan feed health (same as /api/v1/dhan/feed)")
    subscriptions: dict[str, Any] | None
    market_live: dict[str, Any] | None = Field(description="market-live worker status record")
    hub: dict[str, Any] = Field(description="this API process's WebSocket hub")


# --- WebSocket protocol (documentation models; the socket sends JSON objects) -------------------


class WsTopicIn(BaseModel):
    """One topic in a subscribe/unsubscribe message. `kind` decides which fields are needed."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["quote", "candle", "rankings", "screener"]
    instrument_id: int | None = Field(default=None, description="quote, candle")
    timeframe: LiveTimeframe | None = Field(default=None, description="candle, screener")
    universe: str | None = Field(
        default=None, description="rankings (a MARKET_LIVE_UNIVERSES name)"
    )


class WsClientMessage(BaseModel):
    """Client → server."""

    op: Literal["subscribe", "unsubscribe", "ping"]
    topics: list[WsTopicIn] = Field(default_factory=list, max_length=200)
    id: str | None = Field(default=None, max_length=64, description="echoed in the ack")


class WsAck(BaseModel):
    type: Literal["ack"]
    op: str
    id: str | None
    accepted: list[str] = Field(description="topic names, e.g. quote:42, candle:42:5m")
    rejected: list[dict[str, str]] = Field(description="{topic, reason}")
    seq: int


class WsHello(BaseModel):
    type: Literal["hello"]
    connection_id: str
    server_time: datetime
    heartbeat_seconds: float
    max_topics: int
    market: MarketOut
    seq: int


class WsHeartbeat(BaseModel):
    type: Literal["heartbeat"]
    server_time: datetime
    market: MarketOut
    feed: dict[str, Any] = Field(description="connected, status (feed health), redis")
    seq: int


class WsSnapshot(BaseModel):
    """Sent after every accepted subscription and again after a resync. Apply it, then the
    updates that follow (they are never older than the snapshot)."""

    type: Literal["snapshot"]
    topic: str
    kind: Literal["quote", "candle", "rankings", "screener"]
    available: bool
    seq: int
    data: dict[str, Any] | None = Field(default=None, description="quote: a LiveQuoteOut")
    closed: list[dict[str, Any]] | None = Field(default=None, description="candle: closed bars")
    forming: dict[str, Any] | None = Field(default=None, description="candle: the forming bar")
    top: dict[str, list[dict[str, Any]]] | None = Field(default=None, description="rankings")
    coverage: dict[str, Any] | None = None
    scope: dict[str, Any] | None = None
    rows: list[dict[str, Any]] | None = Field(default=None, description="screener (live)")


class WsQuote(LiveQuoteOut):
    type: Literal["quote"]
    topic: str
    key: str
    seq: int


class WsCandle(BaseModel):
    type: Literal["candle"]
    topic: str
    instrument_id: int | None
    key: str
    timeframe: LiveTimeframe
    ts: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    ticks: int
    partial: bool
    closed: bool = Field(description="false: the bar is forming and will change")
    last_trade_at: datetime | None
    source: Literal["ticks"]
    seq: int


class WsRankings(BaseModel):
    type: Literal["rankings"]
    topic: str
    universe: str
    computed_at: datetime
    quotes_as_of: datetime | None
    market: MarketOut
    coverage: dict[str, int]
    scope: dict[str, Any]
    top: dict[str, list[dict[str, Any]]] = Field(description="gainers, losers, unusual_volume")
    seq: int


class WsScreener(BaseModel):
    """`basis: live` = the forming bar's EMA/VWAP state (can still change, never stored);
    `basis: confirmed` = a finished Stock Signals run on closed candles."""

    type: Literal["screener"]
    topic: str
    basis: Literal["live", "confirmed"]
    timeframe: str
    changes: list[dict[str, Any]]
    computed_at: datetime | None = None
    run_id: str | None = None
    boundary: datetime | None = None
    seq: int


class WsStatus(BaseModel):
    type: Literal["status"]
    redis: Literal["ok", "down"]
    resync: bool = Field(description="true: fresh snapshots follow for every topic")
    seq: int


class WsError(BaseModel):
    type: Literal["error"]
    code: str
    detail: str
    seq: int


class WsBye(BaseModel):
    type: Literal["bye"]
    reason: str = Field(description="session_ended, slow_consumer, server_shutdown, …")
    seq: int


class LiveProtocolOut(BaseModel):
    """GET /api/trading/live/protocol: the WebSocket contract, machine-readable."""

    url: str
    topics: dict[str, str]
    limits: dict[str, Any]
    close_codes: dict[str, str]
    universes: list[str]
    screener_timeframes: list[str]
    candle_timeframes: list[str]
    client: WsClientMessage | None = None
    events: dict[str, str]
    example_ack: WsAck | None = None
    example_hello: WsHello | None = None
    example_heartbeat: WsHeartbeat | None = None
    example_snapshot: WsSnapshot | None = None
    example_quote: WsQuote | None = None
    example_candle: WsCandle | None = None
    example_rankings: WsRankings | None = None
    example_screener: WsScreener | None = None
    example_status: WsStatus | None = None
    example_error: WsError | None = None
    example_bye: WsBye | None = None
