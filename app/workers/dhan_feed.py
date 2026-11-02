"""Dhan live-feed worker. It runs in its own process and never inside the API, imaging or Ollama
processes.

    python -m app.workers.dhan_feed

- **Subscribes** to DHAN_FEED_INSTRUMENTS ("SEGMENT:SECURITY_ID", up to 5000) in DHAN_FEED_MODE.
- **Stores** the latest values per instrument in the Redis hash `dhan:tick:<SEG>:<ID>`
  (24 h TTL) and publishes every packet as JSON on the channel `dhan:ticks`.
- **Marks stale:** an instrument with no packet for DHAN_FEED_STALE_SECONDS while NSE is open
  (09:15-15:30 IST, Mon-Fri) gets `stale=1` in its hash and is counted in the heartbeat.
- **Registers** as agent `dhan-feed` (kind `trading`):
  - Pausing it closes the connection; resuming reconnects.
  - An auth failure (expired token, no data plan) sets the state to `error` and stops
    reconnecting until the worker is restarted with a fresh token.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.agents.registry import AgentRegistry, AgentSpec, Heartbeat
from app.agents.trading.dhan.adapter import market_day_bounds
from app.agents.trading.dhan.feed import DhanFeed, FeedPacket, parse_instruments
from app.agents.trading.dhan.parse import IST
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import build_engine, build_sessionmaker

log = get_logger("worker.dhan_feed")
DHAN_FEED = AgentSpec("dhan-feed", "Dhan live market feed", "trading")
TICK_TTL = 86_400
CHANNEL = "dhan:ticks"


def nse_open(now: datetime) -> bool:
    local = now.astimezone(IST)
    if local.weekday() >= 5:
        return False
    open_, close = market_day_bounds(local.date())
    return open_ <= now < close


class FeedSink:
    """Consumes the feed queue into Redis and tracks freshness."""

    def __init__(self, redis: Redis, keys: list[str], stale_after: float) -> None:
        self.redis = redis
        self.keys = keys
        self.stale_after = stale_after
        self.last_seen: dict[str, float] = {}
        self.started = time.time()
        self.stale: set[str] = set()

    async def write(self, p: FeedPacket) -> None:
        data = p.as_json()
        fields: dict[Any, Any] = {
            k: (json.dumps(v) if isinstance(v, list | dict) else str(v)) for k, v in data.items()
        }
        fields["received_at"] = datetime.now(UTC).isoformat()
        fields["stale"] = "0"
        key = f"dhan:tick:{p.key}"
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.hset(key, mapping=fields)
            pipe.expire(key, TICK_TTL)
            pipe.publish(CHANNEL, json.dumps(data))
            await pipe.execute()
        self.last_seen[p.key] = time.time()
        self.stale.discard(p.key)

    async def mark_stale(self, now: datetime | None = None) -> set[str]:
        now = now or datetime.now(UTC)
        if not nse_open(now):
            return set()
        t = now.timestamp()
        newly = {
            k for k in self.keys if t - self.last_seen.get(k, self.started) > self.stale_after
        } - self.stale
        for k in newly:
            await self.redis.hset(f"dhan:tick:{k}", "stale", "1")
        self.stale |= newly
        return newly

    async def drain(self, feed: DhanFeed) -> None:
        while True:
            await self.write(await feed.queue.get())


async def main() -> int:
    settings: Settings = get_settings()
    configure_logging(settings.LOG_LEVEL)
    if not settings.dhan_configured:
        log.error("dhan_not_configured", hint="set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN")
        return 1
    instruments = parse_instruments(settings.DHAN_FEED_INSTRUMENTS)
    if not instruments:
        log.error("no_feed_instruments", hint="set DHAN_FEED_INSTRUMENTS")
        return 1
    assert settings.DHAN_CLIENT_ID and settings.DHAN_ACCESS_TOKEN
    engine = build_engine(settings)
    registry = AgentRegistry(build_sessionmaker(engine))
    redis = Redis.from_url(settings.REDIS_URL)
    feed = DhanFeed(
        url=settings.DHAN_FEED_URL,
        client_id=settings.DHAN_CLIENT_ID,
        access_token=settings.DHAN_ACCESS_TOKEN.get_secret_value(),
        instruments=instruments,
        mode=settings.DHAN_FEED_MODE,
        queue_max=settings.DHAN_FEED_QUEUE_MAX,
        max_backoff=settings.DHAN_FEED_MAX_BACKOFF_SECONDS,
    )
    sink = FeedSink(redis, [f"{s}:{i}" for s, i in instruments], settings.DHAN_FEED_STALE_SECONDS)
    shutdown = asyncio.Event()
    paused = {"value": False}
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    def state() -> tuple[str, dict[str, Any]]:
        st = feed.stats
        s = "error" if st.auth_failed else ("busy" if st.connected else "idle")
        return s, {
            "connected": st.connected,
            "instruments": len(instruments),
            "mode": settings.DHAN_FEED_MODE,
            "packets": st.packets,
            "dropped": st.dropped,
            "skipped": st.skipped,
            "reconnects": st.reconnects,
            "stale": len(sink.stale),
            "auth_failed": st.auth_failed,
            "last_disconnect_reason": st.last_disconnect_reason,
            "queue": feed.queue.qsize(),
        }

    def on_paused(p: bool) -> None:
        paused["value"] = p

    hb = Heartbeat(registry, DHAN_FEED, settings.AGENT_HEARTBEAT_SECONDS, state, on_paused)
    hb.start()
    drain = asyncio.create_task(sink.drain(feed))
    reported_auth = False
    try:
        while not shutdown.is_set():
            if paused["value"] or feed.stats.auth_failed:
                if feed.stats.auth_failed and not reported_auth:
                    reported_auth = True
                    with contextlib.suppress(Exception):
                        await registry.log(
                            DHAN_FEED.id,
                            "error",
                            "feed stopped: Dhan rejected the token or data plan "
                            f"(reason {feed.stats.last_disconnect_reason}); "
                            "update DHAN_ACCESS_TOKEN and restart dhan-feed",
                        )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(shutdown.wait(), 5)
                continue
            run_stop = asyncio.Event()

            async def watch(stop: asyncio.Event = run_stop) -> None:
                while not shutdown.is_set() and not paused["value"]:
                    await sink.mark_stale()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(shutdown.wait(), 5)
                stop.set()

            watcher = asyncio.create_task(watch())
            await feed.run(run_stop)
            run_stop.set()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
    finally:
        drain.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drain
        await hb.stop()
        await redis.aclose()
        await engine.dispose()
        log.info("dhan_feed_stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
