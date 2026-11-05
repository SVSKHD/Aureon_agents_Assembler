"""Broker-specific tables: dhan.* and mt5.* (filled in by Trading Phases B and C).

Credentials (Dhan access token, MT5 password) are NEVER stored here: they live in env or in
core.app_settings (encrypted). dhan.connections keeps only safe connection metadata.
Each broker's full payload is kept in `raw`, so a field we don't model yet is never lost.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

TS = DateTime(timezone=True)
PRICE = Numeric(24, 8)
QTY = Numeric(24, 6)
MONEY = Numeric(20, 4)
JSON_EMPTY = text("'{}'::jsonb")


# --- dhan ---------------------------------------------------------------------------------


class DhanInstrument(Base):
    """One row of Dhan's instrument (scrip) master. Phase B maps it into trading.instruments."""

    __tablename__ = "instruments"
    __table_args__ = (
        PrimaryKeyConstraint("exchange_segment", "security_id", name="pk_instruments"),
        Index("ix_dhan_instruments_trading_symbol", "trading_symbol"),
        {"schema": "dhan"},
    )

    exchange_segment: Mapped[str] = mapped_column(String(16))  # e.g. NSE_EQ, NSE_FNO
    security_id: Mapped[str] = mapped_column(String(32))
    trading_symbol: Mapped[str] = mapped_column(String(100))
    instrument: Mapped[str | None] = mapped_column(String(32))
    lot_size: Mapped[Decimal | None] = mapped_column(QTY)
    tick_size: Mapped[Decimal | None] = mapped_column(PRICE)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    strike_price: Mapped[Decimal | None] = mapped_column(PRICE)
    option_type: Mapped[str | None] = mapped_column(String(4))
    underlying_security_id: Mapped[str | None] = mapped_column(String(32))
    trading_instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="SET NULL")
    )
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    fetched_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class DhanAccountProfile(Base):
    __tablename__ = "account_profiles"
    __table_args__ = ({"schema": "dhan"},)

    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    dhan_client_id: Mapped[str] = mapped_column(String(32))
    token_expires_at: Mapped[datetime | None] = mapped_column(TS)
    active_segments: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    data_plan: Mapped[str | None] = mapped_column(String(32))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    refreshed_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


# What dhan.connections.status can be (docs/28-dhan-operations.md, "Connection states").
DHAN_CONNECTION_STATES = (
    "connected",  # GET /profile answered for the configured client id
    "unverified",  # a token is in use but Dhan could not be asked yet (outage, just set)
    "expired",  # the token's own expiry has passed (no Dhan call needed to know)
    "rejected",  # Dhan refused the token: invalid, revoked, or no longer accepted
    "mismatch",  # the token belongs to a different Dhan client id
    "disconnected",  # an admin logged out and no manual token is in use
    "not_configured",  # no client id, or no token at all
)


class DhanConnection(Base):
    """The Dhan connection's SAFE metadata, one row per client id, kept across restarts.

    Never a token: `credential_fp` is an HMAC (keyed by SECRET_KEY) of the token in use, only
    so a replaced credential is noticed; it is never returned by the API. Logging out keeps the
    row (and every broker account / profile row), it only changes the status.
    """

    __tablename__ = "connections"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in DHAN_CONNECTION_STATES) + ")",
            name="status",
        ),
        {"schema": "dhan"},
    )

    dhan_client_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    token_source: Mapped[str] = mapped_column(String(8), server_default=text("'none'"))
    credential_fp: Mapped[str | None] = mapped_column(String(32))
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    connected_at: Mapped[datetime | None] = mapped_column(TS)
    connected_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(TS)
    disconnected_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    data_plan: Mapped[str | None] = mapped_column(String(32))
    data_valid_until: Mapped[str | None] = mapped_column(String(40))
    active_segments: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    last_verified_at: Mapped[datetime | None] = mapped_column(TS)
    last_check_at: Mapped[datetime | None] = mapped_column(TS)
    last_check_outcome: Mapped[str | None] = mapped_column(String(24))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class DhanFundSnapshot(Base):
    __tablename__ = "fund_snapshots"
    __table_args__ = (
        Index("ix_dhan_fund_snapshots_broker_account_id_as_of", "broker_account_id", "as_of"),
        {"schema": "dhan"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="CASCADE")
    )
    available_balance: Mapped[Decimal | None] = mapped_column(MONEY)
    sod_limit: Mapped[Decimal | None] = mapped_column(MONEY)
    collateral: Mapped[Decimal | None] = mapped_column(MONEY)
    utilized: Mapped[Decimal | None] = mapped_column(MONEY)
    withdrawable: Mapped[Decimal | None] = mapped_column(MONEY)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    as_of: Mapped[datetime] = mapped_column(TS)


# --- mt5 ----------------------------------------------------------------------------------


class Mt5Terminal(Base):
    """The MT5 account/terminal the Aureon_agentic_bot reports on (Phase C)."""

    __tablename__ = "terminals"
    __table_args__ = ({"schema": "mt5"},)

    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    login: Mapped[str] = mapped_column(String(32))
    server: Mapped[str] = mapped_column(String(100))
    company: Mapped[str | None] = mapped_column(String(100))
    currency: Mapped[str | None] = mapped_column(String(3))
    leverage: Mapped[int | None] = mapped_column(Integer)
    terminal_build: Mapped[int | None] = mapped_column(Integer)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    last_seen_at: Mapped[datetime | None] = mapped_column(TS)


# `server` of a symbol spec an admin registered (PUT /api/trading/mt5/instruments/{symbol})
# while MT5_SERVER is unset (paper trading without an MT5 bot). A spec synced for the real
# server wins over it (app/services/paper.py instrument_spec).
MANUAL_MT5_SERVER = "manual"


class Mt5Symbol(Base):
    """Symbol spec, per broker server (contract size, digits and volume limits differ)."""

    __tablename__ = "symbols"
    __table_args__ = (
        PrimaryKeyConstraint("server", "symbol", name="pk_symbols"),
        {"schema": "mt5"},
    )

    server: Mapped[str] = mapped_column(String(100))
    symbol: Mapped[str] = mapped_column(String(64))
    digits: Mapped[int | None] = mapped_column(Integer)
    point: Mapped[Decimal | None] = mapped_column(PRICE)
    contract_size: Mapped[Decimal | None] = mapped_column(QTY)
    volume_min: Mapped[Decimal | None] = mapped_column(QTY)
    volume_step: Mapped[Decimal | None] = mapped_column(QTY)
    volume_max: Mapped[Decimal | None] = mapped_column(QTY)
    currency_base: Mapped[str | None] = mapped_column(String(8))
    currency_profit: Mapped[str | None] = mapped_column(String(8))
    currency_margin: Mapped[str | None] = mapped_column(String(8))
    trading_instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="SET NULL")
    )
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    updated_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Mt5AccountSnapshot(Base):
    __tablename__ = "account_snapshots"
    __table_args__ = (
        Index("ix_mt5_account_snapshots_broker_account_id_as_of", "broker_account_id", "as_of"),
        {"schema": "mt5"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="CASCADE")
    )
    balance: Mapped[Decimal | None] = mapped_column(MONEY)
    equity: Mapped[Decimal | None] = mapped_column(MONEY)
    margin: Mapped[Decimal | None] = mapped_column(MONEY)
    free_margin: Mapped[Decimal | None] = mapped_column(MONEY)
    margin_level: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    profit: Mapped[Decimal | None] = mapped_column(MONEY)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    as_of: Mapped[datetime] = mapped_column(TS)


# --- mt5 push ingest (migration 0018, docs/12-mt5-adapter.md §3) -------------------------

# Failure kinds the server classifies a push into (the UI's "why").
FAILURE_KINDS = (
    "mt5_disconnected",
    "stale_ticks",
    "guardian_error",
    "discord_error",
    "exception",
    "stale_heartbeat",
)


class Mt5Push(Base):
    """One accepted push (idempotency by push_id: the PK). Purged after MT5_EVENT_RETENTION_DAYS."""

    __tablename__ = "pushes"
    __table_args__ = (Index("ix_mt5_pushes_received_at", "received_at"), {"schema": "mt5"})

    push_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    seq: Mapped[int | None] = mapped_column(BigInteger)
    generated_at: Mapped[datetime] = mapped_column(TS)
    received_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    size_bytes: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    events_new: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    events_duplicate: Mapped[int] = mapped_column(Integer, server_default=text("0"))


class Mt5BotSnapshot(Base):
    """The latest push per bot: everything status/v1 lacked (guardian, strategy, health,
    signals_latest) plus the v1 blocks the dashboard reads. One row, replaced per push."""

    __tablename__ = "bot_snapshots"
    __table_args__ = ({"schema": "mt5"},)

    bot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    push_id: Mapped[uuid.UUID] = mapped_column()
    seq: Mapped[int | None] = mapped_column(BigInteger)
    schema_name: Mapped[str] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(TS)
    received_at: Mapped[datetime] = mapped_column(TS)
    bot: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    agents: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    health: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    guardian: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    signals_latest: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    strategy: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    halt: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    console: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ema921e: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ema921e_trades: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    backtests: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    errors: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)


class Mt5Event(Base):
    """Journal events and tracebacks, once each (PK = the bot's event id)."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_mt5_events_t", "t"),
        Index("ix_mt5_events_kind_t", "kind", "t"),
        Index("ix_mt5_events_symbol_t", "symbol", "t"),
        {"schema": "mt5"},
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    t: Mapped[datetime] = mapped_column(TS)
    event: Mapped[str] = mapped_column(String(64))
    # Timeline badge: P, cross, protected, secured, exit, closed, error, or the raw event name.
    kind: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str | None] = mapped_column(String(64))
    ticket: Mapped[int | None] = mapped_column(BigInteger)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    push_id: Mapped[uuid.UUID | None] = mapped_column()
    received_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class Mt5Failure(Base):
    """A classified failure. One OPEN row per `key` (kind + symbol); a recurrence bumps
    last_seen/count, and the row is resolved when the condition clears."""

    __tablename__ = "failures"
    __table_args__ = (
        CheckConstraint(f"kind IN {FAILURE_KINDS}", name="kind"),
        Index(
            "uq_mt5_failures_open_key",
            "key",
            unique=True,
            postgresql_where=text("resolved_at IS NULL"),
        ),
        Index("ix_mt5_failures_last_seen", "last_seen"),
        {"schema": "mt5"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    key: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    first_seen: Mapped[datetime] = mapped_column(TS)
    last_seen: Mapped[datetime] = mapped_column(TS)
    count: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    resolved_at: Mapped[datetime | None] = mapped_column(TS)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)


class Mt5Candle(Base):
    """Recent bars the bot pushed (UTC open time), with its EMA values. Kept separate from
    trading.market_candles: no registered instrument is needed and the source is the bot."""

    __tablename__ = "candles"
    __table_args__ = (
        PrimaryKeyConstraint("symbol", "timeframe", "ts", name="pk_candles"),
        Index("ix_mt5_candles_ts", "ts"),
        {"schema": "mt5"},
    )

    symbol: Mapped[str] = mapped_column(String(64))
    timeframe: Mapped[str] = mapped_column(String(4))
    ts: Mapped[datetime] = mapped_column(TS)
    open: Mapped[Decimal] = mapped_column(PRICE)
    high: Mapped[Decimal] = mapped_column(PRICE)
    low: Mapped[Decimal] = mapped_column(PRICE)
    close: Mapped[Decimal] = mapped_column(PRICE)
    volume: Mapped[Decimal | None] = mapped_column(QTY)
    ema: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    received_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
