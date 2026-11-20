"""The browser fan-out hub (app/services/live_hub.py, docs/38 §5): routing, snapshots,
backpressure, slow consumers, Redis interruptions, demand and limits. No network; Redis is
tests/fake_redis.py and a "socket" is a list the sender appends to.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.core.config import Settings
from app.services import dhan_feed_health as fh
from app.services import live_hub as hub_mod
from app.services import market_live as ml
from app.services.live_hub import Connection, LiveHub, Topic
from tests.fake_redis import FakeRedis


@pytest.fixture
def s(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"MARKET_WS_MAX_CONNECTIONS_PER_USER": 2, "DHAN_FEED_DEMAND_TTL_SECONDS": 30}
    )


class Sock:
    def __init__(self, delay: float = 0.0) -> None:
        self.out: list[dict[str, Any]] = []
        self.delay = delay

    async def send(self, text: str) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.out.append(json.loads(text))


def conn(
    sock: Sock, *, user: str = "u1", outbox: int = 50, timeout: float = 1.0, topics: int = 10
) -> Connection:
    return Connection(user, sock.send, outbox_max=outbox, send_timeout=timeout, max_topics=topics)


QUOTE = Topic("quote", "quote:1", "quote:NSE_EQ:1", instrument_id=1, ref="NSE_EQ:1")
CANDLE = Topic(
    "candle", "candle:1:5m", "candle:5m:NSE_EQ:1", instrument_id=1, ref="NSE_EQ:1", tf="5m"
)
RANK = Topic("rankings", "rankings:fno_stocks", "rankings:fno_stocks", universe="fno_stocks")


async def until(cond: Any, within: float = 3.0) -> None:
    for _ in range(int(within / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def snap(t: Topic) -> dict[str, Any]:
    return {"available": True, "data": {"n": 0}}


# --- one connection: coalescing, order, slow consumers --------------------------------------


async def test_quotes_coalesce_and_ordered_messages_go_first() -> None:
    sock = Sock()
    c = conn(sock)
    for i in range(5):
        c.offer({"type": "quote", "ltp": str(i)}, coalesce="quote:quote:1")
    c.offer({"type": "screener", "n": 1})
    task = asyncio.create_task(c.run_sender())
    await until(lambda: len(sock.out) == 2)
    assert [m["type"] for m in sock.out] == ["screener", "quote"]
    assert sock.out[1]["ltp"] == "4" and c.coalesced == 4  # only the newest quote is sent
    assert [m["seq"] for m in sock.out] == [1, 2]
    c.close("test")
    await task


async def test_a_closed_bar_supersedes_its_queued_forming_copy() -> None:
    sock = Sock()
    c = conn(sock)
    ts = "2026-11-02T04:30:00+00:00"
    c.offer(
        {"type": "candle", "topic": "candle:1:5m", "ts": ts, "closed": False, "close": "1"},
        coalesce="candle:candle:1:5m",
    )
    c.offer({"type": "candle", "topic": "candle:1:5m", "ts": ts, "closed": True, "close": "2"})
    task = asyncio.create_task(c.run_sender())
    await until(lambda: len(sock.out) == 1)
    await asyncio.sleep(0.02)
    assert len(sock.out) == 1 and sock.out[0]["closed"] is True  # never a stale forming copy after
    c.close("test")
    await task


async def test_a_full_outbox_disconnects_the_slow_consumer() -> None:
    c = conn(Sock(), outbox=3)
    for i in range(3):
        assert c.offer({"type": "screener", "n": i})
    assert not c.offer({"type": "screener", "n": 3})
    assert c.closed.is_set() and c.close_reason == "slow_consumer"
    # Coalesced topics never overflow: a slow client just gets the newest value.
    c2 = conn(Sock(), outbox=3)
    for i in range(1000):
        assert c2.offer({"type": "quote", "ltp": str(i)}, coalesce="quote:quote:1")
    assert not c2.closed.is_set() and len(c2.latest) == 1


async def test_a_blocked_send_disconnects_the_slow_consumer() -> None:
    c = conn(Sock(delay=1.0), timeout=0.05)
    c.offer({"type": "hello"})
    await asyncio.wait_for(c.run_sender(), 2)
    assert c.close_reason == "slow_consumer"


# --- the hub ---------------------------------------------------------------------------------


async def test_routing_snapshot_then_updates_and_unsubscribe(s: Settings) -> None:
    r = FakeRedis()
    hub = LiveHub(r, s, snapshot=snap)
    a_sock, b_sock = Sock(), Sock()
    a, b = conn(a_sock), conn(b_sock, user="u2")
    assert await hub.add(a) and await hub.add(b)
    tasks = [asyncio.create_task(x.run_sender()) for x in (a, b)]
    accepted, rejected = await hub.subscribe(a, [QUOTE, RANK])
    assert accepted == ["quote:1", "rankings:fno_stocks"] and rejected == []
    await hub.subscribe(b, [RANK])
    n = hub.dispatch(ml.CH_QUOTES, json.dumps({"type": "quote", "key": "NSE_EQ:1", "ltp": "5"}))
    assert n == 1  # only the quote subscriber
    hub.dispatch(ml.CH_RANKINGS, json.dumps({"type": "rankings", "universe": "fno_stocks"}))
    hub.dispatch(ml.CH_QUOTES, json.dumps({"type": "quote", "key": "NSE_EQ:9", "ltp": "1"}))
    await until(lambda: len(a_sock.out) == 4 and len(b_sock.out) == 2)
    assert [(m["type"], m["topic"]) for m in a_sock.out] == [
        ("snapshot", "quote:1"),
        ("snapshot", "rankings:fno_stocks"),
        ("quote", "quote:1"),
        ("rankings", "rankings:fno_stocks"),
    ]
    assert a_sock.out[2]["instrument_id"] == 1
    assert await hub.unsubscribe(a, ["quote:1"]) == ["quote:1"]
    assert hub.dispatch(ml.CH_QUOTES, json.dumps({"type": "quote", "key": "NSE_EQ:1"})) == 0
    for x in (a, b):
        x.close("test")
    await asyncio.gather(*tasks)


async def test_updates_during_a_snapshot_are_held_until_after_it(s: Settings) -> None:
    r = FakeRedis()
    gate = asyncio.Event()
    hub: LiveHub

    async def slow_snapshot(t: Topic) -> dict[str, Any]:
        # An update is published while the snapshot is being read.
        hub.dispatch(ml.CH_QUOTES, json.dumps({"type": "quote", "key": "NSE_EQ:1", "ltp": "7"}))
        await gate.wait()
        return {"available": True, "data": {"ltp": "6"}}

    hub = LiveHub(r, s, snapshot=slow_snapshot)
    sock = Sock()
    c = conn(sock)
    await hub.add(c)
    task = asyncio.create_task(c.run_sender())
    sub = asyncio.create_task(hub.subscribe(c, [QUOTE]))
    await asyncio.sleep(0.05)
    assert sock.out == []  # the update did not overtake the snapshot
    gate.set()
    await sub
    await until(lambda: len(sock.out) == 2)
    assert [m["type"] for m in sock.out] == ["snapshot", "quote"]
    c.close("test")
    await task


async def test_topic_limit(s: Settings) -> None:
    hub = LiveHub(FakeRedis(), s, snapshot=snap)
    c = conn(Sock(), topics=1)
    await hub.add(c)
    accepted, rejected = await hub.subscribe(c, [QUOTE, CANDLE])
    assert accepted == ["quote:1"] and rejected == [
        {"topic": "candle:1:5m", "reason": "topic_limit"}
    ]


async def test_redis_interruption_tells_clients_and_resyncs(s: Settings) -> None:
    r = FakeRedis()
    calls: list[str] = []

    async def counting(t: Topic) -> dict[str, Any]:
        calls.append(t.name)
        return {"available": True}

    hub = LiveHub(r, s, snapshot=counting, retry_unit=0.01)
    sock = Sock()
    c = conn(sock)
    await hub.add(c)
    sender = asyncio.create_task(c.run_sender())
    await hub.subscribe(c, [QUOTE])
    hub.start()
    await until(lambda: hub.redis_ok is True)
    await r.publish(ml.CH_QUOTES, json.dumps({"type": "quote", "key": "NSE_EQ:1", "ltp": "1"}))
    await until(lambda: any(m["type"] == "quote" for m in sock.out))
    r.fail = True
    await until(lambda: hub.redis_ok is False)
    await until(lambda: any(m["type"] == "status" and m["redis"] == "down" for m in sock.out))
    r.fail = False
    await until(lambda: hub.redis_ok is True and hub.resyncs == 1)
    await until(lambda: any(m["type"] == "status" and m["redis"] == "ok" for m in sock.out))
    ok_at = next(i for i, m in enumerate(sock.out) if m["type"] == "status" and m["redis"] == "ok")
    await until(lambda: len(sock.out) > ok_at + 1)
    assert sock.out[ok_at]["resync"] is True and sock.out[ok_at + 1]["type"] == "snapshot"
    assert calls == ["quote:1", "quote:1"]  # once on subscribe, once on resync
    await hub.stop()
    await sender


async def test_watched_instruments_become_feed_demand(s: Settings) -> None:
    r = FakeRedis()
    hub = LiveHub(r, s, snapshot=snap, retry_unit=0.01)
    c = conn(Sock())
    await hub.add(c)
    await hub.subscribe(c, [QUOTE, CANDLE, RANK])
    assert set(r.zsets[fh.DEMAND_KEY]) == {b"NSE_EQ:1"}  # immediately, once per instrument
    assert hub.watched_refs() == ["NSE_EQ:1"]
    await hub.remove(c)
    assert hub.watched_refs() == [] and hub.routes == {}
    # Redis down: subscribing still works (demand is retried by the refresh loop).
    r.fail = True
    c2 = conn(Sock(), user="u9")
    await hub.add(c2)
    accepted, _ = await hub.subscribe(c2, [QUOTE])
    assert accepted == ["quote:1"]


async def test_connections_per_user_are_limited(s: Settings) -> None:
    r = FakeRedis()
    hub = LiveHub(r, s, snapshot=snap)
    a, b, c = conn(Sock()), conn(Sock()), conn(Sock())
    assert await hub.add(a) and await hub.add(b)
    assert not await hub.add(c)  # MARKET_WS_MAX_CONNECTIONS_PER_USER = 2
    assert await hub.add(conn(Sock(), user="someone-else"))
    await hub.remove(a)
    assert await hub.add(c)
    # The count is shared through Redis: another API process sees this user's two sockets.
    other = LiveHub(r, s, snapshot=snap)
    assert not await other.add(conn(Sock()))
    # Redis down: the process's own count still applies.
    r.fail = True
    assert not await hub.add(conn(Sock()))


async def test_default_snapshots_read_what_the_worker_wrote(s: Settings) -> None:
    r = FakeRedis()
    hub = LiveHub(r, s)
    await r.set(
        ml.QUOTE_KEY.format(ref="NSE_EQ:1"),
        json.dumps(
            {
                "key": "NSE_EQ:1",
                "ltp": "10",
                "received_at": "2026-11-02T04:30:00+00:00",
                "status": "live",
                "change_pct": "1.0000",
            }
        ),
    )
    got = await hub.default_snapshot(QUOTE)
    assert got["available"] and got["data"]["ltp"] == "10"
    missing = await hub.default_snapshot(RANK)
    assert missing == {"available": False, "universe": "fno_stocks"}
    candle = await hub.default_snapshot(CANDLE)
    assert candle["available"] is False and candle["closed"] == []


def test_route_of() -> None:
    assert hub_mod.route_of(ml.CH_CANDLES, {"timeframe": "5m", "key": "NSE_EQ:1"}) == CANDLE.route
    assert hub_mod.route_of(ml.CH_SCREENER, {"timeframe": "15m"}) == "screener:15m"
    assert hub_mod.route_of("other", {}) is None
