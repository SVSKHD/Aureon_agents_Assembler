"""Research agents against the DB: market data + gaps + freshness, indicators, signal dedupe,
risk rejections, backtests (API + worker), AI explanations (mocked Ollama), worker pipeline."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.ollama.client import OllamaClient
from app.agents.research import ai
from app.agents.trading.base import BrokerAdapter
from app.agents.trading.types import AdapterHealth, Candle, Timeframe, utc_now
from app.auth.bootstrap import bootstrap_admin
from app.core.config import Settings
from app.main import create_app
from app.models.brokers import DhanFundSnapshot
from app.models.trading import (
    AiAnalysis,
    BacktestRun,
    BacktestTrade,
    BrokerAccount,
    IndicatorValue,
    Instrument,
    MarketCandle,
    RiskDecision,
    RiskPolicy,
    Strategy,
    StrategyVersion,
    TradingSignal,
)
from app.services import research
from app.workers.research import MARKET_DATA, RISK, SIGNAL, TECHNICAL, ResearchWorker
from tests.conftest import TEST_REDIS_URL

pytestmark = pytest.mark.db
PASSWORD = "correct horse battery staple"
STEP = timedelta(minutes=5)
OLLAMA = "http://ollama.test:11434"


def aligned_now() -> datetime:
    now = datetime.now(UTC)
    return now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)


def series(closes: list[float], end: datetime) -> list[Candle]:
    """Candles whose last bar opens at `end - 5m` (i.e. it is closed by `end`)."""
    start = end - STEP * len(closes)
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(
            Candle(
                ts=start + STEP * i,
                open=Decimal(str(o)),
                high=Decimal(str(max(o, c) + 0.5)),
                low=Decimal(str(min(o, c) - 0.5)),
                close=Decimal(str(c)),
                volume=Decimal(100),
            )
        )
        prev = c
    return out


class FakeAdapter(BrokerAdapter):
    broker = "dhan"

    def __init__(self, rows: list[Candle], ok: bool = True) -> None:
        self.rows, self.ok, self.calls = rows, ok, 0

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            broker="dhan", ok=self.ok, checked_at=utc_now(), detail=None if self.ok else "expired"
        )

    async def candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        self.calls += 1
        return [c for c in self.rows if start <= c.ts < end]


class Env:
    def __init__(self, maker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.maker, self.settings = maker, settings
        self.inst: Instrument
        self.account: BrokerAccount
        self.version: StrategyVersion
        self.admin_id: Any

    async def count(self, model: Any, *where: Any) -> int:
        async with self.maker() as db:
            q = select(func.count()).select_from(model)
            for w in where:
                q = q.where(w)
            return int(await db.scalar(q) or 0)

    async def policy(self, **kw: Any) -> None:
        async with self.maker() as db, db.begin():
            db.add(
                RiskPolicy(
                    broker_account_id=self.account.id,
                    max_daily_loss=Decimal(1000),
                    max_exposure=Decimal(100000),
                    max_position_size=Decimal(10),
                    max_drawdown_pct=Decimal(10),
                    allowed_sessions=["Asia", "London", "NY"],
                    **kw,
                )
            )

    async def fund_snapshot(self, age: timedelta = timedelta(0)) -> None:
        async with self.maker() as db, db.begin():
            db.add(
                DhanFundSnapshot(
                    broker_account_id=self.account.id,
                    available_balance=Decimal(50000),
                    as_of=datetime.now(UTC) - age,
                )
            )

    def item(self) -> research.WatchItem:
        return research.WatchItem(self.version, "ema", self.inst, Timeframe.M5)


@pytest.fixture
async def env(db_clean: str, settings: Settings) -> AsyncIterator[Env]:
    engine = create_async_engine(db_clean)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def clean() -> None:
        async with engine.begin() as conn:
            for t in (
                "trading.backtest_runs",
                "trading.trading_signals",
                "trading.strategies",
                "trading.instruments",
            ):
                await conn.exec_driver_sql(f"DELETE FROM {t}")

    await clean()
    s = Settings.model_validate(
        {**settings.model_dump(), "DATABASE_URL": db_clean, "RESEARCH_AI_MODEL": "llama3:8b"}
    )
    e = Env(maker, s)
    admin = await bootstrap_admin(maker, "admin@spasta.online", PASSWORD)
    e.admin_id = admin.id
    async with maker() as db, db.begin():
        e.inst = Instrument(
            broker="dhan",
            broker_instrument_id="NSE_EQ:1333",
            symbol="HDFCBANK",
            instrument_type="EQUITY",
            currency="INR",
            lot_size=Decimal(1),
            tick_size=Decimal("0.05"),
        )
        e.account = BrokerAccount(
            broker="dhan", account_id="1100003626", base_currency="INR", owner_user_id=admin.id
        )
        strat = Strategy(name="ema-cross-test")
        db.add_all([e.inst, e.account, strat])
        await db.flush()
        e.version = StrategyVersion(
            strategy_id=strat.id,
            version=1,
            code_ref="ema_cross",
            params={"fast": 3, "slow": 8, "instrument_ids": [e.inst.id], "timeframes": ["5m"]},
        )
        db.add(e.version)
    yield e
    await clean()
    await engine.dispose()


CROSS_UP = [10.0] * 60 + [12.0]  # EMA3 crosses above EMA8 on the last bar → BUY


async def _load(env: Env, closes: list[float], end: datetime | None = None) -> FakeAdapter:
    adapter = FakeAdapter(series(closes, end or aligned_now()))
    out = await research.refresh_candles(env.maker, adapter, env.inst, Timeframe.M5, env.settings)
    assert out["supported"]
    return adapter


# --- market data --------------------------------------------------------------------------


async def test_refresh_upserts_marks_gaps_and_freshness(env: Env) -> None:
    # Yesterday 12:00 UTC: the 12 bars sit inside one UTC day, so the hole is a real gap.
    end = aligned_now().replace(hour=12, minute=0) - timedelta(days=1)
    full = series([10.0 + i for i in range(12)], end)
    rows = full[:5] + full[7:]  # bars 5 and 6 missing
    out = await research.refresh_candles(
        env.maker, FakeAdapter(rows), env.inst, Timeframe.M5, env.settings
    )
    assert (out["candles"], out["gaps"]) == (10, 2)
    async with env.maker() as db:
        gaps = (
            await db.scalars(
                select(MarketCandle).where(MarketCandle.is_gap.is_(True)).order_by(MarketCandle.ts)
            )
        ).all()
        loaded = await research.load_candles(db, env.inst.id, Timeframe.M5, limit=100)
        fresh = await research.freshness(db, env.inst.id, Timeframe.M5, env.settings, now=end)
        stale = await research.freshness(
            db, env.inst.id, Timeframe.M5, env.settings, now=end + timedelta(hours=1)
        )
    assert [g.ts for g in gaps] == [full[5].ts, full[6].ts]
    assert all(g.source == "gap" and g.volume == 0 for g in gaps)
    assert gaps[0].close == full[4].close  # carried forward, flagged
    assert len(loaded) == 10  # gap fillers are never fed to analysis
    assert not fresh.stale and stale.stale

    # The missing bars arrive later: real candles replace the fillers.
    await research.refresh_candles(
        env.maker, FakeAdapter(full), env.inst, Timeframe.M5, env.settings
    )
    assert await env.count(MarketCandle, MarketCandle.is_gap.is_(True)) == 0
    assert await env.count(MarketCandle) == 12


async def test_unsupported_adapter_is_reported_not_invented(env: Env) -> None:
    class NoCandles(FakeAdapter):
        async def candles(self, *a: Any) -> list[Candle]:
            raise research.NotSupported("mt5", "candles")

    out = await research.refresh_candles(
        env.maker, NoCandles([]), env.inst, Timeframe.M5, env.settings
    )
    assert out == {"supported": False, "detail": "mt5: candles is not supported"}
    out = await research.refresh_candles(env.maker, None, env.inst, Timeframe.M5, env.settings)
    assert not out["supported"] and await env.count(MarketCandle) == 0


# --- indicators + signals -----------------------------------------------------------------


async def test_indicators_persisted_and_signal_dedupe(env: Env) -> None:
    await _load(env, CROSS_UP)
    out = await research.compute_indicators(env.maker, env.inst, Timeframe.M5, env.settings, {3, 8})
    assert out["bars"] == 61 and out["rows"] > 0
    async with env.maker() as db:
        names = set((await db.scalars(select(IndicatorValue.name).distinct())).all())
        e3 = await db.scalar(
            select(IndicatorValue)
            .where(IndicatorValue.name == "ema:3")
            .order_by(IndicatorValue.ts.desc())
            .limit(1)
        )
    assert {"ema:3", "ema:8", "ema:9", "ema:21", "vwap:Asia/Kolkata", "atr:14"} <= names
    # EMA3 of ...10,10,12: 10 + (12-10)/2 = 11
    assert e3 and e3.values == {"value": "11"}
    # Recomputing is an upsert, never duplicates.
    await research.compute_indicators(env.maker, env.inst, Timeframe.M5, env.settings, {3, 8})
    assert await env.count(IndicatorValue) == out["rows"]

    first = await research.evaluate_signal(env.maker, env.item(), env.settings)
    second = await research.evaluate_signal(env.maker, env.item(), env.settings)
    assert first["side"] == "BUY" and first["signal_id"] is not None
    assert second["signal_id"] is None  # same (version, instrument, timeframe, candle_time)
    async with env.maker() as db:
        sig = await db.scalar(select(TradingSignal))
    assert sig and sig.inputs["code_ref"] == "ema_cross" and sig.inputs["version"] == 1
    assert Decimal(sig.inputs["ema_fast"]) > Decimal(sig.inputs["ema_slow"])
    assert await env.count(TradingSignal) == 1


# --- risk ---------------------------------------------------------------------------------


async def _signal(env: Env) -> Any:
    await _load(env, CROSS_UP)
    out = await research.evaluate_signal(env.maker, env.item(), env.settings)
    assert out["side"] == "BUY"
    return out["signal_id"]


async def _decide(env: Env, sid: Any, adapter: BrokerAdapter | None = None) -> RiskDecision:
    adapters = {"dhan": adapter or FakeAdapter([])}
    res = await research.assess_signal(env.maker, sid, adapters, env.settings)
    assert len(res) == 1
    async with env.maker() as db:
        row = await db.scalar(select(RiskDecision).order_by(RiskDecision.id.desc()).limit(1))
    assert row is not None
    return row


def failed(d: RiskDecision) -> set[str]:
    return {c["name"] for c in d.checks if c["ok"] is False}


async def test_risk_approves_when_everything_is_fresh(env: Env) -> None:
    sid = await _signal(env)
    await env.policy()
    await env.fund_snapshot()
    d = await _decide(env, sid)
    assert d.approved and d.reason == "all checks passed", d.checks
    assert d.policy_snapshot["max_exposure"] == "100000"
    assert d.inputs["proposed_quantity"] == "1" and d.inputs["price"] == "12"
    drawdown = next(c for c in d.checks if c["name"] == "drawdown")
    assert drawdown["ok"] is None  # reported as not evaluated, never silently passed


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("no_policy", {"policy_present"}),
        ("kill_switch", {"kill_switch_off"}),
        ("stale_account", {"account_state_fresh"}),
        ("bad_credentials", {"credentials_ok"}),
        ("small_limits", {"position_size", "exposure"}),
        ("no_session", {"session_allowed"}),
    ],
)
async def test_risk_rejections(env: Env, setup: str, expected: set[str]) -> None:
    sid = await _signal(env)
    adapter: BrokerAdapter | None = None
    if setup != "no_policy":
        kw: dict[str, Any] = {}
        if setup == "kill_switch":
            kw["kill_switch"] = True
        await env.policy(**kw)
    if setup == "small_limits":
        async with env.maker() as db, db.begin():
            p = await db.scalar(select(RiskPolicy))
            assert p
            p.max_position_size = Decimal("0.5")
            p.max_exposure = Decimal(5)
    if setup == "no_session":
        async with env.maker() as db, db.begin():
            p = await db.scalar(select(RiskPolicy))
            assert p
            p.allowed_sessions = []
    await env.fund_snapshot(age=timedelta(hours=2) if setup == "stale_account" else timedelta(0))
    if setup == "bad_credentials":
        adapter = FakeAdapter([], ok=False)
    d = await _decide(env, sid, adapter)
    assert not d.approved and failed(d) == expected, d.checks
    assert d.reason.startswith("rejected: ")


async def test_risk_rejects_stale_data_and_old_signal(env: Env) -> None:
    sid = await _signal(env)
    await env.policy()
    await env.fund_snapshot()
    later = datetime.now(UTC) + timedelta(hours=3)
    await research.assess_signal(env.maker, sid, {"dhan": FakeAdapter([])}, env.settings, now=later)
    async with env.maker() as db:
        d = await db.scalar(select(RiskDecision))
    assert d and not d.approved
    assert {"data_fresh", "signal_fresh"} <= failed(d)


async def test_no_trade_signals_get_no_risk_decision(env: Env) -> None:
    await _load(env, [10.0] * 61)
    out = await research.evaluate_signal(env.maker, env.item(), env.settings)
    assert out["side"] == "NO_TRADE"
    assert await research.assess_signal(env.maker, out["signal_id"], {}, env.settings) == []


# --- worker pipeline ------------------------------------------------------------------------


async def test_worker_pipeline_one_pass(env: Env) -> None:
    await env.policy()
    await env.fund_snapshot()
    adapter = FakeAdapter(series(CROSS_UP, aligned_now()))
    w = ResearchWorker(env.settings, env.maker, {"dhan": adapter})
    assert await w.tick() == 1
    for spec in (MARKET_DATA, TECHNICAL, SIGNAL, RISK):
        lane = w.lane(spec)
        assert lane.queue.qsize() == 1, spec.id
        await w.handlers()[spec.id](lane.queue.get_nowait())
    assert adapter.calls == 1
    assert await env.count(TradingSignal) == 1 and await env.count(RiskDecision) == 1
    async with env.maker() as db:
        d = await db.scalar(select(RiskDecision))
    assert d and d.approved


async def test_bounded_queue_drops_and_counts(env: Env) -> None:
    s = Settings.model_validate({**env.settings.model_dump(), "RESEARCH_QUEUE_MAX": 1})
    w = ResearchWorker(s, env.maker, {})
    lane = w.lane(SIGNAL)
    assert lane.offer(1) and not lane.offer(2)
    assert lane.dropped == 1 and lane.state()[1]["dropped"] == 1


# --- backtests + AI via the API -----------------------------------------------------------


@respx.mock
async def test_backtest_and_explanations_via_api(env: Env) -> None:
    end = aligned_now()
    closes = [100 + (i % 20) - (i % 7) * 0.5 for i in range(300)]
    await _load(env, closes, end)
    redis = Redis.from_url(TEST_REDIS_URL)
    await redis.delete(ai.QUEUE_KEY)
    app: FastAPI = create_app(env.settings)
    async with (
        LifespanManager(app) as mgr,
        AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c,
    ):
        csrf = (
            await c.post(
                "/api/auth/login", json={"email": "admin@spasta.online", "password": PASSWORD}
            )
        ).json()["csrf_token"]
        h = {"x-csrf-token": csrf}
        bad = await c.post(
            "/api/trading/strategies",
            headers=h,
            json={"name": "x", "code_ref": "secret_sauce", "params": {}},
        )
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "unknown_strategy"
        v2 = await c.post(
            "/api/trading/strategies",
            headers=h,
            json={
                "name": "ema-cross-test",
                "code_ref": "ema_cross",
                "params": {"fast": 5, "slow": 13},
            },
        )
        assert v2.status_code == 201 and v2.json()["version"] == 2
        badp = await c.post(
            "/api/trading/strategies",
            headers=h,
            json={"name": "y", "code_ref": "ema_cross", "params": {"fast": 30, "slow": 10}},
        )
        assert badp.status_code == 422

        body = {
            "strategy_version_id": str(env.version.id),
            "instrument_id": env.inst.id,
            "timeframe": "5m",
            "start": (end - STEP * 400).isoformat(),
            "end": end.isoformat(),
            "grid": {"fast": [3, 5], "slow": [8, 13]},
            "costs": {"spread_bps": "2", "slippage_bps": "1", "fee_bps": "3"},
        }
        r = await c.post("/api/trading/strategies/backtest", headers=h, json=body)
        assert r.status_code == 202, r.text
        ids = r.json()["run_ids"]
        assert len(ids) == 4
        too_big = await c.post(
            "/api/trading/strategies/backtest",
            headers=h,
            json={**body, "grid": {"fast": list(range(1, 9)), "slow": list(range(20, 30))}},
        )
        assert too_big.status_code == 422

        # The worker claims and runs them.
        while (run_id := await research.claim_backtest(env.maker)) is not None:
            await research.run_backtest(env.maker, run_id, env.settings)
        detail = (await c.get(f"/api/trading/backtests/{ids[0]}")).json()
        run = detail["run"]
        assert run["status"] == "done" and detail["source"] == "simulated"
        m = run["metrics"]
        assert m["bars"] == 300 and m["fill"] == "next bar open"
        assert {"win_rate", "expectancy", "profit_factor", "max_drawdown", "fees"} <= set(m)
        assert m["trades"] == detail["trades_total"] > 0
        assert isinstance(detail["trades"][0]["pnl"], str)  # Decimal over JSON

        ind = await c.get(
            "/api/trading/indicators",
            params={"instrument_id": env.inst.id, "timeframe": "5m"},
        )
        assert ind.status_code == 200

        # AI explanation: queued, then the agent writes ai_analyses (Ollama mocked).
        q = await c.post(f"/api/trading/backtests/{ids[0]}/explain", headers=h)
        assert q.status_code == 202 and q.json()["position"] == 1
        chunks = [
            {"message": {"content": "Mixed results with modest edge.\n"}, "done": False},
            {"message": {"content": "Costs matter; no news was considered."}, "done": True},
        ]
        respx.post(f"{OLLAMA}/api/chat").mock(
            return_value=httpx.Response(
                200, content="\n".join(json.dumps(x) for x in chunks).encode()
            )
        )
        got = await redis.brpop([ai.QUEUE_KEY], timeout=1)
        assert got
        req = json.loads(got[1])
        ollama = OllamaClient(OLLAMA, connect_timeout=1, read_timeout=5, cache_ttl=0)
        aid = await ai.explain(env.maker, ollama, "llama3:8b", req["kind"], req["id"], limit_s=10)
        await ollama.aclose()
        assert aid is not None
        sent = json.loads(respx.calls.last.request.content)
        assert sent["model"] == "llama3:8b" and "never invent" in sent["messages"][0]["content"]
        lst = (await c.get("/api/trading/ai-analyses", params={"backtest_run_id": ids[0]})).json()
        assert lst[0]["summary"] == "Mixed results with modest edge."

        # Bounded queue: 429 when full.
        s1 = Settings.model_validate({**env.settings.model_dump(), "RESEARCH_AI_QUEUE_MAX": 1})
        await ai.enqueue(redis, "backtest", ids[1], s1.RESEARCH_AI_QUEUE_MAX)
        with pytest.raises(ai.QueueFull):
            await ai.enqueue(redis, "backtest", ids[2], s1.RESEARCH_AI_QUEUE_MAX)
        await redis.delete(ai.QUEUE_KEY)

    # Nothing in Phase D creates or approves an order intent.
    from app.models.trading import OrderIntent

    assert await env.count(OrderIntent) == 0
    assert await env.count(AiAnalysis) == 1
    assert await env.count(BacktestRun, BacktestRun.status == "done") == 4
    assert await env.count(BacktestTrade) > 0
    await redis.aclose()


async def test_explain_needs_a_model(env: Env, settings: Settings) -> None:
    s = Settings.model_validate({**env.settings.model_dump(), "RESEARCH_AI_MODEL": None})
    app: FastAPI = create_app(s)
    async with (
        LifespanManager(app) as mgr,
        AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c,
    ):
        csrf = (
            await c.post(
                "/api/auth/login", json={"email": "admin@spasta.online", "password": PASSWORD}
            )
        ).json()["csrf_token"]
        sid = await _signal(env)
        r = await c.post(f"/api/trading/signals/{sid}/explain", headers={"x-csrf-token": csrf})
        assert r.status_code == 503 and r.json()["detail"]["code"] == "ai_not_configured"
