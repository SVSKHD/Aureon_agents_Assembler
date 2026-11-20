"""The market-live worker (app/workers/market_live.py, docs/38) without a database.

Reference data (instruments, universes, daily closes, history) is injected; Redis is
tests/fake_redis.py. Times: Monday 2026-11-02 (trading day), IST.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.agents.research import live_market as lm
from app.agents.trading.dhan.parse import IST
from app.agents.trading.types import Timeframe
from app.core.config import Settings
from app.services import dhan_feed_health as fh
from app.services import market_live as ml
from app.services import screener as screener_svc
from app.workers import market_live as w
from tests.fake_redis import FakeRedis

D = Decimal
MON = date(2026, 11, 2)
FRI = date(2026, 10, 30)
A, B, C = "NSE_EQ:1", "NSE_EQ:2", "NSE_EQ:3"


def ist(h: int, m: int, s: int = 0, day: date = MON) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=IST).astimezone(UTC)


@pytest.fixture
def s(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "MARKET_LIVE_TIMEFRAMES": ["1m", "5m", "15m"],
            "MARKET_LIVE_SCREENER_TIMEFRAMES": ["15m"],
            "MARKET_LIVE_MIN_PRICE": 10.0,
            "MARKET_LIVE_MIN_AVG_TURNOVER": 0.0,
        }
    )


def runtime(s: Settings, r: FakeRedis, now: datetime) -> w.LiveRuntime:
    clock = {"now": now}
    rt = w.LiveRuntime(s, r, None, asyncio.Event(), clock=lambda: clock["now"], sleep_unit=0.01)
    rt.clock_box = clock  # type: ignore[attr-defined]
    rt.leader = True
    rt.ref.inst = {
        k: ml.InstInfo(i, k, f"SYM{i}", f"Company {i}", "NSE", "NSE_EQ")
        for i, k in ((1, A), (2, B), (3, C))
    }
    rt.ref.universes = {"fno_stocks": [A, B, C]}
    rt.ref.universe_total = {"fno_stocks": 3}
    rt.ref.daily = {A: {FRI: D(100)}, B: {FRI: D(200)}, C: {FRI: D(50)}}
    return rt


def packet(key: str, ltp: str, at: datetime, vol: int, **kw: Any) -> dict[str, Any]:
    seg, sid = key.split(":")
    return {
        "kind": kw.pop("kind", "quote"),
        "segment": seg,
        "security_id": sid,
        "ltp": ltp,
        "ltt": at.isoformat(),
        "volume": str(vol),
        "received_at": (kw.pop("received", at)).isoformat(),
        **kw,
    }


async def test_ticks_become_quotes_and_bars_in_redis(s: Settings) -> None:
    r = FakeRedis()
    rt = runtime(s, r, ist(10, 0, 30))
    rt.handle_packet(packet(A, "102", ist(10, 0, 5), 1000))
    rt.handle_packet(packet(A, "103", ist(10, 0, 25), 1500))
    assert await rt.flush(ist(10, 0, 30)) > 0
    q = json.loads(r.kv[ml.QUOTE_KEY.format(ref=A)])
    assert (
        q["ltp"] == "103" and q["prev_close"] == "100" and q["prev_close_source"] == "daily_candle"
    )
    assert q["change_pct"] == "3.0000" and q["status"] == "live" and q["instrument_id"] == 1
    forming = json.loads(r.kv[ml.BAR_KEY.format(tf="1m", ref=A)])
    assert forming["close"] == "103" and forming["closed"] is False and forming["partial"] is True
    channels = [ch for ch, _ in r.published]
    assert ml.CH_QUOTES in channels and ml.CH_CANDLES in channels
    # The next minute closes 10:00 (published once as closed, appended to today's list).
    r.published.clear()
    rt.handle_packet(packet(A, "104", ist(10, 1, 2), 1600))
    await rt.flush(ist(10, 1, 3))
    closed = [
        json.loads(m) for ch, m in r.published if ch == ml.CH_CANDLES and json.loads(m)["closed"]
    ]
    assert [(c["timeframe"], c["ts"]) for c in closed] == [("1m", ist(10, 0).isoformat())]
    stored = r.lists[ml.BARS_KEY.format(tf="1m", ref=A)]
    assert len(stored) == 1 and json.loads(stored[0])["close"] == "103"
    # Nothing new: nothing published.
    r.published.clear()
    assert await rt.flush(ist(10, 1, 4)) == 0 and r.published == []


async def test_feed_prev_close_and_market_closed(s: Settings) -> None:
    r = FakeRedis()
    rt = runtime(s, r, ist(9, 0))
    rt.handle_packet(
        {
            "kind": "prev_close",
            "segment": "NSE_EQ",
            "security_id": "1",
            "prev_close": "101",
            "received_at": ist(9, 0).isoformat(),
        }
    )
    rt.handle_packet(packet(A, "99.99", ist(15, 29, 58), 9000))
    # The session ends: the last bars close on the clock, and the quote is "closed", not stale.
    n = rt.close_due(ist(15, 30, 3))
    assert n == 3 and {b.tf.value for b in rt.closed_out} == {"1m", "5m", "15m"}
    await rt.flush(ist(20, 0))
    q = json.loads(r.kv[ml.QUOTE_KEY.format(ref=A)])
    assert q["status"] == "closed" and q["prev_close_source"] == "feed" and q["prev_close"] == "101"
    assert q["change_pct"] == "-1.0000"
    # Read back hours later through the API path: still closed, age reported.
    views = await ml.read_quotes(r, [A], now=ist(21, 0), settings=s)
    assert views[A]["status"] == "closed" and views[A]["age_seconds"] > 3600


async def test_stale_quote_while_open(s: Settings) -> None:
    r = FakeRedis()
    rt = runtime(s, r, ist(10, 0))
    rt.handle_packet(packet(A, "101", ist(10, 0), 10))
    await rt.flush(ist(10, 0, 1))
    views = await ml.read_quotes(r, [A], now=ist(10, 5), settings=s)
    assert views[A]["status"] == "stale"


async def test_feed_reconnect_marks_forming_bars_partial(s: Settings) -> None:
    r = FakeRedis()
    rt = runtime(s, r, ist(10, 0))
    await r.set(
        fh.STATUS_KEY, json.dumps({"connected": True, "connects": 1, "last_connected_at": 1.0})
    )
    await rt.watch_feed_once()
    rt.handle_packet(packet(A, "101", ist(10, 0, 1), 10))
    rt.handle_packet(packet(A, "101", ist(10, 1, 1), 20))  # 10:01 bar: complete so far
    assert rt.book.get(A, Timeframe.M1).partial is False  # type: ignore[union-attr]
    await r.set(
        fh.STATUS_KEY, json.dumps({"connected": True, "connects": 2, "last_connected_at": 9.0})
    )
    await rt.watch_feed_once()
    assert rt.counters.continuity_resets == 1
    assert rt.book.get(A, Timeframe.M1).partial is True  # type: ignore[union-attr]


async def test_redis_outage_during_flush_keeps_everything(s: Settings) -> None:
    r = FakeRedis()
    rt = runtime(s, r, ist(10, 0))
    rt.handle_packet(packet(A, "101", ist(10, 0, 1), 10))
    rt.handle_packet(packet(A, "102", ist(10, 1, 1), 20))  # closes 10:00
    r.fail = True
    assert await rt.flush(ist(10, 1, 2)) == 0 and rt.counters.redis_errors == 1
    assert rt.closed_out and rt.dirty_quotes
    r.fail = False
    assert await rt.flush(ist(10, 1, 3)) > 0
    assert len(r.lists[ml.BARS_KEY.format(tf="1m", ref=A)]) == 1 and not rt.closed_out


async def test_tick_subscription_loss_resets_continuity(s: Settings) -> None:
    r = FakeRedis()
    rt = runtime(s, r, ist(10, 0))
    task = asyncio.create_task(rt.ticks_loop())
    for _ in range(100):
        if r._subs:
            break
        await asyncio.sleep(0.01)
    await r.publish(w.TICK_CHANNEL, json.dumps(packet(A, "101", ist(10, 0, 1), 10)))
    for _ in range(100):
        if rt.counters.packets:
            break
        await asyncio.sleep(0.01)
    r.fail = True  # held until the worker notices (its next poll), as a real outage would be
    for _ in range(300):
        if rt.counters.redis_errors:
            break
        await asyncio.sleep(0.01)
    r.fail = False
    for _ in range(300):
        if rt.counters.resubscribes:
            break
        await asyncio.sleep(0.01)
    assert rt.counters.resubscribes >= 1 and rt.counters.continuity_resets >= 1
    rt.shutdown.set()
    task.cancel()


async def test_rankings_snapshot_and_scope(s: Settings) -> None:
    r = FakeRedis()
    rt = runtime(s, r, ist(11, 0))
    rt.handle_packet(packet(A, "105", ist(10, 59), 10))  # +5 %
    rt.handle_packet(packet(B, "190", ist(10, 59), 10))  # -5 %
    # C never traded: no quote.
    assert await rt.compute_rankings(ist(11, 0)) == 1
    snap = json.loads(r.kv[ml.MOVERS_KEY.format(universe="fno_stocks")])
    assert snap["universe_size"] == 3 and snap["subscribed"] == 2 and len(snap["rows"]) == 3
    event = json.loads(next(m for ch, m in r.published if ch == ml.CH_RANKINGS))
    assert [x["instrument_id"] for x in event["top"]["gainers"]] == [1]
    assert [x["instrument_id"] for x in event["top"]["losers"]] == [2]
    assert event["coverage"]["no_data"] == 1 and event["coverage"]["with_quote"] == 2
    assert event["scope"]["market_wide"] is False and "fno_stocks" in event["scope"]["label"]


def test_market_wide_only_for_every_nse_equity() -> None:
    cov = lm.MoverCoverage(universe_size=3, with_quote=3)
    assert ml.scope({"universe": "nse_equities"}, cov)["market_wide"] is True
    assert ml.scope({"universe": "nse_equities", "capped": True}, cov)["market_wide"] is False
    assert ml.scope({"universe": "fno_stocks"}, cov)["market_wide"] is False
    cov.with_quote = 2
    assert ml.scope({"universe": "nse_equities"}, cov)["market_wide"] is False


def stored_15m(days: list[date]) -> list[screener_svc.Bar]:
    out = []
    px = 100.0
    for d in days:
        for k in range(25):
            px += 0.05
            t = ist(9, 15, day=d) + timedelta(minutes=15 * k)
            out.append(
                screener_svc.Bar(
                    t,
                    D(f"{px:.2f}"),
                    D(f"{px + 0.2:.2f}"),
                    D(f"{px - 0.2:.2f}"),
                    D(f"{px:.2f}"),
                    D(1000),
                )
            )
    return out


async def test_live_screener_uses_the_forming_bar_and_announces_changes(s: Settings) -> None:
    r = FakeRedis()
    rt = runtime(s, r, ist(9, 20))
    days = [date(2026, 10, d) for d in (20, 21, 22, 23, 26, 27, 28, 29, 30)]
    rt.history[Timeframe.M15] = {A: stored_15m(days), B: stored_15m(days[:-1])}
    rt.history_at[Timeframe.M15] = ist(9, 20)
    rt.handle_packet(packet(A, "102", ist(9, 16), 100))
    rt.handle_packet(packet(B, "102", ist(9, 16), 100))
    await rt.compute_screener(ist(9, 20))
    snap = json.loads(r.kv[ml.SCREENER_KEY.format(tf="15m")])
    rows = {x["ref"]: x for x in snap["rows"]}
    assert rows[A]["state"] != "UNAVAILABLE" and rows[A]["candle_closed"] is False
    assert rows[A]["basis"] == "live" and D(rows[A]["last_price"]) == D("102")
    # B's stored history stops on Thursday: Friday is missing → unavailable, never bridged.
    assert rows[B]["state"] == "UNAVAILABLE" and rows[B]["history_gap"] is True
    assert rows[C]["state"] == "UNAVAILABLE"
    # First evaluation sets the baseline; only a change on a later one is announced.
    assert not [m for ch, m in r.published if ch == ml.CH_SCREENER]
    first = rows[A]["state"]
    await rt.compute_screener(ist(9, 20, 30))  # nothing new: no event
    assert not [m for ch, m in r.published if ch == ml.CH_SCREENER]
    rt.handle_packet(packet(A, "90", ist(9, 17), 200))  # a sharp drop on the forming bar
    await rt.compute_screener(ist(9, 21))
    second = {x["ref"]: x for x in json.loads(r.kv[ml.SCREENER_KEY.format(tf="15m")])["rows"]}
    events = [json.loads(m) for ch, m in r.published if ch == ml.CH_SCREENER]
    assert bool(events) == (second[A]["state"] != first)
    for ev in events:
        assert ev["basis"] == "live" and ev["changes"][0]["previous_state"] == first
    # Nothing was written anywhere a confirmed signal is read from.
    assert not any(k.startswith("trading") for k in r.kv)


async def test_universe_is_published_for_the_feed(s: Settings) -> None:
    r = FakeRedis()
    rt = runtime(s, r, ist(10, 0))
    await rt.publish_universe()
    assert r.sets[fh.UNIVERSE_KEY] == {A.encode(), B.encode(), C.encode()}


async def test_restart_restores_todays_bars_as_partial(s: Settings) -> None:
    r = FakeRedis()
    rt = runtime(s, r, ist(10, 0))
    rt.handle_packet(packet(A, "101", ist(10, 0, 1), 10))
    rt.handle_packet(packet(A, "101.5", ist(10, 0, 30), 15))
    rt.handle_packet(packet(A, "102", ist(10, 1, 1), 20))
    await rt.flush(ist(10, 1, 2))
    again = runtime(s, r, ist(10, 1, 30))
    assert await again.restore(ist(10, 1, 30)) > 0
    assert again.book.get(A, Timeframe.M1).partial is True  # type: ignore[union-attr]
    assert again.live_closed[(A, Timeframe.M1)][0].ts == ist(10, 0)
    # A late copy of the closed minute is not re-opened.
    again.handle_packet(packet(A, "99", ist(10, 0, 50), 16))
    assert again.book.counters.late >= 1


async def test_chart_rows_append_live_bars_after_stored_history(s: Settings) -> None:
    stored = stored_15m([FRI])
    book = lm.CandleBook([Timeframe.M15])
    book.apply(A, ltt=ist(9, 16), ltp=D("103"), cum_volume=D(10))
    closed = [b for b in book.apply(A, ltt=ist(9, 31), ltp=D("104"), cum_volume=D(30)) if b.closed]
    closed += book.close_due(ist(9, 50))  # 09:15 closed by the packet, 09:30 by the clock

    class Row:
        def __init__(self, b: screener_svc.Bar) -> None:
            self.ts, self.open, self.high, self.low = b.ts, b.open, b.high, b.low
            self.close, self.volume, self.source = b.close, b.volume, "broker"

    live_closed = [b for b in [*closed] if b.closed]
    rows = ml.chart_rows([Row(b) for b in stored], live_closed, None, Timeframe.M15, ist(9, 50))
    assert [(x["ts"], x["source"]) for x in rows[-2:]] == [
        (ist(9, 15), "ticks"),
        (ist(9, 30), "ticks"),
    ]
    assert rows[-3]["ts"] == stored[-1].ts and rows[-3]["source"] == "broker"
    assert rows[-2]["partial"] is True and rows[-1]["partial"] is False
    ov = lm.chart_overlays(rows, Timeframe.M15)
    assert len(ov["ema_fast"]) == len(rows)


async def test_screener_live_overlay_never_bridges_a_gap(s: Settings) -> None:
    r = FakeRedis()
    rt = runtime(s, r, ist(9, 20))
    for key in (A, B, C):
        rt.handle_packet(packet(key, "103", ist(9, 16), 10))
    for key in (A, B):  # a second packet: the forming bar now has a known volume
        rt.handle_packet(packet(key, "103.5", ist(9, 17), 12))
    await rt.flush(ist(9, 20))
    bars = {1: stored_15m([FRI]), 2: stored_15m([date(2026, 10, 29)]), 3: stored_15m([FRI])}
    n = await screener_svc.overlay_live(r, {1: A, 2: B, 3: C}, bars, Timeframe.M15, ist(9, 20))
    # A: Friday's stored bars reach the session end → today's forming bar is appended.
    # B: Friday is missing between the stored bars and today → not overlaid (no bridging).
    # C: its only live bar has unknown volume (the baseline packet) → not overlaid.
    assert n == 1
    assert bars[1][-1].ts == ist(9, 15) and bars[1][-1].close == D("103.5")
    assert bars[1][-1].volume == D(2)
    assert bars[2][-1].ts.astimezone(IST).date() == date(2026, 10, 29)
    assert bars[3][-1].ts.astimezone(IST).date() == FRI
    r.fail = True
    assert await screener_svc.overlay_live(r, {1: A}, bars, Timeframe.M15, ist(9, 20)) == 0
