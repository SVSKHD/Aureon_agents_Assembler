"""Dhan sync jobs, scheduler tasks, feed sink and API wiring (DB + Redis, mocked Dhan)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.registry import SCHEDULER, AgentRegistry
from app.agents.trading.dhan.adapter import DbInstrumentResolver, DhanAdapter
from app.agents.trading.dhan.client import DhanClient
from app.agents.trading.dhan.feed import FeedPacket
from app.agents.trading.types import Timeframe
from app.auth.bootstrap import bootstrap_admin
from app.core.config import Settings
from app.main import create_app
from app.models.agents import AgentLog, ScheduledJob
from app.models.brokers import (
    DhanAccountProfile,
    DhanFundSnapshot,
    DhanInstrument,
    DhanInstrumentSyncRun,
)
from app.models.trading import (
    BrokerAccount,
    BrokerOrderRow,
    Execution,
    Instrument,
    MarketCandle,
    OrderEvent,
    OrderIntent,
    PositionSnapshot,
)
from app.services import dhan_sync
from app.workers.dhan_feed import FeedSink
from app.workers.scheduler import Scheduler, TaskContext, default_jobs, dhan_reconcile
from tests.conftest import TEST_REDIS_URL

pytestmark = pytest.mark.db
FIX = Path(__file__).parent / "fixtures" / "dhan"
BASE = "https://api.dhan.test/v2"
CSV_URL = "https://images.dhan.test/master.csv"
CLIENT = "1100003626"
PASSWORD = "correct horse battery staple"


def fx(name: str) -> Any:
    return json.loads((FIX / name).read_text())


def mock_dhan(profile: dict[str, Any] | None = None) -> None:
    respx.get(f"{BASE}/profile").mock(
        return_value=httpx.Response(200, json=profile or fx("profile.json"))
    )
    for path, name in (
        ("fundlimit", "fundlimit"),
        ("positions", "positions"),
        ("orders", "orders"),
        ("trades", "trades"),
        ("holdings", "holdings"),
    ):
        respx.get(f"{BASE}/{path}").mock(return_value=httpx.Response(200, json=fx(f"{name}.json")))
    respx.get(CSV_URL).mock(
        return_value=httpx.Response(200, content=(FIX / "master_detailed.csv").read_bytes())
    )


class Env:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        adapter: DhanAdapter,
        account: BrokerAccount,
        admin_id: Any,
    ) -> None:
        self.maker, self.adapter, self.account, self.admin_id = maker, adapter, account, admin_id

    async def count(self, model: Any) -> int:
        async with self.maker() as db:
            return int(await db.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture
async def env(db_clean: str) -> AsyncIterator[Env]:
    engine = create_async_engine(db_clean)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        for t in (
            "dhan.instruments",
            "dhan.account_profiles",
            "dhan.fund_snapshots",
            "dhan.instrument_sync_runs",
            "trading.instruments",
        ):
            await conn.exec_driver_sql(f"DELETE FROM {t}")
    admin = await bootstrap_admin(maker, "admin@spasta.online", PASSWORD)
    async with maker() as db, db.begin():
        acct = BrokerAccount(
            broker="dhan", account_id=CLIENT, base_currency="INR", owner_user_id=admin.id
        )
        db.add(acct)
    client = DhanClient(CLIENT, "tok", base_url=BASE, limiter=None)
    adapter = DhanAdapter(client, DbInstrumentResolver(maker), instrument_csv_url=CSV_URL)
    yield Env(maker, adapter, acct, admin.id)
    await adapter.aclose()
    await engine.dispose()


# --- instruments --------------------------------------------------------------------------


@respx.mock
async def test_instrument_sync_upserts_links_and_deactivates(env: Env) -> None:
    mock_dhan()
    async with env.maker() as db, db.begin():
        db.add(
            Instrument(
                broker="dhan",
                broker_instrument_id="NSE_EQ:1",
                symbol="DELISTED",
                instrument_type="EQUITY",
                currency="INR",
                lot_size=Decimal(1),
                tick_size=Decimal("0.05"),
                updated_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
    # Fixed "today" so the fixture's 2026-11-26 option is not treated as expired later.
    day = date(2026, 11, 10)
    out = await dhan_sync.sync_instruments(env.maker, env.adapter, today=day)
    assert (out["instruments"], out["deactivated"], out["expired"]) == (3, 1, 0)
    assert out["rows_unparsed"] == 1  # the NSE,M row has no API segment
    out2 = await dhan_sync.sync_instruments(env.maker, env.adapter, today=day)  # idempotent
    assert out2["instruments"] == 3 and await env.count(DhanInstrument) == 3
    async with env.maker() as db:
        opt = await db.scalar(
            select(Instrument).where(Instrument.broker_instrument_id == "NSE_FNO:52175")
        )
        d = await db.scalar(select(DhanInstrument).where(DhanInstrument.security_id == "52175"))
        old = await db.scalar(
            select(Instrument).where(Instrument.broker_instrument_id == "NSE_EQ:1")
        )
    assert opt and opt.lot_size == Decimal(75) and opt.underlying == "NIFTY" and opt.is_active
    assert d and d.trading_instrument_id == opt.id and d.instrument == "OPTIDX"
    assert old and old.is_active is False and old.inactive_reason == "missing_from_master"
    async with env.maker() as db:
        runs = (
            await db.scalars(select(DhanInstrumentSyncRun).order_by(DhanInstrumentSyncRun.id))
        ).all()
    assert [r.status for r in runs] == ["succeeded", "succeeded"]
    assert runs[0].upserted == 3 and runs[0].source_host == urlsplit(CSV_URL).hostname
    # The DB resolver now knows the instrument type needed for chart APIs.
    ref = await DbInstrumentResolver(env.maker).resolve("NSE_FNO:52175")
    assert ref and ref.instrument == "OPTIDX"


def test_instrument_writes_stay_under_the_postgres_parameter_limit() -> None:
    rows = [{f"c{k}": k for k in range(23)} for _ in range(dhan_sync.BATCH)]
    pieces = dhan_sync._pieces(rows)
    assert sum(len(p) for p in pieces) == dhan_sync.BATCH
    assert all(len(p) * 23 <= dhan_sync.PG_MAX_PARAMS for p in pieces)
    assert dhan_sync._pieces([]) == []


@respx.mock
async def test_instrument_sync_saves_a_full_batch(env: Env) -> None:
    """A real master has ~200k rows: a full 2000-row batch must fit Postgres's 32767 limit."""
    header = (FIX / "master_real_sample.csv").read_text(encoding="utf-8").splitlines()[0]
    n = dhan_sync.BATCH + 500
    lines = [header] + [
        f"NSE,E,{900000 + k},,EQUITY,,SYM{k},SYM {k} LTD,Sym {k},ES,EQ,1.0,,,,0.05,NA,"
        "N,N,N,NA,A,0,0,0,0,0,0,0,0,0,0,"
        for k in range(n)
    ]
    body = "\n".join(lines).encode()
    respx.get(CSV_URL).mock(return_value=httpx.Response(200, content=body))
    out = await dhan_sync.sync_instruments(env.maker, env.adapter, today=date(2026, 11, 10))
    assert out["instruments"] == n
    assert await env.count(DhanInstrument) == n
    assert await env.count(Instrument) == n
    async with env.maker() as db:
        run = await db.scalar(select(DhanInstrumentSyncRun))
    assert run and run.status == "succeeded" and run.upserted == n


@respx.mock
async def test_empty_master_never_deactivates_everything(env: Env) -> None:
    respx.get(CSV_URL).mock(return_value=httpx.Response(200, content=b"EXCH_ID,SEGMENT\n"))
    with pytest.raises(RuntimeError, match="empty"):
        await dhan_sync.sync_instruments(env.maker, env.adapter)


# --- reconciliation -----------------------------------------------------------------------


@respx.mock
async def test_reconcile_is_idempotent_and_records_changes(env: Env) -> None:
    mock_dhan()
    await dhan_sync.sync_instruments(env.maker, env.adapter)
    async with env.maker() as db, db.begin():
        inst = await db.scalar(
            select(Instrument.id).where(Instrument.broker_instrument_id == "NSE_EQ:3499")
        )
        db.add(
            OrderIntent(
                broker_account_id=env.account.id,
                instrument_id=inst,
                side="BUY",
                order_type="LIMIT",
                quantity=Decimal(5),
                mode="OBSERVE",
                source="manual",
                correlation_id="aureon-corr-1",
            )
        )

    first = await dhan_sync.reconcile_account(env.maker, env.adapter, env.account)
    assert (first["orders_new"], first["executions_new"], first["positions"]) == (2, 1, 2)
    second = await dhan_sync.reconcile_account(env.maker, env.adapter, env.account)
    assert (
        second["orders_new"],
        second["orders_changed"],
        second["orders_unchanged"],
        second["executions_new"],
    ) == (0, 0, 2, 0)
    assert await env.count(OrderEvent) == 2  # nothing new on an unchanged book

    # The pending order fills → exactly one status-change event.
    changed = fx("orders.json")
    changed[0] = {
        **changed[0],
        "orderStatus": "TRADED",
        "filledQty": 5,
        "averageTradedPrice": 142.5,
    }
    respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json=changed))
    third = await dhan_sync.reconcile_account(env.maker, env.adapter, env.account)
    assert third["orders_changed"] == 1 and await env.count(OrderEvent) == 3

    async with env.maker() as db:
        row = await db.scalar(
            select(BrokerOrderRow).where(BrokerOrderRow.broker_order_id == "112111182198")
        )
        events = list(await db.scalars(select(OrderEvent).order_by(OrderEvent.id)))
        execution = await db.scalar(select(Execution))
        profile = await db.get(DhanAccountProfile, env.account.id)
    assert row and row.normalized_status == "FILLED" and row.order_intent_id is not None
    assert row.instrument_id is not None
    last = events[-1]
    assert last.event_type == "broker_status_changed" and last.correlation_id == "aureon-corr-1"
    assert last.payload["before"] == {"status": "PENDING", "filled": "0"}
    assert last.payload["request_id"].startswith("aureon-")
    # An order without a correlation id is keyed by the run's correlation id.
    assert events[1].correlation_id and events[1].correlation_id.startswith("recon-")
    assert execution and execution.broker_trade_id == "15112111182199001"
    assert (execution.price, execution.session, execution.is_simulated) == (
        Decimal("130.05"),
        "Asia",
        False,
    )  # 09:21 IST = 03:51 UTC → Asia
    assert await env.count(PositionSnapshot) == 6 and await env.count(DhanFundSnapshot) == 3
    assert profile and profile.token_expires_at == datetime(2030, 3, 30, 10, 7, tzinfo=UTC)
    assert profile.active_segments[0] == "Equity"


@respx.mock
async def test_trades_for_unknown_instruments_are_counted_not_invented(env: Env) -> None:
    mock_dhan()  # no instrument sync → no instrument rows
    out = await dhan_sync.reconcile_account(env.maker, env.adapter, env.account)
    assert out["trades_unknown_instrument"] == 1 and await env.count(Execution) == 0


@respx.mock
async def test_candle_backfill_upserts_with_sessions(env: Env) -> None:
    mock_dhan()
    await dhan_sync.sync_instruments(env.maker, env.adapter)
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    ts = [
        int((now - timedelta(minutes=30)).timestamp()),
        int((now - timedelta(minutes=15)).timestamp()),
    ]
    respx.post(f"{BASE}/charts/intraday").mock(
        return_value=httpx.Response(
            200,
            json={
                "open": [142, 143],
                "high": [143, 144],
                "low": [141, 142],
                "close": [142.5, 143.25],
                "volume": [10, 20],
                "timestamp": ts,
            },
        )
    )
    out = await dhan_sync.backfill_candles(
        env.maker, env.adapter, ["NSE_EQ:3499", "NSE_EQ:7"], [Timeframe.M15], 1
    )
    assert out == {"candles": 2, "unknown_instruments": ["NSE_EQ:7"]}
    again = await dhan_sync.backfill_candles(
        env.maker, env.adapter, ["NSE_EQ:3499"], [Timeframe.M15], 1
    )
    assert again["candles"] == 2 and await env.count(MarketCandle) == 2  # upsert, no dupes
    async with env.maker() as db:
        c = await db.scalar(select(MarketCandle).order_by(MarketCandle.ts.desc()))
    assert c and c.close == Decimal("143.25") and c.source == "broker"
    assert c.session in ("Asia", "London", "NY")


# --- scheduler integration ----------------------------------------------------------------


async def test_dhan_jobs_only_when_configured(settings: Settings) -> None:
    assert not any(j[0].startswith("dhan") for j in default_jobs(settings))
    on = settings.model_copy(update={"DHAN_CLIENT_ID": CLIENT, "DHAN_ACCESS_TOKEN": "x"})
    ids = [j[0] for j in default_jobs(on)]
    assert "dhan-reconcile" in ids and "dhan-refresh-instruments" in ids
    assert "dhan-backfill-candles" not in ids
    with_bf = on.model_copy(update={"DHAN_BACKFILL_INSTRUMENTS": ["NSE_EQ:3499"]})
    assert "dhan-backfill-candles" in [j[0] for j in default_jobs(with_bf)]


@respx.mock
async def test_reconcile_task_warns_admin_before_token_expiry(env: Env, settings: Settings) -> None:
    soon = (datetime.now(UTC) + timedelta(hours=2)).astimezone(
        __import__("app.agents.trading.dhan.parse", fromlist=["IST"]).IST
    )
    mock_dhan({**fx("profile.json"), "tokenValidity": soon.strftime("%d/%m/%Y %H:%M")})
    await AgentRegistry(env.maker).register(SCHEDULER)
    ctx = TaskContext(settings, env.maker, None, env.adapter)
    out = await dhan_reconcile(ctx)
    assert out["token_ok"] and 1.5 < out["token_hours_left"] <= 2.0
    assert out["accounts"][0]["orders_new"] == 2
    async with env.maker() as db:
        log = await db.scalar(select(AgentLog).where(AgentLog.agent_id == "scheduler"))
    assert log and log.level == "warning" and "expires in" in log.message

    respx.get(f"{BASE}/profile").mock(
        return_value=httpx.Response(401, json={"errorCode": "DH-901"})
    )
    with pytest.raises(RuntimeError, match="unusable"):
        await dhan_reconcile(ctx)


async def test_scheduler_seeds_dhan_jobs(env: Env, settings: Settings) -> None:
    on = settings.model_copy(update={"DHAN_CLIENT_ID": CLIENT, "DHAN_ACCESS_TOKEN": "x"})
    sched = Scheduler(on, env.maker, dhan=env.adapter)
    await sched.registry.register(SCHEDULER)
    await sched.ensure_default_jobs()
    async with env.maker() as db:
        job = await db.get(ScheduledJob, "dhan-reconcile")
    assert job and job.cron == on.DHAN_RECONCILE_CRON and job.task == "dhan_reconcile"


# --- feed sink ----------------------------------------------------------------------------


async def test_feed_sink_writes_latest_tick_and_marks_stale(db_clean: str) -> None:
    redis = Redis.from_url(TEST_REDIS_URL)
    sink = FeedSink(redis, ["NSE_EQ:3499", "NSE_EQ:1594"], stale_after=30)
    sub = redis.pubsub()
    await sub.subscribe("dhan:ticks")
    await sink.write(
        FeedPacket(kind="ticker", segment="NSE_EQ", security_id="3499", ltp=Decimal("142.85"))
    )
    stored = await redis.hgetall("dhan:tick:NSE_EQ:3499")
    assert stored[b"ltp"] == b"142.85" and stored[b"stale"] == b"0"
    msg = None
    for _ in range(20):
        msg = await sub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if msg:
            break
    assert msg and json.loads(msg["data"])["ltp"] == "142.85"
    # 10:00 IST on a Monday: market open. 1594 never ticked → stale; 3499 is fresh.
    monday_open = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
    sink.started = monday_open.timestamp() - 120
    sink.last_seen["NSE_EQ:3499"] = monday_open.timestamp() - 5
    assert await sink.mark_stale(monday_open) == {"NSE_EQ:1594"}
    assert (await redis.hget("dhan:tick:NSE_EQ:1594", "stale")) == b"1"
    # Weekend: nothing is "stale" just because the market is closed.
    assert await sink.mark_stale(datetime(2026, 11, 7, 6, 0, tzinfo=UTC)) == set()
    await sub.aclose()
    await redis.aclose()


# --- API with the Dhan adapter registered from settings -----------------------------------


@respx.mock
async def test_api_uses_dhan_adapter_when_configured(db_clean: str, settings: Settings) -> None:
    mock_dhan()
    respx.post(f"{BASE}/optionchain").mock(
        return_value=httpx.Response(200, json=fx("optionchain.json"))
    )
    engine = create_async_engine(db_clean)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    admin = await bootstrap_admin(maker, "admin@spasta.online", PASSWORD)
    await engine.dispose()
    s = settings.model_copy(
        update={"DHAN_CLIENT_ID": CLIENT, "DHAN_ACCESS_TOKEN": "tok", "DHAN_BASE_URL": BASE}
    )
    app: FastAPI = create_app(Settings.model_validate(s.model_dump()))
    async with (
        LifespanManager(app) as mgr,
        AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c,
    ):
        csrf = (
            await c.post(
                "/api/auth/login", json={"email": "admin@spasta.online", "password": PASSWORD}
            )
        ).json()["csrf_token"]
        acct = (
            await c.post(
                "/api/trading/accounts",
                headers={"x-csrf-token": csrf},
                json={
                    "broker": "dhan",
                    "account_id": CLIENT,
                    "base_currency": "INR",
                    "owner_user_id": str(admin.id),
                },
            )
        ).json()["id"]
        h = (await c.get(f"/api/trading/accounts/{acct}/health")).json()
        assert h["supported"] and h["ok"]
        f = (await c.get(f"/api/trading/accounts/{acct}/funds")).json()
        assert f["available"] == "98440.0" and f["source"] == "broker"
        hold = (await c.get(f"/api/trading/accounts/{acct}/holdings")).json()
        assert hold["items"][0]["isin"] == "INE001A01036"
        trades = (await c.get(f"/api/trading/accounts/{acct}/trades")).json()
        assert trades["items"][0]["price"] == "130.05"
        pos = (await c.get(f"/api/trading/accounts/{acct}/positions")).json()
        assert pos["total"] == 2
        oc = (
            await c.get(
                "/api/trading/option-chain",
                params={"underlying": "NSE_EQ:3499", "expiry": "2026-11-26"},
            )
        ).json()
        assert oc["strikes"][1]["call"]["delta"] == "0.53871"
        bad = await c.get(
            "/api/trading/option-chain", params={"underlying": "NIFTY", "expiry": "2026-11-26"}
        )
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "bad_request"
        # A second Dhan account with different credentials: refused, never mixed up.
        other = (
            await c.post(
                "/api/trading/accounts",
                headers={"x-csrf-token": csrf},
                json={"broker": "dhan", "account_id": "999", "base_currency": "INR"},
            )
        ).json()["id"]
        r = await c.get(f"/api/trading/accounts/{other}/funds")
        assert r.status_code == 501 and "different client id" in r.json()["detail"]["detail"]
