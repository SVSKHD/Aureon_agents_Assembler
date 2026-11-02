"""Round-trips through every trading/dhan/mt5 table, plus the constraints that matter."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.security import hash_password
from app.models.auth import User
from app.models.brokers import (
    DhanAccountProfile,
    DhanFundSnapshot,
    DhanInstrument,
    Mt5AccountSnapshot,
    Mt5Symbol,
    Mt5Terminal,
)
from app.models.trading import (
    AiAnalysis,
    BacktestRun,
    BacktestTrade,
    BrokerAccount,
    BrokerOrderRow,
    Execution,
    Instrument,
    MarketCandle,
    OrderEvent,
    OrderIntent,
    PositionSnapshot,
    RiskDecision,
    RiskPolicy,
    Strategy,
    StrategyVersion,
    TradingSignal,
)

pytestmark = pytest.mark.db
T = datetime(2026, 11, 2, 9, 15, tzinfo=UTC)


@pytest.fixture
async def maker(db_clean: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_clean)
    async with engine.begin() as conn:
        for t in (
            "trading.order_events",
            "trading.executions",
            "trading.risk_decisions",
            "trading.broker_orders",
            "trading.order_intents",
            "trading.ai_analyses",
            "trading.backtest_trades",
            "trading.backtest_runs",
            "trading.trading_signals",
            "trading.strategy_versions",
            "trading.strategies",
            "trading.market_candles",
            "trading.positions_snapshots",
            "trading.risk_policies",
            "dhan.instruments",
            "dhan.account_profiles",
            "dhan.fund_snapshots",
            "mt5.terminals",
            "mt5.symbols",
            "mt5.account_snapshots",
            "trading.instruments",
            "trading.broker_accounts",
        ):
            await conn.exec_driver_sql(f"DELETE FROM {t}")
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _base(m: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID, int]:
    async with m() as db, db.begin():
        user = User(email="t@spasta.online", password_hash=hash_password("x" * 12), role_id=2)
        db.add(user)
        await db.flush()
        acct = BrokerAccount(
            broker="dhan", account_id="1100012345", base_currency="INR", owner_user_id=user.id
        )
        inst = Instrument(
            broker="dhan",
            broker_instrument_id="2885",
            symbol="RELIANCE",
            instrument_type="EQUITY",
            currency="INR",
            lot_size=Decimal("1"),
            tick_size=Decimal("0.05"),
            exchange="NSE",
            segment="NSE_EQ",
        )
        db.add_all([acct, inst])
        await db.flush()
        return user.id, acct.id, inst.id


async def test_every_table_round_trips_exact_decimals(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    user_id, acct_id, inst_id = await _base(maker)
    tiny, big = Decimal("0.00000001"), Decimal("9999999999999999.12345678")
    async with maker() as db, db.begin():
        strat = Strategy(name="ema-cross", owner_user_id=user_id)
        db.add(strat)
        await db.flush()
        sv = StrategyVersion(
            strategy_id=strat.id, version=1, code_ref="ema_cross", params={"fast": 9, "slow": 21}
        )
        db.add(sv)
        await db.flush()
        sig = TradingSignal(
            strategy_version_id=sv.id,
            instrument_id=inst_id,
            timeframe="15m",
            candle_time=T,
            session="London",
            side="BUY",
            strength=Decimal("0.7500"),
            inputs={"ema9": "2891.2"},
        )
        candle = MarketCandle(
            instrument_id=inst_id,
            timeframe="15m",
            ts=T,
            open=tiny,
            high=big,
            low=tiny,
            close=Decimal("2890.05"),
            volume=Decimal("123456.789012"),
            session="London",
        )
        policy = RiskPolicy(
            broker_account_id=acct_id,
            max_daily_loss=Decimal("5000.0000"),
            max_exposure=Decimal("250000"),
            max_position_size=Decimal("75"),
            max_drawdown_pct=Decimal("12.500"),
            allowed_sessions=["London", "NY"],
        )
        db.add_all([sig, candle, policy])
        await db.flush()
        intent = OrderIntent(
            broker_account_id=acct_id,
            instrument_id=inst_id,
            signal_id=sig.id,
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("10"),
            limit_price=Decimal("2890.05"),
            mode="OBSERVE",
            source="strategy",
            correlation_id="corr-abc",
        )
        db.add(intent)
        await db.flush()
        border = BrokerOrderRow(
            broker_account_id=acct_id,
            order_intent_id=intent.id,
            broker_order_id="DH-1",
            symbol="RELIANCE",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("10"),
            raw_status="PENDING",
            normalized_status="OPEN",
            placed_at=T,
            raw={"orderStatus": "X"},
        )
        db.add(border)
        await db.flush()
        run = BacktestRun(
            strategy_version_id=sv.id,
            instrument_id=inst_id,
            timeframe="15m",
            period_start=T - timedelta(days=30),
            period_end=T,
            metrics={"profit_factor": "1.42"},
        )
        db.add(run)
        await db.flush()
        db.add_all(
            [
                OrderEvent(
                    order_intent_id=intent.id,
                    broker_order_row_id=border.id,
                    event_type="submitted",
                    correlation_id="corr-abc",
                    occurred_at=T,
                ),
                Execution(
                    broker_account_id=acct_id,
                    instrument_id=inst_id,
                    order_intent_id=intent.id,
                    broker_trade_id="TR-1",
                    side="BUY",
                    quantity=Decimal("10"),
                    price=Decimal("2890.05"),
                    fees=Decimal("20.3456"),
                    currency="INR",
                    executed_at=T,
                    session="London",
                ),
                PositionSnapshot(
                    broker_account_id=acct_id,
                    instrument_id=inst_id,
                    symbol="RELIANCE",
                    quantity=Decimal("-10"),
                    avg_price=Decimal("2890.05"),
                    currency="INR",
                    source="broker",
                    as_of=T,
                ),
                RiskDecision(
                    broker_account_id=acct_id,
                    order_intent_id=intent.id,
                    policy_id=policy.id,
                    approved=False,
                    reason="outside allowed sessions",
                    policy_snapshot={"allowed_sessions": ["London"]},
                    inputs={"session": "Asia"},
                    checks=[{"check": "session", "ok": False}],
                ),
                BacktestTrade(
                    backtest_run_id=run.id,
                    side="SELL",
                    quantity=Decimal("1"),
                    entry_time=T,
                    entry_price=Decimal("1.1"),
                    pnl=Decimal("-0.0001"),
                    session="NY",
                ),
                AiAnalysis(signal_id=sig.id, model="qwen2.5:7b", summary="s", content="c"),
                DhanInstrument(
                    exchange_segment="NSE_EQ",
                    security_id="2885",
                    trading_symbol="RELIANCE",
                    lot_size=Decimal("1"),
                    tick_size=Decimal("0.05"),
                    trading_instrument_id=inst_id,
                    expiry_date=date(2026, 11, 27),
                    raw={"SEM_SMST_SECURITY_ID": "2885"},
                ),
                DhanAccountProfile(
                    broker_account_id=acct_id,
                    dhan_client_id="1100012345",
                    active_segments=["NSE_EQ"],
                ),
                DhanFundSnapshot(
                    broker_account_id=acct_id, available_balance=Decimal("1.2345"), as_of=T
                ),
                Mt5Terminal(broker_account_id=acct_id, login="5012345", server="Broker-Demo"),
                Mt5Symbol(
                    server="Broker-Demo",
                    symbol="XAUUSD",
                    digits=2,
                    point=Decimal("0.01"),
                    contract_size=Decimal("100"),
                    volume_step=Decimal("0.01"),
                ),
                Mt5AccountSnapshot(
                    broker_account_id=acct_id, equity=Decimal("10000.0001"), as_of=T
                ),
            ]
        )

    async with maker() as db:
        c = await db.scalar(select(MarketCandle))
        assert c is not None and (c.open, c.high, c.volume) == (tiny, big, Decimal("123456.789012"))
        assert isinstance(c.close, Decimal)
        p = await db.scalar(select(RiskPolicy))
        assert p is not None and p.allowed_sessions == ["London", "NY"]
        assert p.max_drawdown_pct == Decimal("12.500")
        e = await db.scalar(select(Execution))
        assert e is not None and e.fees == Decimal("20.3456") and e.is_simulated is False
        pos = await db.scalar(select(PositionSnapshot))
        assert pos is not None and pos.quantity == Decimal("-10")
        d = await db.scalar(select(RiskDecision))
        assert d is not None and d.checks == [{"check": "session", "ok": False}]
        bt = await db.scalar(select(BacktestTrade))
        assert bt is not None and bt.pnl == Decimal("-0.0001")
        sym = await db.scalar(select(Mt5Symbol))
        assert sym is not None and sym.point == Decimal("0.01")
        acct = await db.get(BrokerAccount, acct_id)
        assert acct is not None and acct.mode == "OBSERVE"  # default


async def _violates(maker: async_sessionmaker[AsyncSession], *rows: object) -> None:
    with pytest.raises(IntegrityError):
        async with maker() as db, db.begin():
            db.add_all(rows)


async def test_constraints(maker: async_sessionmaker[AsyncSession]) -> None:
    user_id, acct_id, inst_id = await _base(maker)
    # Broker identifiers are unique per broker.
    await _violates(
        maker,
        BrokerAccount(
            broker="dhan", account_id="1100012345", base_currency="INR", owner_user_id=user_id
        ),
    )
    await _violates(
        maker,
        BrokerAccount(broker="binance", account_id="x", base_currency="USD", owner_user_id=user_id),
    )
    await _violates(
        maker,
        BrokerAccount(
            broker="mt5", account_id="y", base_currency="USD", owner_user_id=user_id, mode="YOLO"
        ),
    )
    await _violates(
        maker,
        MarketCandle(
            instrument_id=inst_id,
            timeframe="15m",
            ts=T,
            open=Decimal(2),
            high=Decimal(1),
            low=Decimal(3),
            close=Decimal(2),
            session="London",
        ),
    )
    await _violates(
        maker,
        MarketCandle(
            instrument_id=inst_id,
            timeframe="2m",
            ts=T,
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
            session="London",
        ),
    )
    await _violates(
        maker,
        MarketCandle(
            instrument_id=inst_id,
            timeframe="1m",
            ts=T,
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
            session="Sydney",
        ),
    )
    # Order intents: positive quantity, valid source (AI is not a source).
    for bad in ({"quantity": Decimal("0")}, {"source": "ai"}, {"mode": "LIVE!"}):
        await _violates(
            maker,
            OrderIntent(
                **{
                    "broker_account_id": acct_id,
                    "instrument_id": inst_id,
                    "side": "BUY",
                    "order_type": "MARKET",
                    "quantity": Decimal("1"),
                    "mode": "OBSERVE",
                    "source": "manual",
                    "correlation_id": f"c-{uuid.uuid4()}",
                    **bad,
                }
            ),
        )
    await _violates(
        maker,
        RiskPolicy(
            broker_account_id=acct_id,
            max_daily_loss=Decimal("-1"),
            max_exposure=Decimal(0),
            max_position_size=Decimal(0),
            max_drawdown_pct=Decimal(10),
        ),
    )
    await _violates(maker, OrderEvent(event_type="orphan", occurred_at=T))
    await _violates(maker, AiAnalysis(model="m", summary="s", content="c"))  # no subject

    # Signal dedupe on (strategy_version, instrument, timeframe, candle_time).
    async with maker() as db, db.begin():
        s = Strategy(name="dedupe")
        db.add(s)
        await db.flush()
        sv = StrategyVersion(strategy_id=s.id, version=1, code_ref="x")
        db.add(sv)
        await db.flush()
        sv_id = sv.id
        db.add(
            TradingSignal(
                strategy_version_id=sv_id,
                instrument_id=inst_id,
                timeframe="15m",
                candle_time=T,
                session="London",
                side="BUY",
            )
        )
    await _violates(
        maker,
        TradingSignal(
            strategy_version_id=sv_id,
            instrument_id=inst_id,
            timeframe="15m",
            candle_time=T,
            session="London",
            side="SELL",
        ),
    )
    # Broker trade ids are unique per account (NULL allowed many times, e.g. PAPER fills).
    exec_kw = {
        "broker_account_id": acct_id,
        "instrument_id": inst_id,
        "side": "BUY",
        "quantity": Decimal(1),
        "price": Decimal(1),
        "currency": "INR",
        "executed_at": T,
        "session": "London",
    }
    async with maker() as db, db.begin():
        db.add_all(
            [
                Execution(**exec_kw, broker_trade_id="T1"),
                Execution(**exec_kw, is_simulated=True),
                Execution(**exec_kw, is_simulated=True),
            ]
        )
    await _violates(maker, Execution(**exec_kw, broker_trade_id="T1"))
