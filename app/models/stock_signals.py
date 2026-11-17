"""Stock Signals tables (docs/37-stock-signals.md), schema `trading`, migration 0032.

- stock_signal_runs: one evaluation of a universe for one timeframe and one candle close.
  Unique (timeframe, boundary), so the candle-close trigger, the 09:30 scheduler job and a
  retry can never run the same close twice.
- stock_signal_states: the latest classification per (instrument, timeframe), with the
  previous one, so a change of trend is visible without replaying history.
- stock_signal_events: one row per EMA crossover, unique on (instrument, timeframe,
  direction, cross candle). A VWAP confirmation updates that row (confirmed_at), it never
  makes a second event. Rows older than STOCK_SIGNALS_RETENTION_DAYS are purged.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.trading import JSON_EMPTY, PRICE, TS, S, _created, _uuid_pk

SIGNAL_STATES = (
    "('BULLISH_CONFIRMED','BULLISH_PENDING','BEARISH_CONFIRMED','BEARISH_PENDING',"
    "'BULLISH_TREND','BEARISH_TREND','MIXED','UNAVAILABLE')"
)
SIGNAL_TIMEFRAMES = "('5m','15m','1h','1d')"
RUN_STATES = "('queued','running','done','failed')"
SCORE = Numeric(6, 2)


class StockSignalRun(Base):
    __tablename__ = "stock_signal_runs"
    __table_args__ = (
        UniqueConstraint("timeframe", "boundary", name="uq_stock_signal_runs_tf_boundary"),
        CheckConstraint(f"status IN {RUN_STATES}", name="status"),
        CheckConstraint(f"timeframe IN {SIGNAL_TIMEFRAMES}", name="timeframe"),
        Index("ix_trading_stock_signal_runs_status_created", "status", "created_at"),
        S,
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    timeframe: Mapped[str] = mapped_column(String(4))
    # The candle close this run evaluates (UTC): every bar that closed by then.
    boundary: Mapped[datetime] = mapped_column(TS)
    trigger: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(12), server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    universe_size: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    history_requested: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    evaluated: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    unavailable: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    events_new: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    confirmations: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class StockSignalState(Base):
    __tablename__ = "stock_signal_states"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "timeframe", name="pk_stock_signal_states"),
        CheckConstraint(f"state IN {SIGNAL_STATES}", name="state"),
        CheckConstraint(f"previous_state IN {SIGNAL_STATES}", name="previous_state"),
        CheckConstraint(f"timeframe IN {SIGNAL_TIMEFRAMES}", name="timeframe"),
        Index("ix_trading_stock_signal_states_tf_state", "timeframe", "state"),
        S,
    )

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="CASCADE")
    )
    timeframe: Mapped[str] = mapped_column(String(4))
    state: Mapped[str] = mapped_column(String(20))
    previous_state: Mapped[str | None] = mapped_column(String(20))
    state_changed_at: Mapped[datetime | None] = mapped_column(TS)
    candle_ts: Mapped[datetime | None] = mapped_column(TS)
    candle_close_at: Mapped[datetime | None] = mapped_column(TS)
    last_price: Mapped[Decimal | None] = mapped_column(PRICE)
    ema_fast: Mapped[Decimal | None] = mapped_column(PRICE)
    ema_slow: Mapped[Decimal | None] = mapped_column(PRICE)
    vwap: Mapped[Decimal | None] = mapped_column(PRICE)
    vwap_applicable: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("trading.stock_signal_events.id", ondelete="SET NULL")
    )
    reason: Mapped[str | None] = mapped_column(String(200))
    trend_score: Mapped[Decimal | None] = mapped_column(SCORE)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    in_holdings: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    data_source: Mapped[str] = mapped_column(String(16), server_default=text("'dhan'"))
    evaluated_at: Mapped[datetime] = _created()
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.stock_signal_runs.id", ondelete="SET NULL")
    )


class StockSignalEvent(Base):
    __tablename__ = "stock_signal_events"
    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "timeframe",
            "direction",
            "crossed_ts",
            name="uq_stock_signal_events_cross",
        ),
        CheckConstraint("direction IN ('bullish','bearish')", name="direction"),
        CheckConstraint(f"state IN {SIGNAL_STATES}", name="state"),
        CheckConstraint(f"timeframe IN {SIGNAL_TIMEFRAMES}", name="timeframe"),
        Index("ix_trading_stock_signal_events_crossed", "timeframe", text("crossed_at DESC")),
        Index("ix_trading_stock_signal_events_created", "created_at"),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="CASCADE")
    )
    exchange: Mapped[str] = mapped_column(String(16))  # the instrument segment (NSE_EQ …)
    timeframe: Mapped[str] = mapped_column(String(4))
    direction: Mapped[str] = mapped_column(String(8))
    state: Mapped[str] = mapped_column(String(20))  # *_PENDING until confirmed
    crossed_ts: Mapped[datetime] = mapped_column(TS)  # open time of the cross bar
    crossed_at: Mapped[datetime] = mapped_column(TS)  # close time of the cross bar
    confirmed_at: Mapped[datetime | None] = mapped_column(TS)
    ema_fast: Mapped[Decimal | None] = mapped_column(PRICE)
    ema_slow: Mapped[Decimal | None] = mapped_column(PRICE)
    vwap: Mapped[Decimal | None] = mapped_column(PRICE)
    last_price: Mapped[Decimal | None] = mapped_column(PRICE)  # close of the cross bar
    confirm_price: Mapped[Decimal | None] = mapped_column(PRICE)
    confirm_vwap: Mapped[Decimal | None] = mapped_column(PRICE)
    candle_close_at: Mapped[datetime] = mapped_column(TS)  # newest bar seen at detection
    data_source: Mapped[str] = mapped_column(String(16), server_default=text("'dhan'"))
    data_as_of: Mapped[datetime | None] = mapped_column(TS)
    trend_score: Mapped[Decimal | None] = mapped_column(SCORE)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    in_holdings: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.stock_signal_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


# --- notifications (migration 0033) ------------------------------------------------------------

NOTIFICATION_KINDS = (
    "('bullish_cross','bearish_cross','vwap_confirmed','holding_reversal',"
    "'portfolio_trend_change','replacement_candidate')"
)


class StockSignalNotification(Base):
    """One in-app notification for one user. The id is the stream cursor (Last-Event-ID);
    (user_id, dedup_key) is unique, so a retried run never notifies twice."""

    __tablename__ = "stock_signal_notifications"
    __table_args__ = (
        UniqueConstraint("user_id", "dedup_key", name="uq_stock_signal_notifications_dedup"),
        CheckConstraint(f"kind IN {NOTIFICATION_KINDS}", name="kind"),
        Index("ix_trading_stock_signal_notifications_user_id", "user_id", text("id DESC")),
        Index(
            "ix_trading_stock_signal_notifications_unread",
            "user_id",
            postgresql_where=text("read_at IS NULL"),
        ),
        Index("ix_trading_stock_signal_notifications_created", "created_at"),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    event_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("trading.stock_signal_events.id", ondelete="SET NULL")
    )
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="CASCADE")
    )
    timeframe: Mapped[str | None] = mapped_column(String(4))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    dedup_key: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = _created()
    read_at: Mapped[datetime | None] = mapped_column(TS)


class StockSignalPrefs(Base):
    """A user's Stock Signals notification settings. No row = the defaults: on, every kind,
    every timeframe, only instruments on the user's watchlists or holdings."""

    __tablename__ = "stock_signal_prefs"
    __table_args__ = (S,)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.users.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    # Empty = every kind / every timeframe.
    kinds: Mapped[list[str]] = mapped_column(ARRAY(String(32)), server_default=text("'{}'"))
    timeframes: Mapped[list[str]] = mapped_column(ARRAY(String(4)), server_default=text("'{}'"))
    # Also every crossover in the evaluated universe, not just watchlists and holdings.
    all_market: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    confirmed_only: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    updated_at: Mapped[datetime] = _created()
