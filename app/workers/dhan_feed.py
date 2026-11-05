"""Dhan live-feed worker. It runs in its own process and never inside the API, imaging or Ollama
processes. Read-only: it only subscribes to market data (docs/28-dhan-operations.md).

    python -m app.workers.dhan_feed

- **Subscribes** to DHAN_FEED_INSTRUMENTS ("SEGMENT:SECURITY_ID", up to 5000) in DHAN_FEED_MODE.
- **Stores** the latest values per instrument in the Redis hash `dhan:tick:<SEG>:<ID>`
  (24 h TTL) and publishes every accepted packet as JSON on the channel `dhan:ticks`.
- **Validates** every packet before it is stored: unknown segment, an instrument we did not
  subscribe to, a trade time in the future or before 2010, a negative / non-finite price are
  dropped and counted. An identical repeat of the last packet (Dhan re-sends a snapshot after
  a reconnect) and a packet older than the last one seen for that instrument are dropped too.
- **Never dies on Redis:** a failed write keeps the newest packet per instrument and is retried
  with backoff; `redis_ok` and `pending_writes` say so in the heartbeat and status.
- **Marks stale:** an instrument with no packet for DHAN_FEED_STALE_SECONDS while NSE is open
  (09:15-15:30 IST, Mon-Fri, not a DHAN_MARKET_HOLIDAYS day) gets `stale=1` in its hash.
- **Reports** a status record (`dhan:feed:status`) every 5 s for the health API and monitor.
- **One feed at a time:** a Redis leader lock (`dhan:feed:leader`) keeps a second worker (a
  deploy overlap, a manual run) from opening a second socket and publishing duplicate ticks.
- **No restart loop:**
  - unconfigured (no client id, no token, no instruments): the worker stays up, reports
    `waiting_config`, and re-executes itself when a DHAN_ setting changes;
  - after DHAN_FEED_CRASH_LOOP_STARTS starts in 10 minutes it waits (cooldown, 15 s doubling
    up to 5 min) before connecting;
  - an unexpected crash is logged, written to the agent log and `dhan:feed:last_crash`, and
    the process exits non-zero (docker restarts it; the guard above bounds the loop).
- **Tokens:** read at every connect (the login session's token while valid, else a non-expired
  manual token). After an auth failure the worker waits until the token changes, then
  reconnects by itself. A settings change also re-executes it (app_settings.run_worker).
- **Reconnect requests** on `dhan:feed:control` (admin button, bounded recovery) reconnect at
  most once per DHAN_FEED_RECONNECT_COOLDOWN_SECONDS.
- **Registers** as agent `dhan-feed` (kind `trading`); pausing it closes the connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import secrets
import signal
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from redis.asyncio import Redis

from app.agents.registry import AgentRegistry, AgentSpec, Heartbeat
from app.agents.trading.dhan.feed import DhanFeed, FeedPacket, parse_instruments
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.db.session import build_engine, build_sessionmaker
from app.services import app_settings
from app.services import dhan_feed_health as fh

log = get_logger("worker.dhan_feed")
DHAN_FEED = AgentSpec("dhan-feed", "Dhan live market feed", "trading")
TICK_TTL = 86_400
CHANNEL = "dhan:ticks"
MIN_TRADE_TIME = datetime(2010, 1, 1, tzinfo=UTC)
MAX_FUTURE_S = 300
STATUS_EVERY_S = 5.0
MAX_STARTUP_DELAY_S = 300.0


def nse_open(now: datetime, holidays: Any = ()) -> bool:
    return fh.is_open(now, holidays)


def validate(p: FeedPacket, subscribed: set[str], now: datetime) -> str | None:
    """Why a packet must not be stored (None = fine)."""
    if p.segment is None:
        return "unknown_segment"
    if p.key not in subscribed:
        return "unsubscribed"
    if p.ltt is not None and (
        p.ltt < MIN_TRADE_TIME or (p.ltt - now).total_seconds() > MAX_FUTURE_S
    ):
        return "bad_time"
    for price in (p.ltp, p.open, p.high, p.low, p.close, p.prev_close):
        if price is not None and (not price.is_finite() or price < 0):
            return "bad_price"
    return None


class FeedSink:
    """Consumes the feed queue into Redis, tracks freshness, survives Redis outages."""

    def __init__(
        self,
        redis: Redis,
        keys: list[str],
        stale_after: float,
        holidays: Any = (),
    ) -> None:
        self.redis = redis
        self.keys = keys
        self.subscribed = set(keys)
        self.stale_after = stale_after
        self.holidays = holidays
        self.last_seen: dict[str, float] = {}
        self.started = time.time()
        self.stale: set[str] = set()
        self.last_sig: dict[tuple[str, str], str] = {}
        self.last_ltt: dict[str, datetime] = {}
        self.invalid: dict[str, int] = {}
        self.duplicates = 0
        self.out_of_order = 0
        self.written = 0
        self.redis_ok = True
        self.redis_errors = 0
        self.redis_failing_since: float | None = None
        self.last_redis_error: str | None = None
        self.drain_errors = 0
        # Newest packet per (instrument, kind) that could not be written yet.
        self.pending: dict[tuple[str, str], FeedPacket] = {}
        self._retry_at = 0.0
        self._retry_backoff = 0.5

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

    def _redis_failed(self, exc: BaseException) -> None:
        self.redis_errors += 1
        self.last_redis_error = type(exc).__name__
        if self.redis_ok:
            log.warning("dhan_feed_redis_write_failed", error=type(exc).__name__)
            self.redis_failing_since = time.time()
        self.redis_ok = False
        self._retry_at = time.monotonic() + self._retry_backoff
        self._retry_backoff = min(10.0, self._retry_backoff * 2)

    def _redis_recovered(self) -> None:
        if not self.redis_ok:
            log.info("dhan_feed_redis_recovered", errors=self.redis_errors)
        self.redis_ok = True
        self.redis_failing_since = None
        self._retry_backoff = 0.5

    async def accept(self, p: FeedPacket, now: datetime | None = None) -> bool:
        """Validate, de-duplicate and store one packet. True = stored."""
        now = now or datetime.now(UTC)
        reason = validate(p, self.subscribed, now)
        if reason is not None:
            self.invalid[reason] = self.invalid.get(reason, 0) + 1
            return False
        last = self.last_ltt.get(p.key)
        if p.ltt is not None and last is not None and p.ltt < last:
            self.out_of_order += 1
            return False
        sig = json.dumps(p.as_json(), sort_keys=True)
        if self.last_sig.get((p.key, p.kind)) == sig:
            self.duplicates += 1
            # Still proof the instrument is alive.
            self.last_seen[p.key] = time.time()
            return False
        self.last_sig[(p.key, p.kind)] = sig
        if p.ltt is not None:
            self.last_ltt[p.key] = p.ltt
        self.last_seen[p.key] = time.time()
        self.stale.discard(p.key)
        if not self.redis_ok and time.monotonic() < self._retry_at:
            self.pending[(p.key, p.kind)] = p
            return False
        try:
            await self.write(p)
        except Exception as exc:  # Redis down / timeout: hold the newest, retry later
            self.pending[(p.key, p.kind)] = p
            self._redis_failed(exc)
            return False
        self.written += 1
        self._redis_recovered()
        if self.pending:
            await self.flush_pending()
        return True

    async def flush_pending(self) -> int:
        """Write the held packets (newest per instrument and kind). Stops at the first error."""
        if not self.pending or time.monotonic() < self._retry_at:
            return 0
        n = 0
        for k in list(self.pending):
            try:
                await self.write(self.pending[k])
            except Exception as exc:
                self._redis_failed(exc)
                return n
            self.pending.pop(k, None)
            self.written += 1
            n += 1
        self._redis_recovered()
        return n

    def fresh_count(self, now: float | None = None) -> int:
        t = time.time() if now is None else now
        return sum(1 for k in self.keys if t - self.last_seen.get(k, -math.inf) <= self.stale_after)

    async def mark_stale(self, now: datetime | None = None) -> set[str]:
        now = now or datetime.now(UTC)
        market = fh.market_state(now, self.holidays)
        if not market["open"]:
            self.stale.clear()  # silence is expected; counts restart at the next open
            return set()
        t = now.timestamp()
        # Nothing is stale before it had DHAN_FEED_STALE_SECONDS of open market to tick in.
        baseline = max(self.started, market["opened_at"].timestamp())
        newly = {
            k for k in self.keys if t - self.last_seen.get(k, baseline) > self.stale_after
        } - self.stale
        for k in newly:
            try:
                await self.redis.hset(f"dhan:tick:{k}", "stale", "1")
            except Exception as exc:
                self._redis_failed(exc)
                break
        self.stale |= newly
        return newly

    async def drain(self, feed: DhanFeed) -> None:
        """Never ends on its own: a bad packet or a Redis error is counted, not fatal."""
        while True:
            p = await feed.queue.get()
            try:
                await self.accept(p)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.drain_errors += 1
                log.warning("dhan_feed_drain_error", error=type(exc).__name__)


class _Registry(Protocol):
    async def log(self, agent_id: str, level: str, message: str) -> None: ...


def startup_delay(recent_starts: int, limit: int) -> float:
    """Crash-loop guard: 0 below `limit` starts in the window, then 15 s doubling to 5 min."""
    if recent_starts < limit:
        return 0.0
    return float(min(MAX_STARTUP_DELAY_S, 15 * 2 ** (recent_starts - limit)))


def config_problem(settings: Settings) -> str | None:
    if not settings.DHAN_CLIENT_ID:
        return "set DHAN_CLIENT_ID on /settings"
    if settings.dhan_access_token() is None:
        return "log in to Dhan on /settings (or set a valid DHAN_ACCESS_TOKEN)"
    if not settings.DHAN_FEED_INSTRUMENTS:
        return "set DHAN_FEED_INSTRUMENTS on /settings"
    try:
        parse_instruments(settings.DHAN_FEED_INSTRUMENTS)
    except ValueError as exc:
        return str(exc)[:200]
    return None


class FeedRuntime:
    """Everything the worker does between start and stop; injectable for tests."""

    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        registry: _Registry,
        shutdown: asyncio.Event,
        *,
        connector: Any = None,
        sleep_unit: float = 1.0,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.registry = registry
        self.shutdown = shutdown
        self.connector = connector
        self.unit = sleep_unit  # tests shrink every wait
        self.instance = f"{os.getpid()}-{secrets.token_hex(4)}"
        self.state_name = "starting"
        self.problem: str | None = None
        self.paused = False
        self.leader = False
        self.feed: DhanFeed | None = None
        self.sink: FeedSink | None = None
        self.instruments: list[tuple[str, str]] = []
        self.recent_starts = 0
        self.last_reconnect_at = 0.0
        self.reported_auth = False
        self._run_stop: asyncio.Event | None = None
        self._tasks: list[asyncio.Task[Any]] = []

    # --- state ------------------------------------------------------------------------

    def token_source(self) -> str:
        if self.settings.dhan_session_active():
            return "session"
        return "manual" if self.settings.dhan_access_token() is not None else "none"

    def status(self) -> dict[str, Any]:
        feed, sink = self.feed, self.sink
        st = feed.stats if feed else None
        return {
            "state": self.state_name,
            "problem": self.problem,
            "leader": self.leader,
            "connected": bool(st and st.connected),
            "subscribed": st.subscribed if st else 0,
            "instruments": len(self.instruments),
            "fresh": sink.fresh_count() if sink else 0,
            "stale": len(sink.stale) if sink else 0,
            "last_packet_at": st.last_packet_at if st else None,
            "packets": st.packets if st else 0,
            "reconnects": st.reconnects if st else 0,
            "reconnects_requested": st.reconnects_requested if st else 0,
            "consecutive_failures": st.consecutive_failures if st else 0,
            "dropped": st.dropped if st else 0,
            "skipped": st.skipped if st else 0,
            "auth_failed": bool(st and st.auth_failed),
            "last_disconnect_reason": st.last_disconnect_reason if st else None,
            "last_error": st.last_error if st else None,
            "redis_ok": sink.redis_ok if sink else True,
            "redis_errors": sink.redis_errors if sink else 0,
            "pending_writes": len(sink.pending) if sink else 0,
            "invalid": sum(sink.invalid.values()) if sink else 0,
            "invalid_by_reason": dict(sink.invalid) if sink else {},
            "duplicates": sink.duplicates if sink else 0,
            "out_of_order": sink.out_of_order if sink else 0,
            "drain_errors": sink.drain_errors if sink else 0,
            "queue": feed.queue.qsize() if feed else 0,
            "mode": self.settings.DHAN_FEED_MODE,
            "token_source": self.token_source(),
            "recent_starts": self.recent_starts,
            "updated_at": time.time(),
        }

    def heartbeat_state(self) -> tuple[str, dict[str, Any]]:
        details = self.status()
        if self.state_name in ("auth_failed", "cooldown", "waiting_config") or (
            self.sink is not None and not self.sink.redis_ok
        ):
            state = "error"
        elif details["connected"]:
            state = "busy"
        else:
            state = "idle"
        return state, details

    async def publish_status(self) -> None:
        """Write the status record, unless another worker leads the feed (its record is the
        one that matters; a standby must not overwrite it)."""
        try:
            if not self.leader and await self.redis.get(fh.LEADER_KEY) is not None:
                return
            await self.redis.set(
                fh.STATUS_KEY, json.dumps(self.status(), default=str), ex=fh.STATUS_TTL
            )
        except Exception as exc:
            if self.sink is not None:
                self.sink._redis_failed(exc)

    async def _wait(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.shutdown.wait(), seconds * self.unit)

    # --- startup guards ---------------------------------------------------------------

    async def record_start(self) -> int:
        """Remember this start in Redis; how many starts in the last 10 minutes."""
        now = time.time()
        try:
            await self.redis.lpush(fh.STARTS_KEY, str(now))
            await self.redis.ltrim(fh.STARTS_KEY, 0, 49)
            await self.redis.expire(fh.STARTS_KEY, fh.CRASH_WINDOW_S * 2)
            raw = await self.redis.lrange(fh.STARTS_KEY, 0, -1)
        except Exception:
            return 0
        starts = []
        for r in raw:
            with contextlib.suppress(ValueError):
                starts.append(float(r.decode() if isinstance(r, bytes) else r))
        return sum(1 for t in starts if now - t <= fh.CRASH_WINDOW_S)

    async def acquire_leader(self) -> bool:
        try:
            got = await self.redis.set(fh.LEADER_KEY, self.instance, nx=True, ex=fh.LEADER_TTL)
        except Exception:
            return False
        if got:
            return True
        try:
            cur = await self.redis.get(fh.LEADER_KEY)
        except Exception:
            return False
        cur_s = cur.decode() if isinstance(cur, bytes) else cur
        return cur_s == self.instance

    async def renew_leader(self) -> bool:
        """Keep the lock while it is ours. A Redis blip keeps leadership (the lock has a TTL;
        another worker can only take it once it lapsed)."""
        try:
            cur = await self.redis.get(fh.LEADER_KEY)
            cur_s = cur.decode() if isinstance(cur, bytes) else cur
            if cur_s == self.instance:
                await self.redis.expire(fh.LEADER_KEY, fh.LEADER_TTL)
                return True
            if cur_s is None:
                return bool(
                    await self.redis.set(fh.LEADER_KEY, self.instance, nx=True, ex=fh.LEADER_TTL)
                )
            return False
        except Exception:
            return True

    async def release_leader(self) -> None:
        with contextlib.suppress(Exception):
            cur = await self.redis.get(fh.LEADER_KEY)
            if (cur.decode() if isinstance(cur, bytes) else cur) == self.instance:
                await self.redis.delete(fh.LEADER_KEY)

    # --- control ----------------------------------------------------------------------

    def handle_control(self, raw: Any) -> str:
        """A message from `dhan:feed:control`. Returns what was done (for logs and tests)."""
        try:
            msg = json.loads(raw.decode() if isinstance(raw, bytes) else str(raw))
        except (ValueError, UnicodeDecodeError):
            return "ignored_bad_message"
        if not isinstance(msg, dict) or msg.get("cmd") != "reconnect":
            return "ignored_unknown"
        if self.feed is None or not self.leader:
            return "ignored_not_running"
        now = time.monotonic()
        cooldown = self.settings.DHAN_FEED_RECONNECT_COOLDOWN_SECONDS * self.unit
        if self.last_reconnect_at and now - self.last_reconnect_at < cooldown:
            return "ignored_cooldown"
        self.last_reconnect_at = now
        if self.feed.stats.auth_failed:
            if not self.feed.token_changed():
                return "ignored_auth_failed"  # same dead token: a person has to log in
            self.feed.reset_auth()
            return "auth_reset"
        self.feed.request_reconnect()
        return "reconnect"

    async def control_loop(self) -> None:
        backoff = 1.0
        while not self.shutdown.is_set():
            pubsub = None
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(fh.CONTROL_CHANNEL)
                backoff = 1.0
                while not self.shutdown.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if msg and msg.get("type") == "message":
                        what = self.handle_control(msg.get("data"))
                        log.info("dhan_feed_control", outcome=what)
                        with contextlib.suppress(Exception):
                            await self.registry.log(DHAN_FEED.id, "info", f"control: {what}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("dhan_feed_control_failed", error=type(exc).__name__)
                await self._wait(backoff)
                backoff = min(30.0, backoff * 2)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()  # type: ignore[no-untyped-call]

    # --- the loops --------------------------------------------------------------------

    async def watch(self) -> None:
        """Every 5 s: staleness, held writes, leadership, auth recovery, status record."""
        while not self.shutdown.is_set():
            try:
                if self.sink is not None:
                    await self.sink.mark_stale()
                    await self.sink.flush_pending()
                if self.leader and not await self.renew_leader():
                    log.warning("dhan_feed_leadership_lost")
                    self.leader = False
                    self.state_name = "standby"
                    if self._run_stop is not None:
                        self._run_stop.set()
                await self.publish_status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("dhan_feed_watch_failed", error=type(exc).__name__)
            await self._wait(STATUS_EVERY_S)

    def _ensure_drain(self) -> None:
        """Restart the drain task if it ever ended (it should not)."""
        assert self.sink is not None and self.feed is not None
        drain = self._tasks[0] if self._tasks else None
        if drain is None or drain.done():
            if drain is not None and not drain.cancelled() and drain.exception() is not None:
                self.sink.drain_errors += 1
                log.error("dhan_feed_drain_died", error=type(drain.exception()).__name__)
            task = asyncio.create_task(self.sink.drain(self.feed), name="dhan-feed-drain")
            if self._tasks:
                self._tasks[0] = task
            else:
                self._tasks.append(task)

    def build_feed(self, token: Callable[[], str | None]) -> DhanFeed:
        s = self.settings
        assert s.DHAN_CLIENT_ID
        return DhanFeed(
            url=s.DHAN_FEED_URL,
            client_id=s.DHAN_CLIENT_ID,
            access_token=token,
            instruments=self.instruments,
            mode=s.DHAN_FEED_MODE,
            queue_max=s.DHAN_FEED_QUEUE_MAX,
            max_backoff=s.DHAN_FEED_MAX_BACKOFF_SECONDS,
            connector=self.connector,
        )

    def _token(self) -> str | None:
        tok = self.settings.dhan_access_token()  # time-aware: session, else non-expired manual
        return tok.get_secret_value() if tok is not None else None

    async def run(self) -> int:
        problem = config_problem(self.settings)
        if problem is not None:
            # Stay up and say why; a DHAN_ change re-executes the worker. Exiting would make
            # docker restart it forever (docs/27 finding 2).
            self.state_name, self.problem = "waiting_config", problem
            log.warning("dhan_feed_waiting_for_config", hint=problem)
            with contextlib.suppress(Exception):
                await self.registry.log(DHAN_FEED.id, "warning", f"feed waiting: {problem}")
            while not self.shutdown.is_set():
                await self.publish_status()
                await self._wait(STATUS_EVERY_S)
            return 0
        self.instruments = parse_instruments(self.settings.DHAN_FEED_INSTRUMENTS)
        self.recent_starts = await self.record_start()
        delay = startup_delay(self.recent_starts, self.settings.DHAN_FEED_CRASH_LOOP_STARTS)
        if delay:
            self.state_name = "cooldown"
            self.problem = f"{self.recent_starts} starts in 10 min: waiting {delay:.0f}s"
            log.error("dhan_feed_crash_loop", starts=self.recent_starts, delay_s=delay)
            with contextlib.suppress(Exception):
                await self.registry.log(DHAN_FEED.id, "error", f"crash-loop guard: {self.problem}")
            await self.publish_status()
            await self._wait(delay)
            self.problem = None
        self.feed = self.build_feed(self._token)
        self.sink = FeedSink(
            self.redis,
            [f"{s}:{i}" for s, i in self.instruments],
            self.settings.DHAN_FEED_STALE_SECONDS,
            self.settings.DHAN_MARKET_HOLIDAYS,
        )
        self._ensure_drain()
        self._tasks.append(asyncio.create_task(self.watch(), name="dhan-feed-watch"))
        self._tasks.append(asyncio.create_task(self.control_loop(), name="dhan-feed-control"))
        try:
            while not self.shutdown.is_set():
                if not self.leader:
                    self.leader = await self.acquire_leader()
                    if not self.leader:
                        self.state_name = "standby"
                        await self._wait(STATUS_EVERY_S)
                        continue
                if self.paused:
                    self.state_name = "paused"
                    await self._wait(STATUS_EVERY_S)
                    continue
                if self.feed.stats.auth_failed:
                    if self.feed.token_changed():
                        log.info("dhan_feed_token_changed_reconnecting")
                        self.feed.reset_auth()
                        self.reported_auth = False
                        continue
                    self.state_name = "auth_failed"
                    if not self.reported_auth:
                        self.reported_auth = True
                        with contextlib.suppress(Exception):
                            await self.registry.log(
                                DHAN_FEED.id,
                                "error",
                                "feed stopped: Dhan rejected the token or data plan (reason "
                                f"{self.feed.stats.last_disconnect_reason or 'no token'}); "
                                "log in to Dhan on /settings (the feed reconnects by itself)",
                            )
                    await self._wait(STATUS_EVERY_S)
                    continue
                self.state_name = "running"
                self._run_stop = asyncio.Event()
                stopper = asyncio.create_task(self._stop_when_needed(self._run_stop))
                try:
                    await self.feed.run(self._run_stop)
                finally:
                    stopper.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stopper
                self._ensure_drain()
        finally:
            await self.close()
        return 0

    async def _stop_when_needed(self, run_stop: asyncio.Event) -> None:
        while not run_stop.is_set():
            if self.shutdown.is_set() or self.paused or not self.leader:
                run_stop.set()
                return
            await asyncio.sleep(0.2 * self.unit)

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        if self.sink is not None:
            with contextlib.suppress(Exception):
                await self.sink.flush_pending()
        self.state_name = "stopped"
        await self.publish_status()
        await self.release_leader()

    async def report_crash(self, exc: BaseException) -> None:
        """An unexpected exception: say so everywhere it will be seen, then let it exit."""
        kind = type(exc).__name__
        log.error("dhan_feed_crashed", error=kind)
        with contextlib.suppress(Exception):
            await self.registry.log(DHAN_FEED.id, "error", f"feed worker crashed: {kind}")
        with contextlib.suppress(Exception):
            await self.redis.set(
                fh.LAST_CRASH_KEY,
                json.dumps({"at": datetime.now(UTC).isoformat(), "error": kind}),
                ex=7 * 86_400,
            )


async def main() -> int:
    settings: Settings = await app_settings.worker_settings()
    configure_logging(settings.LOG_LEVEL)
    engine = build_engine(settings)
    maker = build_sessionmaker(engine)
    registry = AgentRegistry(maker)
    redis = Redis.from_url(settings.REDIS_URL)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows dev runs
            loop.add_signal_handler(sig, shutdown.set)
    # A change to its keys from Spasta re-execs this worker (docs/settings.md).
    app_settings.start_worker_watch(settings, maker, shutdown, ("DHAN_",), redis)
    rt = FeedRuntime(settings, redis, registry, shutdown)

    def on_paused(p: bool) -> None:
        rt.paused = p

    hb = Heartbeat(
        registry, DHAN_FEED, settings.AGENT_HEARTBEAT_SECONDS, rt.heartbeat_state, on_paused
    )
    hb.start()
    try:
        return await rt.run()
    except Exception as exc:
        await rt.report_crash(exc)
        raise
    finally:
        await hb.stop()
        await redis.aclose()
        await engine.dispose()
        log.info("dhan_feed_stopped")


if __name__ == "__main__":
    app_settings.run_worker(main, "app.workers.dhan_feed")
