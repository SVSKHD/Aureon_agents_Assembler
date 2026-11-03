"""Broker-specific tables: dhan.* and mt5.* (filled in by Trading Phases B and C).

Credentials (Dhan access token, MT5 password) are NEVER stored here: they live in env only.
Each broker's full payload is kept in `raw`, so a field we don't model yet is never lost.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
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
