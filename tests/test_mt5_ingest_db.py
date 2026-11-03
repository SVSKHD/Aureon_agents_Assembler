"""MT5 push ingest against the database: idempotency (push_id, event ids), the shared v1 sync,
failures open/bump/resolve, stale watch job, retention, the dashboard reads and the SSE event."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.registry import effective_state
from app.agents.trading.mt5.push import parse_push
from app.auth.bootstrap import bootstrap_admin
from app.core.config import Settings
from app.main import create_app
from app.models.agents import Agent, AgentLog
from app.models.brokers import Mt5BotSnapshot, Mt5Candle, Mt5Event, Mt5Failure, Mt5Push
from app.services import mt5_ingest, trading_stream
from app.services.mt5_ingest import DbIngestStore
from tests.mt5_push_fixture import TOKEN, push_doc

pytestmark = pytest.mark.db
PASSWORD = "correct horse battery staple"
TABLES = ("mt5.pushes", "mt5.bot_snapshots", "mt5.events", "mt5.failures", "mt5.candles")


class Env:
    def __init__(self, maker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.maker, self.settings = maker, settings
        self.store = DbIngestStore(maker)

    async def push(self, d: dict[str, Any]) -> mt5_ingest.IngestResult:
        return await self.store.store(parse_push(json.dumps(d).encode()), 1, self.settings)

    async def count(self, model: Any, *where: Any) -> int:
        async with self.maker() as db:
            return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)

    async def failures(self) -> list[Mt5Failure]:
        async with self.maker() as db:
            return list((await db.scalars(select(Mt5Failure).order_by(Mt5Failure.id))).all())


@pytest.fixture
async def env(db_clean: str, settings: Settings) -> AsyncIterator[Env]:
    engine = create_async_engine(db_clean)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def clean() -> None:
        async with engine.begin() as conn:
            for t in TABLES:
                await conn.exec_driver_sql(f"DELETE FROM {t}")

    await clean()
    s = Settings.model_validate(
        {**settings.model_dump(), "MT5_INGEST_TOKEN": TOKEN, "DATABASE_URL": db_clean}
    )
    yield Env(maker, s)
    await clean()
    await engine.dispose()


async def test_push_is_stored_once(env: Env) -> None:
    d = push_doc()
    r1 = await env.push(d)
    assert not r1.duplicate and r1.events_new == len(d["events"])
    r2 = await env.push(d)
    assert r2.duplicate
    assert await env.count(Mt5Push) == 1
    assert await env.count(Mt5Event) == len(d["events"])
    # v1 sync reused: heartbeat + journal logs, written once
    async with env.maker() as db:
        bot = await db.get(Agent, "mt5-bot")
        assert bot is not None and bot.details["source"] == "push"
        assert bot.details["stale_after_s"] == env.settings.MT5_STALE_HEARTBEAT_SECONDS
        assert effective_state(bot, 60) == "idle"
    assert await env.count(AgentLog, AgentLog.agent_id == "mt5-bot") == len(d["events"])


async def test_duplicate_event_ids_do_nothing(env: Env) -> None:
    d = push_doc()
    await env.push(d)
    d2 = push_doc(events=[*d["events"], {"id": "new-1", "t": 1793612900, "event": "locked"}])
    r = await env.push(d2)
    assert (r.events_new, r.events_duplicate) == (1, len(d["events"]))
    assert await env.count(Mt5Event) == len(d["events"]) + 1


async def test_snapshot_candles_and_older_push_does_not_win(env: Env) -> None:
    await env.push(push_doc(seq=5))
    old = push_doc(seq=4, generated_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat())
    await env.push(old)
    async with env.maker() as db:
        snap = await db.get(Mt5BotSnapshot, "mt5-bot")
        assert snap is not None and snap.seq == 5
        assert snap.guardian["XAUUSD"][0]["phase"] == "protect"
        assert snap.strategy["ema"] == {"fast": 50, "slow": 80}
        c = (await db.scalars(select(Mt5Candle).order_by(Mt5Candle.ts))).all()
    assert len(c) == 3
    # server clock (offset_h 2) → UTC
    assert c[-1].ts == datetime.fromtimestamp(1793620500, UTC) - timedelta(hours=2)
    assert c[-1].ema == {"50": "2647.1", "80": "2642.2"}


async def test_failures_open_bump_and_resolve(env: Env) -> None:
    bad = push_doc()
    bad["health"]["mt5"] = {"connected": False, "last_error": "IPC timeout"}
    await env.push(bad)
    bad2 = push_doc(health=bad["health"])
    await env.push(bad2)
    rows = {(f.kind, f.symbol): f for f in await env.failures()}
    assert rows[("mt5_disconnected", None)].count == 2
    assert rows[("mt5_disconnected", None)].resolved_at is None
    assert "IPC timeout" in rows[("mt5_disconnected", None)].reason
    # the journal error event is counted once although both pushes carry it
    assert rows[("exception", "XAGUSD")].count == 1
    await env.push(push_doc())  # healthy again
    rows = {(f.kind, f.symbol): f for f in await env.failures()}
    assert rows[("mt5_disconnected", None)].resolved_at is not None
    assert rows[("exception", "XAGUSD")].resolved_at is None  # occurrences wait for quiet time


async def test_watch_opens_stale_heartbeat_and_a_push_resolves_it(env: Env) -> None:
    await env.push(push_doc())
    assert (await mt5_ingest.watch(env.maker, env.settings))["stale"] is False
    async with env.maker() as db, db.begin():
        await db.execute(
            update(Mt5BotSnapshot).values(received_at=func.now() - timedelta(seconds=200))
        )
    assert (await mt5_ingest.watch(env.maker, env.settings))["stale"] is True
    await mt5_ingest.watch(env.maker, env.settings)
    stale = [f for f in await env.failures() if f.kind == "stale_heartbeat"]
    assert len(stale) == 1 and stale[0].count == 2 and "no push" in stale[0].reason
    await env.push(push_doc())
    stale = [f for f in await env.failures() if f.kind == "stale_heartbeat"]
    assert stale[0].resolved_at is not None


async def test_purge_retention(env: Env) -> None:
    await env.push(push_doc())
    async with env.maker() as db, db.begin():
        await db.execute(update(Mt5Candle).values(ts=Mt5Candle.ts - timedelta(days=400)))
        await db.execute(update(Mt5Event).values(t=Mt5Event.t - timedelta(days=400)))
    out = await mt5_ingest.purge(env.maker, env.settings)
    assert out["candles"] == 3 and out["events"] > 0
    assert await env.count(Mt5Candle) == 0


async def test_api_end_to_end_and_stream_event(env: Env) -> None:
    await bootstrap_admin(env.maker, "admin@spasta.online", PASSWORD)
    app = create_app(env.settings)
    async with (
        LifespanManager(app) as mgr,
        AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c,
    ):
        d = push_doc()
        r = await c.post(
            "/api/trading/mt5/ingest",
            content=json.dumps(d).encode(),
            headers={"authorization": f"Bearer {TOKEN}", "x-aureon-push-id": d["push_id"]},
        )
        assert r.status_code == 202, r.text
        r = await c.post(
            "/api/trading/mt5/ingest",
            content=json.dumps(d).encode(),
            headers={"authorization": f"Bearer {TOKEN}"},
        )
        assert r.status_code == 200 and r.json()["duplicate"] is True

        # reads need a session
        assert (await c.get("/api/trading/mt5/bot-status")).status_code == 401
        await c.post("/api/auth/login", json={"email": "admin@spasta.online", "password": PASSWORD})

        st = (await c.get("/api/trading/mt5/bot-status")).json()
        assert st["state"] == "online" and st["push_id"] == d["push_id"]
        assert st["open_failures"] == 1  # the journal error event

        mk = (await c.get("/api/trading/mt5/market")).json()
        xau = next(s for s in mk["symbols"] if s["symbol"] == "XAUUSD")
        assert xau["open"] is True and xau["timeframe"] == "M5"
        assert xau["session"] in ("Asia", "London", "NY") and xau["bot_session"] == "london"
        assert xau["last_price"] == "2653.5" and xau["ema_periods"] == ["50", "80"]
        assert len(xau["candles"]) == 3
        xag = next(s for s in mk["symbols"] if s["symbol"] == "XAGUSD")
        assert xag["candles"] == [] and xag["last_price"] == "31.38"  # no M5 bars: position price

        pos = (await c.get("/api/trading/mt5/positions")).json()
        p101 = next(p for p in pos["items"] if p["ticket"] == 101)
        assert p101["guardian"]["phase"] == "protect" and p101["guardian"]["leg_phase"] == "B"
        assert p101["guardian"]["secured_level"] == "2648" and p101["guardian"]["peak"] == "2656.1"
        assert pos["stale"] is False

        sig = (await c.get("/api/trading/mt5/signals", params={"kind": "cross"})).json()
        assert sig["total"] == 2 and {i["kind"] for i in sig["items"]} == {"cross"}
        assert sig["latest"]["XAUUSD"]["partial"] == {"x": 1}
        ex = await c.get("/api/trading/mt5/signals", params=[("kind", "exit"), ("kind", "error")])
        assert {i["kind"] for i in ex.json()["items"]} == {"exit", "error"}

        fl = (await c.get("/api/trading/mt5/failures")).json()
        assert fl["open_count"] == 1 and fl["items"][0]["reason"] == "tick timeout"

        stg = (await c.get("/api/trading/mt5/strategy")).json()
        assert stg["mode"] == "ema5080" and stg["ema"] == {"fast": 50, "slow": 80}

        rep = (await c.get("/api/trading/mt5/report", params={"days": 90})).json()
        assert rep["performance"]["exits_by_symbol"]["XAUUSD"]["exits"] == 2
        assert rep["backtests"][0]["mode"] == "ema5080"

        # stale on read, before the watch job runs
        async with env.maker() as db, db.begin():
            await db.execute(
                update(Mt5BotSnapshot).values(received_at=func.now() - timedelta(seconds=300))
            )
        st = (await c.get("/api/trading/mt5/bot-status")).json()
        assert st["state"] == "stale"
        fl = (await c.get("/api/trading/mt5/failures")).json()
        assert fl["items"][0]["kind"] == "stale_heartbeat" and fl["items"][0]["id"] is None
        assert (await c.get("/api/trading/mt5/positions")).json()["stale"] is True

    # SSE: one `mt5` event per change
    state = trading_stream.StreamState(
        trading_stream.Cursor(datetime.now(UTC), str(uuid.UUID(int=0)), 0, datetime.now(UTC))
    )
    async with env.maker() as db:
        first = await trading_stream.mt5_events(db, state)
        again = await trading_stream.mt5_events(db, state)
    assert [e for e, _ in first] == ["mt5"] and first[0][1]["push_id"] == uuid.UUID(d["push_id"])
    assert again == []
    await env.push(push_doc())
    async with env.maker() as db:
        assert [e for e, _ in await trading_stream.mt5_events(db, state)] == ["mt5"]
