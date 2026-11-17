"""Stock Signals runs against Postgres (docs/37): one event per crossover, a VWAP confirmation
updates it, states and changes, history requested on the shared queue, unavailable data,
retries and a stopped worker's run, the candle-close trigger, retention."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents.trading.types import Timeframe
from app.models.stock_signals import StockSignalEvent, StockSignalRun, StockSignalState
from app.models.trading import CandleBackfillRequest, MarketCandle
from app.services import market_session as ms
from app.services import stock_signals as svc
from tests.test_market_db import Env, EnvFactory, make_env  # noqa: F401

pytestmark = pytest.mark.db
M15 = Timeframe.M15
STEP = timedelta(minutes=15)
MON = date(2026, 11, 9)
DAYS = [MON + timedelta(days=i) for i in range(4)]  # Mon .. Thu
SIGNALS = {
    "STOCK_SIGNALS_UNIVERSES": ["nse_equities"],
    "STOCK_SIGNALS_FETCH_TIMEOUT_SECONDS": 10,
}


def ist(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ms.IST).astimezone(UTC)


def session(d: date) -> list[datetime]:
    lo, _ = ms.session_bounds(d)
    return [lo + i * STEP for i in range(25)]


@pytest.fixture(autouse=True)
async def clean_runs(db_clean: str) -> AsyncIterator[None]:
    engine = create_async_engine(db_clean)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("DELETE FROM trading.stock_signal_runs")
    await engine.dispose()
    yield


async def put(
    env: Env, iid: int, times: list[datetime], closes: list[float], vol: list[float]
) -> None:
    rows = [
        {
            "instrument_id": iid,
            "timeframe": "15m",
            "ts": t,
            "open": Decimal(str(c)),
            "high": Decimal(str(c)),
            "low": Decimal(str(c)),
            "close": Decimal(str(c)),
            "volume": Decimal(str(v)),
            "session": "Asia",
            "source": "broker",
        }
        for t, c, v in zip(times, closes, vol, strict=True)
    ]
    async with env.maker() as db, db.begin():
        await db.execute(insert(MarketCandle), rows)


async def pending_then_confirmed(env: Env, iid: int) -> None:
    """Three falling sessions, then Thursday: a huge-volume open at 190 lifts the session
    VWAP, the EMA cross at 09:45 closes under it (pending), 10:00's 230 clears it."""
    times = session(DAYS[0]) + session(DAYS[1]) + session(DAYS[2]) + session(DAYS[3])[:5]
    closes = [*np.linspace(200, 125, 75), 190.0, 150, 152, 230, 240]
    vol = [1.0] * 75 + [5000.0] + [1.0] * 4
    await put(env, iid, times, [float(c) for c in closes], vol)


def instant() -> Any:
    """A clock that jumps 60 s per call, and a sleep that does nothing: the history wait
    times out at once (no backfill worker runs in these tests)."""
    t = [datetime(2026, 11, 12, 6, 0, tzinfo=UTC)]

    def clock() -> datetime:
        t[0] += timedelta(seconds=60)
        return t[0]

    async def sleep(_: float) -> None:
        return None

    return clock, sleep


async def run_at(env: Env, boundary: datetime) -> svc.RunResult:
    rid = await svc.ensure_run(env.maker, M15, boundary, "test")
    assert rid is not None
    assert await svc.claim_run(env.maker) == rid
    clock, sleep = instant()
    return await svc.execute_run(env.maker, rid, env.settings, sleep=sleep, clock=clock)


async def test_one_event_per_cross_confirmed_later(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS) as env:
        iid = await env.instrument("SIGNAL")
        await env.instrument("FUTONLY", seg="NSE_FNO", itype="FUTSTK")  # not an equity
        await pending_then_confirmed(env, iid)

        # 10:00 IST: the 09:45 bar crossed under VWAP → one new pending event.
        first = await run_at(env, ist(DAYS[3], 10, 0))
        assert first.status == "done" and len(first.new_events) == 1
        assert first.confirmed_events == []
        async with env.maker() as db:
            ev = await db.get(StockSignalEvent, first.new_events[0])
            assert ev is not None and ev.state == "BULLISH_PENDING" and ev.direction == "bullish"
            assert ev.crossed_at == ist(DAYS[3], 10, 0) and ev.confirmed_at is None
            assert ev.exchange == "NSE" and ev.last_price == Decimal("152.00000000")
            st = await db.get(StockSignalState, (iid, "15m"))
            assert st is not None and st.state == "BULLISH_PENDING" and st.event_id == ev.id
            run = await db.get(StockSignalRun, (await db.scalar(select(StockSignalRun.id))))
            assert run is not None and run.universe_size == 1  # equities only
            assert run.history_requested == 1 and run.events_new == 1
            reqs = (await db.scalars(select(CandleBackfillRequest))).all()
            assert [(r.instrument_id, r.timeframe, r.source) for r in reqs] == [
                (iid, "15m", "system")
            ]

        # Same close again: no second run.
        assert await svc.ensure_run(env.maker, M15, ist(DAYS[3], 10, 0), "retry") is None

        # 10:15: the 10:00 bar closed above VWAP → the same event is confirmed.
        second = await run_at(env, ist(DAYS[3], 10, 15))
        assert second.new_events == [] and second.confirmed_events == first.new_events
        assert (iid, "15m", "BULLISH_PENDING", "BULLISH_CONFIRMED") in second.changes
        async with env.maker() as db:
            ev = await db.get(StockSignalEvent, first.new_events[0])
            assert ev is not None and ev.state == "BULLISH_CONFIRMED"
            assert ev.crossed_at == ist(DAYS[3], 10, 0)  # the cross time is kept
            assert ev.confirmed_at == ist(DAYS[3], 10, 15)
            assert ev.confirm_price == Decimal("230.00000000")
            st = await db.get(StockSignalState, (iid, "15m"))
            assert st is not None and st.previous_state == "BULLISH_PENDING"
            assert await db.scalar(select(func.count()).select_from(StockSignalEvent)) == 1

        # 10:30: still inside the window, already confirmed → nothing new, nothing re-sent.
        third = await run_at(env, ist(DAYS[3], 10, 30))
        assert third.new_events == [] and third.confirmed_events == []


async def test_missing_and_short_history_is_unavailable(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS) as env:
        empty = await env.instrument("EMPTY")
        short = await env.instrument("SHORT")
        await put(env, short, session(DAYS[3])[:10], [100.0 + i for i in range(10)], [1.0] * 10)
        out = await run_at(env, ist(DAYS[3], 12, 0))
        assert out.status == "done" and out.counts == {"UNAVAILABLE": 2}
        async with env.maker() as db:
            a = await db.get(StockSignalState, (empty, "15m"))
            b = await db.get(StockSignalState, (short, "15m"))
        assert a is not None and a.reason == "no closed candles stored" and a.ema_fast is None
        assert b is not None and "insufficient data" in (b.reason or "") and b.vwap is None


async def test_retry_fail_and_stopped_run(
    make_env: EnvFactory,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with make_env(**SIGNALS, STOCK_SIGNALS_MAX_ATTEMPTS=2) as env:
        rid = await svc.ensure_run(env.maker, M15, ist(DAYS[3], 11, 0), "test")
        assert rid is not None

        async def broken(*_: Any) -> list[int]:
            raise RuntimeError("database went away")

        monkeypatch.setattr(svc, "universe", broken)
        clock, sleep = instant()
        assert await svc.claim_run(env.maker) == rid
        r1 = await svc.execute_run(env.maker, rid, env.settings, sleep=sleep, clock=clock)
        assert r1.status == "queued"
        assert await svc.claim_run(env.maker) == rid
        r2 = await svc.execute_run(env.maker, rid, env.settings, sleep=sleep, clock=clock)
        assert r2.status == "failed"
        async with env.maker() as db:
            run = await db.get(StockSignalRun, rid)
        assert run is not None and run.attempts == 2 and "database went away" in (run.error or "")

        # A worker stopped mid-run: after an hour the run goes back to the queue.
        rid2 = await svc.ensure_run(env.maker, M15, ist(DAYS[3], 11, 15), "test")
        assert await svc.claim_run(env.maker) == rid2
        assert await svc.claim_run(env.maker) is None  # running, not claimable twice
        async with env.maker() as db, db.begin():
            await db.execute(
                update(StockSignalRun)
                .where(StockSignalRun.id == rid2)
                .values(started_at=datetime.now(UTC) - timedelta(hours=2))
            )
        assert await svc.claim_run(env.maker) == rid2


async def test_candle_close_trigger_is_idempotent(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS, STOCK_SIGNALS_TIMEFRAMES=["15m", "1h"]) as env:
        now = ist(DAYS[3], 10, 31)  # 15m close 10:30, 1h close 10:15
        made = await svc.queue_due_runs(env.maker, env.settings, now)
        again = await svc.queue_due_runs(env.maker, env.settings, now + timedelta(seconds=30))
        assert len(made) == 2 and again == []
        async with env.maker() as db:
            got = {
                (r.timeframe, r.boundary) for r in (await db.scalars(select(StockSignalRun))).all()
            }
        assert got == {("15m", ist(DAYS[3], 10, 30)), ("1h", ist(DAYS[3], 10, 15))}
        # Inside the run delay (20 s), and on a Saturday: nothing.
        assert (
            await svc.queue_due_runs(
                env.maker,
                env.settings,
                ist(
                    DAYS[3],
                    10,
                    45,
                ),
            )
            == []
        )
        assert (
            await svc.queue_due_runs(env.maker, env.settings, ist(date(2026, 11, 14), 11, 0)) == []
        )


async def test_purge_keeps_recent_events(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS) as env:
        iid = await env.instrument("OLD")
        await pending_then_confirmed(env, iid)
        out = await run_at(env, ist(DAYS[3], 10, 0))
        async with env.maker() as db, db.begin():
            await db.execute(
                update(StockSignalEvent).values(created_at=datetime.now(UTC) - timedelta(days=400))
            )
        assert (await svc.purge(env.maker, env.settings))["events"] == len(out.new_events) == 1
        async with env.maker() as db:
            st = await db.get(StockSignalState, (iid, "15m"))
        assert st is not None and st.event_id is None  # the state stays, its link is cleared
