"""trading.* — broker-agnostic trading tables.

Conventions:
- Money / prices / quantities: NUMERIC (Decimal in Python), never float.
- Timestamps: timestamptz in UTC; market rows also carry a `session` tag (Asia/London/NY).
- Broker identifiers are always namespaced by account: a row points at
  trading.broker_accounts, which is unique on (broker, account_id).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
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
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

S = {"schema": "trading"}
PRICE = Numeric(24, 8)
QTY = Numeric(24, 6)
MONEY = Numeric(20, 4)
TS = DateTime(timezone=True)
JSON_EMPTY = text("'{}'::jsonb")

BROKERS = "('dhan','mt5')"
MODES = "('OBSERVE','PAPER','LIVE')"
SESSIONS = "('Asia','London','NY')"
SIDES = "('BUY','SELL')"
TIMEFRAMES = "('1m','5m','15m','30m','1h','4h','1d')"
ORDER_TYPES = "('MARKET','LIMIT','STOP','STOP_LIMIT')"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, server_default=text("gen_random_uuid()"))


def _created() -> Mapped[datetime]:
    return mapped_column(TS, server_default=func.now())


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"
    __table_args__ = (
        UniqueConstraint("broker", "account_id", name="uq_broker_accounts_broker_account_id"),
        CheckConstraint(f"broker IN {BROKERS}", name="broker"),
        CheckConstraint(f"mode IN {MODES}", name="mode"),
        S,
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    broker: Mapped[str] = mapped_column(String(16))
    account_id: Mapped[str] = mapped_column(String(64))  # the broker's own id (client id/login)
    label: Mapped[str | None] = mapped_column(String(100))
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.users.id", ondelete="RESTRICT"), index=True
    )
    mode: Mapped[str] = mapped_column(String(8), server_default=text("'OBSERVE'"))
    mode_changed_at: Mapped[datetime | None] = mapped_column(TS)
    mode_changed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    base_currency: Mapped[str] = mapped_column(String(3))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint(
            "broker", "broker_instrument_id", name="uq_instruments_broker_broker_instrument_id"
        ),
        CheckConstraint(f"broker IN {BROKERS}", name="broker"),
        Index("ix_trading_instruments_symbol", "symbol"),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    broker: Mapped[str] = mapped_column(String(16))
    broker_instrument_id: Mapped[str] = mapped_column(String(64))  # Dhan security_id / MT5 symbol
    symbol: Mapped[str] = mapped_column(String(100))
    exchange: Mapped[str | None] = mapped_column(String(16))
    segment: Mapped[str | None] = mapped_column(String(32))
    instrument_type: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(3))
    lot_size: Mapped[Decimal] = mapped_column(QTY)
    tick_size: Mapped[Decimal] = mapped_column(PRICE)
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[Decimal | None] = mapped_column(PRICE)
    option_type: Mapped[str | None] = mapped_column(String(4))
    underlying: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    updated_at: Mapped[datetime] = _created()


class Strategy(Base):
    __tablename__ = "strategies"
    __table_args__ = (UniqueConstraint("name", name="uq_strategies_name"), S)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))
    created_at: Mapped[datetime] = _created()


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="uq_strategy_versions_strategy_id_version"),
        CheckConstraint("version >= 1", name="version"),
        S,
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.strategies.id", ondelete="CASCADE")
    )
    version: Mapped[int] = mapped_column(Integer)
    # Identifies the deterministic implementation, e.g. "ema_cross" (Phase D).
    code_ref: Mapped[str] = mapped_column(String(100))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "timeframe", "ts", name="pk_market_candles"),
        CheckConstraint(f"timeframe IN {TIMEFRAMES}", name="timeframe"),
        CheckConstraint(f"session IN {SESSIONS}", name="session"),
        CheckConstraint("high >= low", name="high_low"),
        S,
    )

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="CASCADE")
    )
    timeframe: Mapped[str] = mapped_column(String(4))
    ts: Mapped[datetime] = mapped_column(TS)  # candle open time
    open: Mapped[Decimal] = mapped_column(PRICE)
    high: Mapped[Decimal] = mapped_column(PRICE)
    low: Mapped[Decimal] = mapped_column(PRICE)
    close: Mapped[Decimal] = mapped_column(PRICE)
    volume: Mapped[Decimal] = mapped_column(QTY, server_default=text("0"))
    oi: Mapped[Decimal | None] = mapped_column(QTY)
    session: Mapped[str] = mapped_column(String(8))
    source: Mapped[str] = mapped_column(String(16), server_default=text("'broker'"))
    is_gap: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    ingested_at: Mapped[datetime] = _created()


class TradingSignal(Base):
    __tablename__ = "trading_signals"
    __table_args__ = (
        # Phase D dedupe key.
        UniqueConstraint(
            "strategy_version_id",
            "instrument_id",
            "timeframe",
            "candle_time",
            name="uq_trading_signals_dedupe",
        ),
        CheckConstraint("side IN ('BUY','SELL','NO_TRADE')", name="side"),
        CheckConstraint(f"session IN {SESSIONS}", name="session"),
        CheckConstraint(f"timeframe IN {TIMEFRAMES}", name="timeframe"),
        Index(
            "ix_trading_trading_signals_instrument_id_candle_time", "instrument_id", "candle_time"
        ),
        S,
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.strategy_versions.id", ondelete="RESTRICT")
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="RESTRICT")
    )
    timeframe: Mapped[str] = mapped_column(String(4))
    candle_time: Mapped[datetime] = mapped_column(TS)
    session: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    strength: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    created_at: Mapped[datetime] = _created()


class RiskPolicy(Base):
    __tablename__ = "risk_policies"
    __table_args__ = (
        UniqueConstraint("broker_account_id", name="uq_risk_policies_broker_account_id"),
        CheckConstraint(
            "max_daily_loss >= 0 AND max_exposure >= 0 AND max_position_size >= 0",
            name="non_negative",
        ),
        CheckConstraint("max_drawdown_pct >= 0 AND max_drawdown_pct <= 100", name="drawdown"),
        S,
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="CASCADE")
    )
    max_daily_loss: Mapped[Decimal] = mapped_column(MONEY)  # account currency
    max_exposure: Mapped[Decimal] = mapped_column(MONEY)  # gross notional, account currency
    max_position_size: Mapped[Decimal] = mapped_column(QTY)  # per instrument, in lots/units
    max_drawdown_pct: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    allowed_sessions: Mapped[list[str]] = mapped_column(
        ARRAY(String(8)), server_default=text("ARRAY['Asia','London','NY']::varchar[]")
    )
    kill_switch: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class OrderIntent(Base):
    __tablename__ = "order_intents"
    __table_args__ = (
        UniqueConstraint("correlation_id", name="uq_order_intents_correlation_id"),
        CheckConstraint(f"side IN {SIDES}", name="side"),
        CheckConstraint(f"order_type IN {ORDER_TYPES}", name="order_type"),
        CheckConstraint(f"mode IN {MODES}", name="mode"),
        CheckConstraint("quantity > 0", name="quantity"),
        CheckConstraint("source IN ('manual','strategy')", name="source"),
        CheckConstraint(
            "status IN ('PENDING_RISK','REJECTED','APPROVED','SUBMITTED','FILLED',"
            "'CANCELLED','EXPIRED')",
            name="status",
        ),
        Index(
            "ix_trading_order_intents_broker_account_id_created_at",
            "broker_account_id",
            "created_at",
        ),
        S,
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="RESTRICT")
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="RESTRICT")
    )
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.trading_signals.id", ondelete="SET NULL")
    )
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(12))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    limit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_price: Mapped[Decimal | None] = mapped_column(PRICE)
    product: Mapped[str | None] = mapped_column(String(16))
    validity: Mapped[str] = mapped_column(String(8), server_default=text("'DAY'"))
    # Account mode when the intent was created; the execution path never upgrades it.
    mode: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'PENDING_RISK'"))
    # Who/what asked for it. AI agents are deliberately not a valid source.
    source: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class BrokerOrderRow(Base):
    __tablename__ = "broker_orders"
    __table_args__ = (
        UniqueConstraint(
            "broker_account_id", "broker_order_id", name="uq_broker_orders_account_order"
        ),
        CheckConstraint(f"side IN {SIDES}", name="side"),
        CheckConstraint(
            "normalized_status IN ('OPEN','PARTIAL','FILLED','CANCELLED','REJECTED','EXPIRED',"
            "'UNKNOWN')",
            name="normalized_status",
        ),
        S,
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="RESTRICT")
    )
    order_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.order_intents.id", ondelete="SET NULL"), index=True
    )
    broker_order_id: Mapped[str] = mapped_column(String(64))
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="SET NULL")
    )
    symbol: Mapped[str] = mapped_column(String(100))
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(12))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    filled_quantity: Mapped[Decimal] = mapped_column(QTY, server_default=text("0"))
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(PRICE)
    raw_status: Mapped[str] = mapped_column(String(32))
    normalized_status: Mapped[str] = mapped_column(String(12))
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    placed_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = _created()
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)


class OrderEvent(Base):
    __tablename__ = "order_events"
    __table_args__ = (
        CheckConstraint(
            "order_intent_id IS NOT NULL OR broker_order_row_id IS NOT NULL", name="has_subject"
        ),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    order_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.order_intents.id", ondelete="CASCADE"), index=True
    )
    broker_order_row_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.broker_orders.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(32))
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    occurred_at: Mapped[datetime] = mapped_column(TS)
    recorded_at: Mapped[datetime] = _created()


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (
        CheckConstraint(f"side IN {SIDES}", name="side"),
        CheckConstraint(f"session IN {SESSIONS}", name="session"),
        CheckConstraint("quantity > 0 AND price > 0 AND fees >= 0", name="amounts"),
        Index(
            "uq_executions_account_broker_trade",
            "broker_account_id",
            "broker_trade_id",
            unique=True,
            postgresql_where=text("broker_trade_id IS NOT NULL"),
        ),
        Index(
            "ix_trading_executions_broker_account_id_executed_at",
            "broker_account_id",
            "executed_at",
        ),
        S,
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="RESTRICT")
    )
    broker_order_row_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.broker_orders.id", ondelete="SET NULL")
    )
    order_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.order_intents.id", ondelete="SET NULL")
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="RESTRICT")
    )
    broker_trade_id: Mapped[str | None] = mapped_column(String(64))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    price: Mapped[Decimal] = mapped_column(PRICE)
    fees: Mapped[Decimal] = mapped_column(MONEY, server_default=text("0"))
    currency: Mapped[str] = mapped_column(String(3))
    executed_at: Mapped[datetime] = mapped_column(TS)
    session: Mapped[str] = mapped_column(String(8))
    # True for PAPER fills; the UI labels these "simulated", never "broker-reported".
    is_simulated: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = _created()


class PositionSnapshot(Base):
    __tablename__ = "positions_snapshots"
    __table_args__ = (
        CheckConstraint("source IN ('broker','simulated','calculated')", name="source"),
        Index(
            "ix_trading_positions_snapshots_broker_account_id_as_of", "broker_account_id", "as_of"
        ),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="CASCADE")
    )
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="SET NULL")
    )
    symbol: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[Decimal] = mapped_column(QTY)  # signed
    avg_price: Mapped[Decimal] = mapped_column(PRICE)
    last_price: Mapped[Decimal | None] = mapped_column(PRICE)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    source: Mapped[str] = mapped_column(String(12))
    as_of: Mapped[datetime] = mapped_column(TS)


class RiskDecision(Base):
    __tablename__ = "risk_decisions"
    __table_args__ = (
        Index(
            "ix_trading_risk_decisions_broker_account_id_created_at",
            "broker_account_id",
            "created_at",
        ),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="CASCADE")
    )
    order_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.order_intents.id", ondelete="SET NULL"), index=True
    )
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.trading_signals.id", ondelete="SET NULL")
    )
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.risk_policies.id", ondelete="SET NULL")
    )
    approved: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    # Exactly what was evaluated: the policy as it was, the inputs, and each check's result.
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = _created()


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','done','failed')", name="status"),
        CheckConstraint(f"timeframe IN {TIMEFRAMES}", name="timeframe"),
        CheckConstraint("period_end > period_start", name="period"),
        S,
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.strategy_versions.id", ondelete="RESTRICT")
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="RESTRICT")
    )
    timeframe: Mapped[str] = mapped_column(String(4))
    period_start: Mapped[datetime] = mapped_column(TS)
    period_end: Mapped[datetime] = mapped_column(TS)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    costs: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    status: Mapped[str] = mapped_column(String(8), server_default=text("'queued'"))
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = _created()
    started_at: Mapped[datetime | None] = mapped_column(TS)
    finished_at: Mapped[datetime | None] = mapped_column(TS)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (
        CheckConstraint(f"side IN {SIDES}", name="side"),
        CheckConstraint(f"session IN {SESSIONS}", name="session"),
        Index(
            "ix_trading_backtest_trades_backtest_run_id_entry_time", "backtest_run_id", "entry_time"
        ),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    backtest_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.backtest_runs.id", ondelete="CASCADE")
    )
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    entry_time: Mapped[datetime] = mapped_column(TS)
    entry_price: Mapped[Decimal] = mapped_column(PRICE)
    exit_time: Mapped[datetime | None] = mapped_column(TS)
    exit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    pnl: Mapped[Decimal | None] = mapped_column(MONEY)
    fees: Mapped[Decimal] = mapped_column(MONEY, server_default=text("0"))
    session: Mapped[str] = mapped_column(String(8))


class AiAnalysis(Base):
    __tablename__ = "ai_analyses"
    __table_args__ = (
        CheckConstraint("(signal_id IS NULL) <> (backtest_run_id IS NULL)", name="one_subject"),
        S,
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.trading_signals.id", ondelete="CASCADE"), index=True
    )
    backtest_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.backtest_runs.id", ondelete="CASCADE"), index=True
    )
    model: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    # Explanations only: nothing in this table can create or approve an order intent.
    created_at: Mapped[datetime] = _created()


class IndicatorValue(Base):
    """Technical-analysis output per candle (Phase D). `name` carries the parameters
    ("ema:9", "macd:12:26:9"); `values` holds Decimal strings, never floats."""

    __tablename__ = "indicator_values"
    __table_args__ = (
        PrimaryKeyConstraint(
            "instrument_id", "timeframe", "name", "ts", name="pk_indicator_values"
        ),
        CheckConstraint(f"timeframe IN {TIMEFRAMES}", name="timeframe"),
        S,
    )

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="CASCADE")
    )
    timeframe: Mapped[str] = mapped_column(String(4))
    name: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime] = mapped_column(TS)  # candle open time the value is for
    values: Mapped[dict[str, Any]] = mapped_column(JSONB)
    computed_at: Mapped[datetime] = _created()


class PaperLedger(Base):
    """Money movements of a PAPER account: realized P&L and charges per simulated fill.
    Equity = PAPER_STARTING_CAPITAL + sum(amount) + unrealized P&L."""

    __tablename__ = "paper_ledger"
    __table_args__ = (
        CheckConstraint("kind IN ('realized','fees')", name="kind"),
        Index(
            "ix_trading_paper_ledger_broker_account_id_created_at",
            "broker_account_id",
            "created_at",
        ),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="CASCADE")
    )
    execution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.executions.id", ondelete="SET NULL")
    )
    order_intent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading.order_intents.id", ondelete="SET NULL")
    )
    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("trading.instruments.id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(10))
    amount: Mapped[Decimal] = mapped_column(MONEY)  # signed, account currency
    currency: Mapped[str] = mapped_column(String(3))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    created_at: Mapped[datetime] = _created()


class Reconciliation(Base):
    """A paper-account check: positions_snapshots vs the replayed simulated executions, and
    the ledger vs the replayed P&L and charges. `drift` lists every difference found."""

    __tablename__ = "reconciliations"
    __table_args__ = (
        Index(
            "ix_trading_reconciliations_broker_account_id_created_at",
            "broker_account_id",
            "created_at",
        ),
        S,
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    broker_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading.broker_accounts.id", ondelete="CASCADE")
    )
    ok: Mapped[bool] = mapped_column(Boolean)
    drift: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=JSON_EMPTY)
    created_at: Mapped[datetime] = _created()
