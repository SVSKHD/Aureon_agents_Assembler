"""Trading API schemas. Decimals serialize as JSON strings ("2890.10"), never floats."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agents.trading.types import Broker, ExecutionMode, Session

NonNegDecimal = Annotated[Decimal, Field(ge=0, max_digits=24, decimal_places=8)]
# Every broker-facing value says where it came from; the UI shows this label.
ValueSource = Literal["broker", "simulated", "calculated"]


class _FromAttrs(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- accounts -----------------------------------------------------------------------------


class AccountCreate(BaseModel):
    broker: Broker
    account_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    label: str | None = Field(default=None, max_length=100)
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")
    owner_user_id: uuid.UUID | None = None  # default: the admin creating it


class AccountOut(_FromAttrs):
    id: uuid.UUID
    broker: str
    account_id: str
    label: str | None
    owner_user_id: uuid.UUID
    mode: ExecutionMode
    mode_changed_at: datetime | None
    base_currency: str
    is_active: bool
    created_at: datetime


class ModeChange(BaseModel):
    mode: ExecutionMode
    confirmation_token: str | None = Field(default=None, max_length=128)


class LiveToken(BaseModel):
    token: str
    expires_in: int


class RiskPolicyIn(BaseModel):
    max_daily_loss: NonNegDecimal
    max_exposure: NonNegDecimal
    max_position_size: NonNegDecimal
    max_drawdown_pct: Annotated[Decimal, Field(ge=0, le=100, decimal_places=3)]
    allowed_sessions: list[Session] = Field(min_length=1, max_length=3)
    kill_switch: bool = False

    @field_validator("allowed_sessions")
    @classmethod
    def _unique(cls, v: list[Session]) -> list[Session]:
        if len(set(v)) != len(v):
            raise ValueError("allowed_sessions must not repeat")
        return v


class RiskPolicyOut(_FromAttrs):
    id: uuid.UUID
    broker_account_id: uuid.UUID
    max_daily_loss: Decimal
    max_exposure: Decimal
    max_position_size: Decimal
    max_drawdown_pct: Decimal
    allowed_sessions: list[str]
    kill_switch: bool
    updated_at: datetime


# --- live broker reads (adapter DTOs) -----------------------------------------------------


class HealthOut(BaseModel):
    broker: str
    supported: bool
    ok: bool
    checked_at: datetime | None = None
    detail: str | None = None
    credentials_expire_at: datetime | None = None


class FundsOut(_FromAttrs):
    source: ValueSource = "broker"
    account_id: str
    currency: str
    available: Decimal
    used_margin: Decimal
    equity: Decimal | None
    as_of: datetime


class PositionOut(_FromAttrs):
    source: ValueSource = "broker"
    account_id: str
    symbol: str
    broker_instrument_id: str | None
    quantity: Decimal
    avg_price: Decimal
    currency: str
    last_price: Decimal | None
    unrealized_pnl: Decimal | None
    realized_pnl: Decimal | None
    product: str | None


class OrderOut(_FromAttrs):
    source: ValueSource = "broker"
    account_id: str
    order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    filled_quantity: Decimal
    status: str
    raw_status: str
    placed_at: datetime
    price: Decimal | None
    avg_fill_price: Decimal | None
    updated_at: datetime | None
    correlation_id: str | None


class QuoteOut(_FromAttrs):
    source: ValueSource = "broker"
    symbol: str
    ts: datetime
    last: Decimal | None
    bid: Decimal | None
    ask: Decimal | None
    volume: Decimal | None


class HoldingOut(_FromAttrs):
    source: ValueSource = "broker"
    account_id: str
    symbol: str
    isin: str | None
    quantity: Decimal
    available_quantity: Decimal
    avg_cost: Decimal
    last_price: Decimal | None


class TradeOut(_FromAttrs):
    source: ValueSource = "broker"
    account_id: str
    trade_id: str
    order_id: str
    symbol: str
    broker_instrument_id: str | None
    side: str
    quantity: Decimal
    price: Decimal
    executed_at: datetime


class OptionLegOut(_FromAttrs):
    broker_instrument_id: str | None
    last_price: Decimal | None
    oi: Decimal | None
    volume: Decimal | None
    implied_volatility: Decimal | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    previous_close: Decimal | None
    previous_oi: Decimal | None
    bid: Decimal | None
    ask: Decimal | None


class OptionStrikeOut(_FromAttrs):
    strike: Decimal
    call: OptionLegOut | None
    put: OptionLegOut | None


class OptionChainOut(_FromAttrs):
    source: ValueSource = "broker"
    underlying: str
    expiry: date
    underlying_price: Decimal | None
    as_of: datetime
    strikes: list[OptionStrikeOut]


# --- stored data --------------------------------------------------------------------------


class InstrumentOut(_FromAttrs):
    id: int
    broker: str
    broker_instrument_id: str
    symbol: str
    exchange: str | None
    segment: str | None
    instrument_type: str
    currency: str
    lot_size: Decimal
    tick_size: Decimal
    expiry: date | None
    strike: Decimal | None
    option_type: str | None
    underlying: str | None


class CandleOut(_FromAttrs):
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    oi: Decimal | None
    session: str
    source: str
    is_gap: bool


class StrategyVersionOut(_FromAttrs):
    id: uuid.UUID
    version: int
    code_ref: str
    params: dict[str, object]
    created_at: datetime


class StrategyOut(_FromAttrs):
    id: uuid.UUID
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    versions: list[StrategyVersionOut] = []


class SignalOut(_FromAttrs):
    id: uuid.UUID
    strategy_version_id: uuid.UUID
    instrument_id: int
    timeframe: str
    candle_time: datetime
    session: str
    side: str
    strength: Decimal | None
    inputs: dict[str, object]
    created_at: datetime


class ExecutionOut(_FromAttrs):
    id: uuid.UUID
    broker_account_id: uuid.UUID
    instrument_id: int
    order_intent_id: uuid.UUID | None
    broker_trade_id: str | None
    side: str
    quantity: Decimal
    price: Decimal
    fees: Decimal
    currency: str
    executed_at: datetime
    session: str
    is_simulated: bool
    source: ValueSource


class TradingAgentStatus(BaseModel):
    id: str
    name: str
    state: str
    paused: bool
    last_heartbeat_at: datetime | None
    details: dict[str, object]


# --- MT5 bot (Trading Phase C) ------------------------------------------------------------

PosDecimal = Annotated[Decimal, Field(gt=0, max_digits=24, decimal_places=8)]


class Mt5InstrumentIn(BaseModel):
    """Contract spec typed in by an admin from MT5's symbol Specification window.
    The bot's export does not carry it, so Aureon never guesses it."""

    lot_size: PosDecimal  # minimum volume step the strategy trades in (e.g. 0.01)
    tick_size: PosDecimal  # MT5 "point" / tick size (e.g. 0.01 for XAUUSD)
    currency: str = Field(pattern=r"^[A-Z]{3}$")  # profit currency
    instrument_type: str = Field(default="CFD", min_length=1, max_length=32)
    digits: int | None = Field(default=None, ge=0, le=10)
    contract_size: PosDecimal | None = None
    volume_min: PosDecimal | None = None
    volume_step: PosDecimal | None = None
    volume_max: PosDecimal | None = None


class Mt5StatusOut(BaseModel):
    """The bot as Aureon sees it right now (live read of its export)."""

    ok: bool
    detail: str | None
    source: Literal["file", "http"]
    generated_at: datetime
    age_s: int
    bot: dict[str, object]
    agents: dict[str, dict[str, object]]
    halt: dict[str, object] | None
    backtests: list[dict[str, object]]
    performance: dict[str, object]
    export_errors: dict[str, str]


# --- research agents (Trading Phase D) ----------------------------------------------------


class StrategyCreate(BaseModel):
    """A new strategy (version 1) or, for an existing name, its next version."""

    name: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._: -]+$")
    description: str | None = Field(default=None, max_length=2000)
    code_ref: str = Field(min_length=1, max_length=100)
    params: dict[str, object] = Field(default_factory=dict)


class BacktestCosts(BaseModel):
    spread_bps: NonNegDecimal = Decimal(0)
    slippage_bps: NonNegDecimal = Decimal(0)
    fee_bps: NonNegDecimal = Decimal(0)


class BacktestRequest(BaseModel):
    strategy_version_id: uuid.UUID
    instrument_id: int
    timeframe: Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
    start: datetime
    end: datetime
    # Overrides on top of the version's params; `grid` expands to one run per combination.
    params: dict[str, object] = Field(default_factory=dict)
    grid: dict[str, list[object]] = Field(default_factory=dict)
    costs: BacktestCosts = Field(default_factory=BacktestCosts)
    quantity: PosDecimal | None = None

    @field_validator("end")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        return v


class BacktestQueued(BaseModel):
    run_ids: list[uuid.UUID]


class BacktestRunOut(_FromAttrs):
    id: uuid.UUID
    strategy_version_id: uuid.UUID
    instrument_id: int
    timeframe: str
    period_start: datetime
    period_end: datetime
    params: dict[str, object]
    costs: dict[str, object]
    status: str
    metrics: dict[str, object]
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class BacktestTradeOut(_FromAttrs):
    side: str
    quantity: Decimal
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime | None
    exit_price: Decimal | None
    pnl: Decimal | None
    fees: Decimal
    session: str


class BacktestDetail(BaseModel):
    run: BacktestRunOut
    trades: list[BacktestTradeOut]
    trades_total: int
    source: ValueSource = "simulated"


class IndicatorOut(_FromAttrs):
    name: str
    ts: datetime
    values: dict[str, object]


class RiskDecisionOut(_FromAttrs):
    id: int
    broker_account_id: uuid.UUID
    signal_id: uuid.UUID | None
    approved: bool
    reason: str
    checks: list[dict[str, object]]
    inputs: dict[str, object]
    created_at: datetime


class AiAnalysisOut(_FromAttrs):
    id: uuid.UUID
    signal_id: uuid.UUID | None
    backtest_run_id: uuid.UUID | None
    model: str
    summary: str
    content: str
    created_at: datetime


class ExplainQueued(BaseModel):
    queued: bool = True
    position: int
