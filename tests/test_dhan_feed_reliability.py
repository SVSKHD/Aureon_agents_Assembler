"""Dhan feed reliability (app/workers/dhan_feed.py, app/services/dhan_feed_health.py).

No network, no real Redis: the WebSocket is a scripted fake (tests/test_dhan_unit.py) and
Redis is tests/fake_redis.py, which can be taken down and brought back. Scenarios from the
brief: normal connection, Redis disconnect/recovery, WebSocket disconnect/recovery, crash and
restart, credential expiry/replacement, duplicate subscriptions, stale ticks, restart with
persisted state, market closed.
"""

from __future__ import annotations

import asyncio
import json
import struct
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr

from app.agents.trading.dhan.feed import FeedPacket, token_fp
from app.core.config import Settings
from app.services import dhan_feed_control as ctl
from app.services import dhan_feed_health as fh
from app.services.research import TickAggregator
from app.workers import dhan_feed as w
from tests.fake_redis import FakeRedis
from tests.test_dhan_unit import FakeWS, hdr, ticker

CLIENT = "1100003626"
TOKEN = "manual-token-" + "a" * 40
TOKEN2 = "manual-token-" + "b" * 40
KEY = "NSE_EQ:3499"
# 10:00 IST on Monday 2026-11-02 (market open); Saturday 2026-11-07 (closed).
OPEN = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
SATURDAY = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)


@pytest.fixture
def s(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "DHAN_CLIENT_ID": CLIENT,
            "DHAN_ACCESS_TOKEN": SecretStr(TOKEN),
            "DHAN_FEED_INSTRUMENTS": [KEY],
            "DHAN_FEED_MAX_BACKOFF_SECONDS": 0.05,
            "DHAN_FEED_MODE": "ticker",
        }
    )


class FakeRegistry:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []

    async def log(self, agent_id: str, level: str, message: str) -> None:
        self.logs.append((level, message))


def pkt(
    ltp: str = "142.85",
    ltt: datetime | None = None,
    sid: str = "3499",
    seg: str | None = "NSE_EQ",
) -> FeedPacket:
    return FeedPacket(
        kind="ticker",
        segment=seg,
        security_id=sid,
        ltp=Decimal(ltp),
        ltt=ltt or datetime(2026, 11, 2, 4, 29, tzinfo=UTC),
    )


async def until(cond: Any, within: float = 3.0) -> None:
    for _ in range(int(within / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def runtime(
    s: Settings, redis: FakeRedis, sockets: list[FakeWS] | None = None
) -> tuple[w.FeedRuntime, asyncio.Event, list[str], FakeRegistry]:
    urls: list[str] = []
    it = iter(sockets or [])

    async def connector(url: str) -> FakeWS:
        urls.append(url)
        try:
            return next(it)
        except StopIteration:
            return FakeWS([])

    stop = asyncio.Event()
    reg = FakeRegistry()
    rt = w.FeedRuntime(s, redis, reg, stop, connector=connector, sleep_unit=0.01)  # type: ignore[arg-type]
    return rt, stop, urls, reg


# --- validation, duplicates, ordering -------------------------------------------------------


def test_packets_are_validated() -> None:
    subs = {KEY}
    now = OPEN
    assert w.validate(pkt(), subs, now) is None
    assert w.validate(pkt(seg=None), subs, now) == "unknown_segment"
    assert w.validate(pkt(sid="1"), subs, now) == "unsubscribed"
    assert w.validate(pkt(ltt=now + timedelta(hours=1)), subs, now) == "bad_time"
    assert w.validate(pkt(ltt=datetime(1999, 1, 1, tzinfo=UTC)), subs, now) == "bad_time"
    assert w.validate(pkt(ltp="-1"), subs, now) == "bad_price"
    assert w.validate(pkt(ltp="NaN"), subs, now) == "bad_price"


async def test_duplicate_and_out_of_order_ticks_are_not_stored_twice() -> None:
    r = FakeRedis()
    sink = w.FeedSink(r, [KEY], 30)  # type: ignore[arg-type]
    t0 = datetime(2026, 11, 2, 4, 29, tzinfo=UTC)
    assert await sink.accept(pkt(ltt=t0), OPEN)
    assert not await sink.accept(pkt(ltt=t0), OPEN)  # Dhan re-sends a snapshot
    assert await sink.accept(pkt("143", t0 + timedelta(seconds=1)), OPEN)
    assert not await sink.accept(pkt("141", t0 - timedelta(seconds=5)), OPEN)  # late
    assert not await sink.accept(pkt(sid="999"), OPEN)
    assert sink.duplicates == 1 and sink.out_of_order == 1 and sink.invalid == {"unsubscribed": 1}
    ticks = [json.loads(d) for c, d in r.published if c == w.CHANNEL]
    assert [t["ltp"] for t in ticks] == ["142.85", "143"]
    assert (await r.hgetall(f"dhan:tick:{KEY}"))[b"ltp"] == b"143"


# --- Redis disconnect and recovery ----------------------------------------------------------


async def test_redis_outage_holds_newest_and_recovers() -> None:
    r = FakeRedis()
    sink = w.FeedSink(r, [KEY, "NSE_EQ:1594"], 30)  # type: ignore[arg-type]
    t0 = datetime(2026, 11, 2, 4, 29, tzinfo=UTC)
    r.fail = True
    for i in range(5):
        assert not await sink.accept(pkt(str(140 + i), t0 + timedelta(seconds=i)), OPEN)
    assert not sink.redis_ok and sink.redis_errors >= 1
    assert len(sink.pending) == 1  # newest per instrument, bounded
    assert sink.fresh_count() == 1  # the feed itself is alive: Redis is the problem
    r.fail = False
    sink._retry_at = 0  # the backoff has elapsed
    assert await sink.flush_pending() == 1
    assert sink.redis_ok and not sink.pending
    assert (await r.hgetall(f"dhan:tick:{KEY}"))[b"ltp"] == b"144"


async def test_drain_never_dies() -> None:
    r = FakeRedis()
    sink = w.FeedSink(r, [KEY], 30)  # type: ignore[arg-type]
    calls = {"n": 0}
    real = sink.accept

    async def flaky(p: FeedPacket, now: datetime | None = None) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("unexpected")
        return await real(p, OPEN)

    sink.accept = flaky  # type: ignore[method-assign]

    class Q:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[FeedPacket] = asyncio.Queue()

    feed = Q()
    task = asyncio.create_task(sink.drain(feed))  # type: ignore[arg-type]
    feed.queue.put_nowait(pkt())
    feed.queue.put_nowait(pkt("150", datetime(2026, 11, 2, 4, 29, 30, tzinfo=UTC)))
    await until(lambda: sink.written == 1)
    assert sink.drain_errors == 1 and not task.done()
    task.cancel()


# --- normal connection, status, leadership --------------------------------------------------


async def test_normal_connection_writes_ticks_and_status(s: Settings) -> None:
    r = FakeRedis()
    rt, stop, urls, _ = runtime(s, r, [FakeWS([ticker(), ticker(ltp=143.0, ltt=1793350001)])])
    task = asyncio.create_task(rt.run())
    await until(lambda: rt.sink is not None and rt.sink.written == 2)
    await until(lambda: r.kv.get(fh.STATUS_KEY) is not None)
    status = json.loads(r.kv[fh.STATUS_KEY])
    assert status["state"] == "running" and status["leader"] and status["instruments"] == 1
    assert "token=" in urls[0] and TOKEN not in json.dumps(status)
    stop.set()
    await asyncio.wait_for(task, 3)
    final = json.loads(r.kv[fh.STATUS_KEY])
    assert final["state"] == "stopped"
    assert fh.assess(s, final, None, datetime.now(UTC))["status"] == "not_running"
    assert fh.LEADER_KEY not in r.kv  # released on a clean stop


async def test_second_worker_stands_by_no_duplicate_subscription(s: Settings) -> None:
    r = FakeRedis()
    ws1 = FakeWS([ticker()])
    a, stop_a, urls_a, _ = runtime(s, r, [ws1])
    b, stop_b, urls_b, _ = runtime(s, r, [FakeWS([ticker()])])
    ta = asyncio.create_task(a.run())
    await until(lambda: a.leader and a.feed is not None and a.feed.stats.connected)
    tb = asyncio.create_task(b.run())
    await until(lambda: b.state_name == "standby")
    await asyncio.sleep(0.1)
    assert urls_b == [] and len(urls_a) == 1  # one socket, one subscription set
    await asyncio.sleep(0.1)
    assert json.loads(r.kv[fh.STATUS_KEY])["state"] == "running"  # standby never overwrites
    assert sum(1 for m in ws1.sent if json.loads(m).get("RequestCode") == 15) == 1
    # The leader stops; the standby takes over.
    stop_a.set()
    await asyncio.wait_for(ta, 3)
    await until(lambda: b.leader and len(urls_b) == 1)
    stop_b.set()
    await asyncio.wait_for(tb, 3)


# --- WebSocket disconnect and recovery; reconnect requests ---------------------------------


async def test_reconnect_request_resubscribes_once_and_is_rate_limited(s: Settings) -> None:
    r = FakeRedis()
    ws1, ws2 = FakeWS([ticker()]), FakeWS([ticker(ltp=150.0, ltt=1793350009)])
    rt, stop, urls, _ = runtime(s, r, [ws1, ws2])
    task = asyncio.create_task(rt.run())
    await until(lambda: rt.feed is not None and rt.feed.stats.packets >= 1)
    assert rt.handle_control(json.dumps({"cmd": "reconnect"})) == "reconnect"
    await until(lambda: len(urls) == 2 and rt.feed is not None and rt.feed.stats.packets >= 2)
    assert json.loads(ws1.sent[-1]) == {"RequestCode": 12}  # clean disconnect first
    assert sum(1 for m in ws2.sent if json.loads(m).get("RequestCode") == 15) == 1
    assert rt.handle_control(json.dumps({"cmd": "reconnect"})) == "ignored_cooldown"
    assert rt.handle_control("not json") == "ignored_bad_message"
    assert rt.handle_control(json.dumps({"cmd": "place_order"})) == "ignored_unknown"
    stop.set()
    await asyncio.wait_for(task, 3)


async def test_control_channel_reaches_the_worker(s: Settings) -> None:
    r = FakeRedis()
    rt, stop, urls, _ = runtime(s, r, [FakeWS([ticker()]), FakeWS([])])
    task = asyncio.create_task(rt.run())
    await until(lambda: rt.feed is not None and rt.feed.stats.packets >= 1)
    await until(lambda: any(fh.CONTROL_CHANNEL in sub.channels for sub in r._subs))
    res = await ctl.request_reconnect(r, s, "test")  # type: ignore[arg-type]
    assert res.accepted and res.receivers == 1
    await until(lambda: len(urls) == 2)
    again = await ctl.request_reconnect(r, s, "test")  # type: ignore[arg-type]
    assert not again.accepted and again.retry_after_s >= 1  # shared cooldown
    stop.set()
    await asyncio.wait_for(task, 3)


async def test_request_reconnect_fails_closed_without_redis(s: Settings) -> None:
    r = FakeRedis()
    r.fail = True
    with pytest.raises(ctl.ControlUnavailable):
        await ctl.request_reconnect(r, s, "x")  # type: ignore[arg-type]
    with pytest.raises(ctl.ControlUnavailable):
        await ctl.request_reconnect(None, s, "x")


# --- credential expiry and replacement ------------------------------------------------------


async def test_auth_failure_waits_for_a_new_token_then_reconnects(s: Settings) -> None:
    r = FakeRedis()
    disc = hdr(50, 10, 0, 0) + struct.pack("<H", 807)  # token expired
    rt, stop, urls, reg = runtime(s, r, [FakeWS([disc]), FakeWS([ticker()])])
    task = asyncio.create_task(rt.run())
    await until(lambda: rt.state_name == "auth_failed")
    await asyncio.sleep(0.1)
    assert len(urls) == 1  # no reconnect storm with the dead token
    assert rt.handle_control(json.dumps({"cmd": "reconnect"})) == "ignored_auth_failed"
    assert any("log in to Dhan" in m for _, m in reg.logs)
    # An admin sets a new manual token (or logs in): the worker notices and reconnects.
    rt.settings = s.model_copy(update={"DHAN_ACCESS_TOKEN": SecretStr(TOKEN2)})
    await until(lambda: len(urls) == 2)
    assert TOKEN2 in urls[1] and TOKEN not in urls[1]
    await until(lambda: rt.feed is not None and rt.feed.stats.packets >= 1)
    assert rt.feed is not None and rt.feed.stats.token_fp == token_fp(TOKEN2)
    stop.set()
    await asyncio.wait_for(task, 3)


async def test_expired_session_falls_back_to_manual_at_reconnect(s: Settings) -> None:
    session = "session-token-" + "s" * 40
    live = s.model_copy(
        update={
            "DHAN_SESSION_TOKEN": SecretStr(session),
            "DHAN_SESSION_EXPIRES_AT": datetime.now(UTC) + timedelta(hours=1),
        }
    )
    r = FakeRedis()
    rt, stop, urls, _ = runtime(live, r, [FakeWS([ticker()]), FakeWS([])])
    task = asyncio.create_task(rt.run())
    await until(lambda: len(urls) == 1)
    assert session in urls[0]
    rt.settings = live.model_copy(
        update={"DHAN_SESSION_EXPIRES_AT": datetime.now(UTC) - timedelta(seconds=1)}
    )
    assert rt.feed is not None
    rt.feed.request_reconnect()
    await until(lambda: len(urls) == 2)
    assert TOKEN in urls[1] and session not in urls[1]
    stop.set()
    await asyncio.wait_for(task, 3)


# --- crashes, restarts, no restart loop -----------------------------------------------------


async def test_unconfigured_worker_stays_up_instead_of_crash_looping(s: Settings) -> None:
    r = FakeRedis()
    rt, stop, urls, reg = runtime(s.model_copy(update={"DHAN_FEED_INSTRUMENTS": []}), r)
    task = asyncio.create_task(rt.run())
    await until(lambda: r.kv.get(fh.STATUS_KEY) is not None)
    await asyncio.sleep(0.05)
    assert not task.done() and urls == []
    assert json.loads(r.kv[fh.STATUS_KEY])["state"] == "waiting_config"
    assert any("DHAN_FEED_INSTRUMENTS" in m for _, m in reg.logs)
    stop.set()
    assert await asyncio.wait_for(task, 3) == 0


def test_config_problems() -> None:
    assert w.startup_delay(4, 5) == 0
    assert w.startup_delay(5, 5) == 15 and w.startup_delay(7, 5) == 60
    assert w.startup_delay(50, 5) == w.MAX_STARTUP_DELAY_S


async def test_crash_loop_guard_and_crash_report_survive_restarts(s: Settings) -> None:
    r = FakeRedis()
    limited = s.model_copy(update={"DHAN_FEED_CRASH_LOOP_STARTS": 3})
    for n in range(1, 3):  # two earlier (crashed) runs of the worker
        rt, *_ = runtime(limited, r)
        assert await rt.record_start() == n
    rt, *_ = runtime(limited, r)
    await rt.report_crash(RuntimeError("boom"))
    crash = json.loads(r.kv[fh.LAST_CRASH_KEY])
    assert crash["error"] == "RuntimeError"
    # The next start is the third within 10 minutes: cooldown before connecting.
    rt, stop, urls, reg = runtime(limited, r, [FakeWS([ticker()])])
    task = asyncio.create_task(rt.run())
    await until(lambda: rt.state_name == "cooldown")
    assert urls == [] and any("crash-loop" in m for _, m in reg.logs)
    await until(lambda: len(urls) == 1, within=5)  # 15 s x 0.01 later it connects
    stop.set()
    await asyncio.wait_for(task, 3)


# --- staleness, market hours, health --------------------------------------------------------


async def test_stale_marking_respects_open_and_holidays() -> None:
    r = FakeRedis()
    sink = w.FeedSink(r, [KEY, "NSE_EQ:1594"], 30, [date(2026, 11, 2)])  # type: ignore[arg-type]
    sink.started = OPEN.timestamp() - 3600
    assert await sink.mark_stale(OPEN) == set()  # holiday: silence is expected
    sink.holidays = []
    sink.last_seen[KEY] = OPEN.timestamp() - 5
    assert await sink.mark_stale(OPEN) == {"NSE_EQ:1594"}
    # Right after the open nothing is stale yet (no ticks are due before 09:15 IST).
    sink2 = w.FeedSink(r, [KEY], 30)  # type: ignore[arg-type]
    sink2.started = OPEN.timestamp() - 7200
    just_open = datetime(2026, 11, 2, 3, 45, 10, tzinfo=UTC)
    assert await sink2.mark_stale(just_open) == set()


def agent(fresh: bool = True, **details: Any) -> dict[str, Any]:
    return {"registered": True, "stale": not fresh, "state": "busy", "details": details}


def status(now: datetime, **kw: Any) -> dict[str, Any]:
    base = {
        "state": "running",
        "connected": True,
        "subscribed": 1,
        "fresh": 1,
        "stale": 0,
        "redis_ok": True,
        "updated_at": now.timestamp() - 2,
        "last_packet_at": now.timestamp() - 1,
    }
    return base | kw


def test_health_is_never_ok_without_fresh_data(s: Settings) -> None:
    ok = fh.assess(s, status(OPEN), agent(), OPEN)
    assert ok["status"] == "ok" and ok["healthy"] and ok["codes"] == []
    cases = {
        "disconnected": status(OPEN, connected=False),
        "no_fresh_data": status(OPEN, fresh=0),
        "stale_instruments": status(OPEN, fresh=1, stale=1),
        "subscriptions_incomplete": status(OPEN, subscribed=0),
        "auth_failed": status(OPEN, auth_failed=True),
        "redis_writes_failing": status(OPEN, redis_ok=False),
    }
    for code, st in cases.items():
        out = fh.assess(s, st, agent(), OPEN)
        assert not out["healthy"] and code in out["codes"], (code, out)


def test_health_market_closed_vs_failure(s: Settings) -> None:
    closed = fh.assess(s, status(SATURDAY, connected=False, fresh=0), agent(), SATURDAY)
    assert closed["status"] == "market_closed" and not closed["healthy"]
    assert closed["market"] == {"open": False, "session": "weekend"}
    holiday = s.model_copy(update={"DHAN_MARKET_HOLIDAYS": [date(2026, 11, 2)]})
    assert fh.assess(holiday, status(OPEN, fresh=0), agent(), OPEN)["status"] == "market_closed"
    # A dead worker is reported even when the market is closed.
    gone = fh.assess(s, None, None, SATURDAY)
    assert gone["status"] == "not_running" and gone["codes"] == ["not_running"]
    auth = fh.assess(s, status(SATURDAY, auth_failed=True), agent(), SATURDAY)
    assert auth["status"] == "down"


def test_health_without_redis_or_status(s: Settings) -> None:
    old = status(OPEN, updated_at=OPEN.timestamp() - 600)
    assert fh.assess(s, old, None, OPEN)["status"] == "not_running"
    unread = fh.assess(s, None, None, OPEN, status_readable=False)
    assert unread["status"] == "unknown" and unread["codes"] == ["status_unreadable"]
    blind = fh.assess(s, None, agent(redis_ok=False), OPEN)
    assert blind["codes"] == ["redis_writes_failing"]
    waiting = fh.assess(s, None, agent(state="waiting_config"), OPEN)
    assert waiting["codes"] == ["worker_not_ready"]
    none = fh.assess(s.model_copy(update={"DHAN_FEED_INSTRUMENTS": []}), None, None, OPEN)
    assert none["status"] == "not_configured"


def test_market_sessions() -> None:
    assert fh.market_state(OPEN)["session"] == "open"
    assert fh.market_state(datetime(2026, 11, 2, 3, 35, tzinfo=UTC))["session"] == "pre_open"
    assert fh.market_state(datetime(2026, 11, 2, 11, 0, tzinfo=UTC))["session"] == "closed"
    assert fh.market_state(SATURDAY)["session"] == "weekend"
    assert fh.market_state(OPEN, [date(2026, 11, 2)])["session"] == "holiday"


# --- candle aggregation input validation ----------------------------------------------------


def test_aggregator_skips_malformed_packets() -> None:
    agg = TickAggregator()
    good = {"segment": "NSE_EQ", "security_id": "3499", "ltp": "100", "ltt": OPEN.isoformat()}
    assert agg.add(good) == []
    for bad in (
        {**good, "ltp": "abc"},
        {**good, "ltp": "-5"},
        {**good, "ltp": "0"},
        {**good, "ltt": "yesterday"},
        {**good, "security_id": "x"},
        {**good, "segment": None},
    ):
        assert agg.add(bad) == []
    nxt = {**good, "ltp": "101", "ltt": (OPEN + timedelta(minutes=1)).isoformat()}
    bars = agg.add(nxt)
    assert len(bars) == 1 and bars[0][1].close == Decimal("100")  # bad ones never got in
    # An identical repeat changes nothing (no extra volume, same OHLC).
    assert agg.add(nxt) == [] and agg.open["NSE_EQ:3499"]["close"] == Decimal("101")


# --- the docker compose `dhan` profile ------------------------------------------------------


def test_compose_dhan_profile_starts_the_feed_with_its_dependencies() -> None:
    doc = yaml.safe_load((Path(__file__).resolve().parents[1] / "compose.yaml").read_text())
    svc = doc["services"]["dhan-feed"]
    assert svc["profiles"] == ["dhan"]
    assert svc["command"] == ["python", "-m", "app.workers.dhan_feed"]
    # Redis healthy, migrations done (so dhan.connections exists), API healthy.
    deps = svc["depends_on"]
    assert deps["redis"]["condition"] == "service_healthy"
    assert deps["migrate"]["condition"] == "service_completed_successfully"
    assert deps["api"]["condition"] == "service_healthy"
    assert {"redis", "migrate", "api"} <= set(doc["services"])
    for always_on in ("redis", "migrate", "api"):  # no profile: they start without `dhan`
        assert not doc["services"][always_on].get("profiles"), always_on
    # Health = a fresh agent heartbeat, which the worker keeps even while it waits for
    # credentials (so docker does not restart a worker that is only waiting).
    assert "app.workers.healthcheck" in svc["healthcheck"]["test"]
    assert svc["restart"] == "unless-stopped" and "ports" not in svc
    assert set(svc["networks"]) == {"aureon", "private-ai"}
