"""MT5 bot export → DB sync, stale detection and API (fixture exports; DB, no MT5)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.registry import effective_state
from app.agents.trading.mt5.adapter import MT5Adapter
from app.agents.trading.mt5.status import StatusInvalid, StatusSource
from app.auth.bootstrap import bootstrap_admin
from app.core.config import Settings
from app.main import create_app
from app.models.agents import Agent, AgentLog
from app.models.brokers import Mt5Symbol, Mt5Terminal
from app.models.trading import (
    BrokerAccount,
    Instrument,
    PositionSnapshot,
    Strategy,
    TradingSignal,
)
from app.services import mt5_sync
from app.workers.scheduler import Scheduler

pytestmark = pytest.mark.db
FIX = Path(__file__).parent / "fixtures" / "mt5" / "status.json"
LOGIN = "51234567"
PASSWORD = "correct horse battery staple"


def doc(**changes: Any) -> dict[str, Any]:
    d: dict[str, Any] = json.loads(FIX.read_text())
    d["generated_at"] = datetime.now(UTC).isoformat()
    d.update(changes)
    return d


class Env:
    def __init__(
        self, maker: async_sessionmaker[AsyncSession], path: Path, settings: Settings
    ) -> None:
        self.maker, self.path, self.settings = maker, path, settings
        self.adapter = MT5Adapter(
            StatusSource(path=str(path)), account_id=LOGIN, currency="USD", stale_after=90
        )

    def write(self, d: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(d))

    async def count(self, model: Any) -> int:
        async with self.maker() as db:
            return int(await db.scalar(select(func.count()).select_from(model)) or 0)

    async def sync(self) -> dict[str, Any]:
        return await mt5_sync.sync_status(self.maker, self.adapter, self.settings)


@pytest.fixture
async def env(db_clean: str, settings: Settings, tmp_path: Path) -> AsyncIterator[Env]:
    engine = create_async_engine(db_clean)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def clean() -> None:
        async with engine.begin() as conn:
            for t in (
                "trading.trading_signals",
                "trading.strategy_versions",
                "trading.strategies",
                "mt5.symbols",
                "trading.instruments",
            ):
                await conn.exec_driver_sql(f"DELETE FROM {t}")

    await clean()
    s = Settings.model_validate(
        {
            **settings.model_dump(),
            "MT5_STATUS_FILE": str(tmp_path / "status.json"),
            "MT5_ACCOUNT_ID": LOGIN,
            "MT5_SERVER": "Broker-Demo",
            "DATABASE_URL": db_clean,
        }
    )
    e = Env(maker, tmp_path / "status.json", s)
    e.write(doc())
    yield e
    await e.adapter.aclose()
    await clean()  # later tests expect empty trading tables
    await engine.dispose()


async def _instrument(env: Env, sym: str = "XAUUSD") -> int:
    async with env.maker() as db, db.begin():
        inst = Instrument(
            broker="mt5",
            broker_instrument_id=sym,
            symbol=sym,
            instrument_type="CFD",
            currency="USD",
            lot_size=Decimal("0.01"),
            tick_size=Decimal("0.01"),
        )
        db.add(inst)
    return inst.id


async def _account(env: Env) -> BrokerAccount:
    admin = await bootstrap_admin(env.maker, "admin@spasta.online", PASSWORD)
    async with env.maker() as db, db.begin():
        acct = BrokerAccount(
            broker="mt5", account_id=LOGIN, base_currency="USD", owner_user_id=admin.id
        )
        db.add(acct)
    return acct


async def test_sync_heartbeat_logs_and_signals_idempotent(env: Env) -> None:
    await _instrument(env)
    first = await env.sync()
    assert first["logs_inserted"] == 7
    assert first["signals"]["inserted"] == 1
    assert first["signals"]["unknown_instrument"] == ["EURUSD"]  # never guessed
    assert first["positions"] is None  # no registered account yet

    second = await env.sync()  # same export: nothing new
    assert second["logs_inserted"] == 0 and second["signals"]["inserted"] == 0
    assert await env.count(AgentLog) == 7 and await env.count(TradingSignal) == 1

    async with env.maker() as db:
        agent = await db.get(Agent, "mt5-bot")
        sig = await db.scalar(select(TradingSignal))
        strat = await db.scalar(select(Strategy))
        levels = dict(
            (await db.execute(select(AgentLog.level, func.count()).group_by(AgentLog.level))).all()
        )
    assert agent and agent.kind == "trading" and agent.state == "idle"
    assert agent.version == "3.0.0" and agent.details["stale_after_s"] == 90
    assert agent.details["agents"]["XAUUSD"]["positions"] == 1
    assert sig and strat and strat.name == "mt5:ema5080"
    # bar 1793620500 is server time (UTC+2) → 2 h earlier in UTC
    assert sig.candle_time == datetime.fromtimestamp(1793620500 - 7200, UTC)
    assert sig.side == "BUY" and sig.timeframe == "5m" and sig.session == "London"
    assert sig.inputs["price"] == "2650.1" and sig.inputs["bot_version"] == "3.0.0"
    assert levels == {"info": 6, "error": 1}


async def test_new_events_append_only(env: Env) -> None:
    await env.sync()
    d = doc()
    d["events"] = [
        *d["events"][-2:],
        {"t": 1793613000, "event": "exit", "symbol": "XAUUSD", "points": 3.0, "reason": "cross"},
    ]
    env.write(d)
    out = await env.sync()
    assert out["logs_inserted"] == 1 and await env.count(AgentLog) == 8


async def test_positions_and_terminal_for_registered_account(env: Env) -> None:
    acct = await _account(env)
    inst = await _instrument(env)
    out = await env.sync()
    assert out["positions"] == 2 and out["account"] == "registered"
    async with env.maker() as db:
        rows = {r.symbol: r for r in (await db.scalars(select(PositionSnapshot))).all()}
        term = await db.get(Mt5Terminal, acct.id)
    assert rows["XAUUSD"].quantity == Decimal("0.1") and rows["XAUUSD"].instrument_id == inst
    assert rows["XAGUSD"].quantity == Decimal("-0.5") and rows["XAGUSD"].instrument_id is None
    assert rows["XAUUSD"].source == "broker" and rows["XAUUSD"].unrealized_pnl is None
    assert term and term.login == LOGIN and term.server == "Broker-Demo"


async def test_stale_export_goes_stale_and_writes_no_positions(env: Env) -> None:
    await _account(env)
    old = datetime.now(UTC) - timedelta(minutes=10)
    env.write(doc(generated_at=old.isoformat()))
    out = await env.sync()
    assert out["positions"] == 0
    assert await env.count(PositionSnapshot) == 0
    async with env.maker() as db:
        agent = await db.get(Agent, "mt5-bot")
    assert agent and agent.last_heartbeat_at == old and agent.state == "error"
    # Its own 90 s window, not the global AGENT_STALE_SECONDS (here 1 h).
    assert effective_state(agent, 3600) == "stale"


async def test_invalid_export_writes_nothing(env: Env) -> None:
    env.path.write_text(json.dumps({**doc(), "schema": "other"}))
    with pytest.raises(StatusInvalid):
        await env.sync()
    assert await env.count(Agent) == 0 and await env.count(AgentLog) == 0


async def test_paused_flag_is_kept(env: Env) -> None:
    await env.sync()
    async with env.maker() as db, db.begin():
        agent = await db.get(Agent, "mt5-bot")
        assert agent
        agent.paused = True
    await env.sync()
    async with env.maker() as db:
        agent = await db.get(Agent, "mt5-bot")
    assert agent and agent.paused


async def test_scheduler_task_runs(env: Env) -> None:
    sched = Scheduler(env.settings, env.maker, tasks=None, mt5=env.adapter)
    out = await sched.tasks["mt5_sync_status"](sched.ctx)
    assert out["signals"]["seen"] == 2


async def test_api_status_and_instrument_registration(env: Env) -> None:
    await bootstrap_admin(env.maker, "admin@spasta.online", PASSWORD)
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
        st = (await c.get("/api/trading/mt5/status")).json()
        assert st["ok"] and st["source"] == "file"
        assert st["agents"]["XAUUSD"]["positions"][0]["price_open"] == "2650.55"
        assert st["performance"]["source"] == "calculated"
        assert st["backtests"][0]["summary"]["win_rate"] == "0.6"

        r = await c.put(
            "/api/trading/mt5/instruments/xauusd",
            json={"lot_size": "0.01", "tick_size": "0.01", "currency": "USD", "digits": 2},
        )
        assert r.status_code == 403  # CSRF
        r = await c.put(
            "/api/trading/mt5/instruments/xauusd",
            headers={"x-csrf-token": csrf},
            json={"lot_size": "0.01", "tick_size": "0.01", "currency": "USD", "digits": 2},
        )
        assert r.status_code == 200 and r.json()["broker_instrument_id"] == "XAUUSD"
        bad = await c.put(
            "/api/trading/mt5/instruments/XAUUSD",
            headers={"x-csrf-token": csrf},
            json={"lot_size": "0", "tick_size": "0.01", "currency": "USD"},
        )
        assert bad.status_code == 422

        acct = (
            await c.post(
                "/api/trading/accounts",
                headers={"x-csrf-token": csrf},
                json={"broker": "mt5", "account_id": LOGIN, "base_currency": "USD"},
            )
        ).json()["id"]
        h = (await c.get(f"/api/trading/accounts/{acct}/health")).json()
        assert h["supported"] and h["ok"]
        pos = (await c.get(f"/api/trading/accounts/{acct}/positions")).json()
        assert pos["total"] == 2
        f = await c.get(f"/api/trading/accounts/{acct}/funds")
        assert f.status_code == 501 and f.json()["detail"]["code"] == "not_supported"

    async with env.maker() as db:
        sym = await db.scalar(select(Mt5Symbol))
    assert sym and sym.server == "Broker-Demo" and sym.digits == 2
