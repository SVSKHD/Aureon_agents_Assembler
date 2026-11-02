"""GET /api/trading/stream (SSE) and the OpenAPI export used by the frontend's typed client."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.bootstrap import bootstrap_admin
from app.core.config import Settings
from app.main import create_app
from app.models.auth import UserSession
from app.models.trading import Instrument, Strategy, StrategyVersion, TradingSignal
from app.services import trading_stream
from app.services.trading_stream import Cursor
from tests.conftest import TEST_REDIS_URL

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct horse battery staple"


def test_openapi_export_lists_the_frontend_endpoints() -> None:
    out = subprocess.run(
        [sys.executable, "scripts/export_openapi.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = json.loads(out.stdout)["paths"]
    for p in (
        "/api/auth/login",
        "/api/auth/me",
        "/api/chat",
        "/api/models",
        "/api/trading/stream",
        "/api/trading/signals",
        "/api/trading/mt5/status",
        "/api/trading/backtests/{run_id}",
    ):
        assert p in paths, p


def test_cursor_roundtrip_and_garbage() -> None:
    c = Cursor(
        datetime(2026, 11, 2, tzinfo=UTC),
        "00000000-0000-0000-0000-000000000001",
        42,
        datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert Cursor.decode(c.encode()) == c
    assert Cursor.decode("not-base64!!") is None
    assert Cursor.decode(None) is None


def parse(body: str) -> list[dict[str, Any]]:
    events = []
    for block in body.split("\n\n"):
        ev: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            k, _, v = line.partition(": ")
            ev[k] = v
        if "event" in ev:
            ev["data"] = json.loads(ev["data"])
            events.append(ev)
    return events


class Env:
    def __init__(self, maker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.maker, self.settings = maker, settings
        self.inst_id = 0
        self.version_id: Any = None

    async def signal(self, minute: int) -> None:
        async with self.maker() as db, db.begin():
            db.add(
                TradingSignal(
                    strategy_version_id=self.version_id,
                    instrument_id=self.inst_id,
                    timeframe="5m",
                    candle_time=datetime(2026, 11, 2, 9, minute, tzinfo=UTC),
                    session="London",
                    side="BUY",
                    inputs={},
                )
            )


@pytest.fixture
async def env(
    db_clean: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Env]:
    monkeypatch.setattr(trading_stream, "LAG", timedelta(milliseconds=150))
    engine = create_async_engine(db_clean)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def clean() -> None:
        async with engine.begin() as conn:
            for t in ("trading.trading_signals", "trading.strategies", "trading.instruments"):
                await conn.exec_driver_sql(f"DELETE FROM {t}")

    await clean()
    s = Settings.model_validate(
        {
            **settings.model_dump(),
            "DATABASE_URL": db_clean,
            "TRADING_STREAM_POLL_SECONDS": 0.1,
            "TRADING_STREAM_MAX_SECONDS": 1.0,
            "TRADING_STREAM_HEARTBEAT_SECONDS": 0.3,
        }
    )
    e = Env(maker, s)
    await bootstrap_admin(maker, "admin@spasta.online", PASSWORD)
    async with maker() as db, db.begin():
        inst = Instrument(
            broker="dhan",
            broker_instrument_id="NSE_EQ:1333",
            symbol="HDFCBANK",
            instrument_type="EQUITY",
            currency="INR",
            lot_size=Decimal(1),
            tick_size=Decimal("0.05"),
        )
        strat = Strategy(name="stream-test")
        db.add_all([inst, strat])
        await db.flush()
        v = StrategyVersion(strategy_id=strat.id, version=1, code_ref="ema_cross", params={})
        db.add(v)
        await db.flush()
        e.inst_id, e.version_id = inst.id, v.id
    yield e
    await clean()
    await engine.dispose()


async def _client(env: Env) -> tuple[Any, AsyncClient]:
    mgr = LifespanManager(create_app(env.settings))
    await mgr.__aenter__()
    c = AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test")
    r = await c.post("/api/auth/login", json={"email": "admin@spasta.online", "password": PASSWORD})
    assert r.status_code == 200
    return mgr, c


async def test_stream_delivers_new_rows_and_resumes(env: Env) -> None:
    await env.signal(0)  # existed before the stream: not replayed
    redis = Redis.from_url(TEST_REDIS_URL)
    await redis.hset(
        "dhan:tick:NSE_EQ:1333", mapping={"ltp": "1650.5", "ltt": "2026-11-02T09:01:00+00:00"}
    )
    mgr, c = await _client(env)
    try:
        assert (
            await AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test").get(
                "/api/trading/stream"
            )
        ).status_code == 401

        async def later() -> None:
            await asyncio.sleep(0.3)
            await env.signal(5)

        task = asyncio.create_task(later())
        r = await c.get("/api/trading/stream", params={"instrument_id": env.inst_id})
        await task
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-accel-buffering"] == "no"
        events = parse(r.text)
        kinds = [e["event"] for e in events]
        assert kinds[0] == "hello" and kinds[-1] == "bye"
        sigs = [e for e in events if e["event"] == "signal"]
        assert len(sigs) == 1 and sigs[0]["data"]["candle_time"].startswith("2026-11-02T09:05")
        quote = next(e for e in events if e["event"] == "quote")
        assert quote["data"]["ltp"] == "1650.5" and quote["data"]["source"] == "broker"
        assert ": ping" in r.text  # heartbeat while idle
        last_id = [e["id"] for e in events if "id" in e][-1]

        # Reconnect with the cursor: nothing is repeated; a row written meanwhile arrives.
        await env.signal(10)
        r2 = await c.get("/api/trading/stream", headers={"Last-Event-ID": last_id})
        sigs2 = [e for e in parse(r2.text) if e["event"] == "signal"]
        assert [s["data"]["candle_time"][11:16] for s in sigs2] == ["09:10"]
    finally:
        await redis.delete("dhan:tick:NSE_EQ:1333")
        await redis.aclose()
        await c.aclose()
        await mgr.__aexit__(None, None, None)


async def test_stream_ends_when_the_session_is_revoked(env: Env) -> None:
    mgr, c = await _client(env)
    try:

        async def revoke() -> None:
            await asyncio.sleep(0.25)
            async with env.maker() as db, db.begin():
                await db.execute(update(UserSession).values(revoked_at=datetime.now(UTC)))

        task = asyncio.create_task(revoke())
        r = await c.get("/api/trading/stream")
        await task
        bye = [e for e in parse(r.text) if e["event"] == "bye"]
        assert bye and bye[-1]["data"]["reason"] == "session_ended"
    finally:
        await c.aclose()
        await mgr.__aexit__(None, None, None)
