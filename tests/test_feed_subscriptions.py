"""Managed Dhan feed subscriptions (DHAN_FEED_DYNAMIC, docs/38 §2).

One upstream socket for everyone: browsers register demand in Redis, the market-live worker
publishes its universe, and the feed worker subscribes the union (static first) on its single
connection, sending only the difference and re-subscribing everything after a reconnect.
No network, no real Redis (tests/fake_redis.py, the scripted FakeWS from test_dhan_unit).
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from pydantic import SecretStr

from app.agents.trading.dhan.feed import FeedPacket
from app.core.config import Settings
from app.services import dhan_feed_health as fh
from app.workers import dhan_feed as w
from tests.fake_redis import FakeRedis
from tests.test_dhan_feed_reliability import CLIENT, TOKEN, runtime, until
from tests.test_dhan_unit import FakeWS, make_feed, ticker


@pytest.fixture
def s(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "DHAN_CLIENT_ID": CLIENT,
            "DHAN_ACCESS_TOKEN": SecretStr(TOKEN),
            "DHAN_FEED_INSTRUMENTS": ["NSE_EQ:3499"],
            "DHAN_FEED_MAX_BACKOFF_SECONDS": 0.05,
            "DHAN_FEED_MODE": "ticker",
            "DHAN_FEED_DYNAMIC": True,
        }
    )


def codes(ws: FakeWS) -> list[tuple[int, list[str]]]:
    out = []
    for m in ws.sent:
        msg = json.loads(m)
        ids = [f"{i['ExchangeSegment']}:{i['SecurityId']}" for i in msg.get("InstrumentList", [])]
        out.append((msg["RequestCode"], ids))
    return out


# --- planning ---------------------------------------------------------------------------------


def test_plan_priority_dedupe_cap_and_invalid() -> None:
    plan = w.plan_subscriptions(
        [("NSE_EQ", "1")],
        ["NSE_EQ:2", "NSE_EQ:1", "bad", "NSE_EQ:x"],
        ["NSE_EQ:3", "NSE_EQ:2", "NSE_EQ:4"],
        cap=3,
    )
    assert plan.wanted == [("NSE_EQ", "1"), ("NSE_EQ", "2"), ("NSE_EQ", "3")]
    assert (plan.static, plan.demand, plan.universe) == (1, 1, 1)
    assert plan.over_capacity == 1 and plan.invalid == 2


def test_browser_demand_outranks_the_universe_at_capacity() -> None:
    universe = [f"NSE_EQ:{i}" for i in range(100, 110)]
    plan = w.plan_subscriptions([], ["NSE_EQ:7"], universe, cap=5)
    assert ("NSE_EQ", "7") in plan.wanted and len(plan.wanted) == 5


# --- the socket -------------------------------------------------------------------------------


async def test_changes_send_only_the_difference_on_the_open_socket() -> None:
    ws = FakeWS([ticker()])
    feed, _ = make_feed([ws])
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    await until(lambda: feed.stats.connected and feed.stats.packets >= 1)
    added, removed = await feed.set_instruments([("NSE_EQ", "3499"), ("NSE_EQ", "1333")])
    assert (added, removed) == (1, 0)
    added, removed = await feed.set_instruments([("NSE_EQ", "1333")])
    assert (added, removed) == (0, 1)
    assert await feed.set_instruments([("NSE_EQ", "1333")]) == (0, 0)  # nothing to send
    # ticker: subscribe 15, unsubscribe 16
    assert codes(ws) == [
        (15, ["NSE_EQ:3499"]),
        (15, ["NSE_EQ:1333"]),
        (16, ["NSE_EQ:3499"]),
    ]
    assert feed.stats.subscribed == 1 and feed.stats.subscription_changes == 2
    stop.set()
    await asyncio.wait_for(task, 3)


async def test_a_reconnect_resubscribes_the_whole_current_set_once() -> None:
    ws1 = FakeWS([ticker(), ConnectionError("network drop")])
    ws2 = FakeWS([ticker(ltp=150.0, ltt=1793350009)])
    feed, urls = make_feed([ws1, ws2])
    stop = asyncio.Event()
    # Changed while disconnected (before the first connect): applied on the next socket.
    await feed.set_instruments([("NSE_EQ", "3499"), ("NSE_EQ", "1333")])
    task = asyncio.create_task(feed.run(stop))
    await until(lambda: len(urls) == 2 and feed.stats.connected)
    for ws in (ws1, ws2):
        subs = [c for c in codes(ws) if c[0] == 15]
        assert subs == [(15, ["NSE_EQ:3499", "NSE_EQ:1333"])]  # one message, full set, no dupes
    stop.set()
    await asyncio.wait_for(task, 3)


async def test_more_than_100_changes_are_split_into_messages() -> None:
    ws = FakeWS([ticker()])
    feed, _ = make_feed([ws])
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    await until(lambda: feed.stats.connected)
    many = [("NSE_EQ", str(1000 + i)) for i in range(250)]
    await feed.set_instruments([("NSE_EQ", "3499"), *many])
    sizes = [len(ids) for code, ids in codes(ws)[1:]]
    assert sizes == [100, 100, 50]
    stop.set()
    await asyncio.wait_for(task, 3)


# --- the worker -------------------------------------------------------------------------------


async def test_demand_and_universe_reach_the_single_socket(s: Settings) -> None:
    r = FakeRedis()
    ws = FakeWS([ticker()])
    rt, stop, urls, _ = runtime(s, r, [ws])
    task = asyncio.create_task(rt.run())
    await until(lambda: rt.feed is not None and rt.feed.stats.connected)
    now = time.time()
    await r.zadd(fh.DEMAND_KEY, {"NSE_EQ:1333": now + 60, "NSE_EQ:11536": now - 1})  # 2nd expired
    await r.sadd(fh.UNIVERSE_KEY, "NSE_EQ:2885", "NSE_EQ:3499")
    await until(lambda: len(rt.instruments) == 3)
    assert rt.instruments == [("NSE_EQ", "3499"), ("NSE_EQ", "1333"), ("NSE_EQ", "2885")]
    assert rt.sink is not None and rt.sink.subscribed == {
        "NSE_EQ:3499",
        "NSE_EQ:1333",
        "NSE_EQ:2885",
    }
    assert len(urls) == 1  # no new upstream connection for new watchers
    await until(lambda: b"NSE_EQ:11536" not in r.zsets[fh.DEMAND_KEY])  # leader purges expired
    await until(lambda: json.loads(r.kv.get(fh.STATUS_KEY, b"{}")).get("desired") == 3)
    st = json.loads(r.kv[fh.STATUS_KEY])
    assert st["subscriptions"]["demand"] == 1 and st["subscriptions"]["universe"] == 1
    assert st["subscriptions"]["static"] == 1 and st["dynamic"] is True
    # The watcher leaves (demand expires): unsubscribed, the static one stays.
    await r.zadd(fh.DEMAND_KEY, {"NSE_EQ:1333": time.time() - 1})
    await until(lambda: ("NSE_EQ", "1333") not in rt.instruments)
    assert (16, ["NSE_EQ:1333"]) in codes(ws)
    stop.set()
    await asyncio.wait_for(task, 3)


async def test_redis_outage_keeps_the_current_subscriptions(s: Settings) -> None:
    r = FakeRedis()
    rt, stop, _, _ = runtime(s, r, [FakeWS([ticker()])])
    task = asyncio.create_task(rt.run())
    await until(lambda: rt.feed is not None and rt.feed.stats.connected)
    await r.sadd(fh.UNIVERSE_KEY, "NSE_EQ:2885")
    await until(lambda: len(rt.instruments) == 2)
    r.fail = True
    before = rt.reconcile_errors
    assert not await rt.reconcile()
    assert rt.reconcile_errors == before + 1 and len(rt.instruments) == 2  # nothing dropped
    r.fail = False
    stop.set()
    await asyncio.wait_for(task, 3)


async def test_no_socket_until_something_is_wanted(s: Settings) -> None:
    r = FakeRedis()
    rt, stop, urls, _ = runtime(
        s.model_copy(update={"DHAN_FEED_INSTRUMENTS": [], "DHAN_FEED_RECONCILE_SECONDS": 1}),
        r,
        [FakeWS([ticker(sid=1333)])],
    )
    task = asyncio.create_task(rt.run())
    await until(lambda: rt.state_name == "waiting_demand")
    await asyncio.sleep(0.05)
    assert urls == []
    status = json.loads(r.kv[fh.STATUS_KEY])
    health = fh.assess(rt.settings, status, None)
    assert health["status"] in ("degraded", "market_closed")
    assert "no_subscriptions" in health["codes"]
    await r.zadd(fh.DEMAND_KEY, {"NSE_EQ:1333": time.time() + 60})
    await until(lambda: len(urls) == 1 and rt.feed is not None and rt.feed.stats.connected)
    stop.set()
    await asyncio.wait_for(task, 3)


async def test_static_only_when_dynamic_is_off(s: Settings) -> None:
    r = FakeRedis()
    rt, _stop, _, _ = runtime(s.model_copy(update={"DHAN_FEED_DYNAMIC": False}), r, [FakeWS([])])
    await r.sadd(fh.UNIVERSE_KEY, "NSE_EQ:2885")
    rt.instruments = [("NSE_EQ", "3499")]
    assert not await rt.reconcile()
    assert rt.instruments == [("NSE_EQ", "3499")]
    assert (
        w.config_problem(
            s.model_copy(update={"DHAN_FEED_DYNAMIC": False, "DHAN_FEED_INSTRUMENTS": []})
        )
        is not None
    )
    assert w.config_problem(s.model_copy(update={"DHAN_FEED_INSTRUMENTS": []})) is None


async def test_prev_close_records_when_it_was_received() -> None:
    r = FakeRedis()
    sink = w.FeedSink(r, ["NSE_EQ:3499"], 60.0)
    p = FeedPacket(kind="prev_close", segment="NSE_EQ", security_id="3499")
    from decimal import Decimal

    p.prev_close, p.prev_oi = Decimal("140.5"), 0
    assert await sink.accept(p)
    h = r.hashes["dhan:tick:NSE_EQ:3499"]
    assert h[b"prev_close"] == b"140.5" and b"prev_close_received_at" in h
    published: dict[str, Any] = json.loads(r.published[-1][1])
    assert published["kind"] == "prev_close" and "received_at" in published
