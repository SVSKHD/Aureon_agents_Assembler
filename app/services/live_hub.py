"""Browser fan-out for live market data: one hub per API process (docs/38 §5).

- **One Redis subscription per process.** The hub subscribes to the market-live channels once
  and routes every message to the WebSocket connections that asked for it. Browsers never cause
  a Dhan connection: they add *demand* (`dhan:feed:demand`), and the single feed worker
  subscribes what is wanted.
- **Topics** (client names → internal routes):
  `quote:<instrument_id>`, `candle:<instrument_id>:<tf>`, `rankings:<universe>`, `screener:<tf>`.
- **Snapshots first.** Subscribing sends a `snapshot` for the topic; updates that arrive while it
  is being read are held and delivered right after it, so a client never applies an older
  snapshot over a newer update.
- **Backpressure.** Quotes, forming candles and rankings are coalesced per topic (only the
  newest is kept for a slow client). Everything else (closed candles, screener changes,
  snapshots, acks) goes through a bounded queue; a client that lets it fill up is disconnected
  as a slow consumer (it reconnects and gets fresh snapshots). A send that blocks longer than
  MARKET_WS_SEND_TIMEOUT_SECONDS does the same.
- **Redis interruptions.** A lost subscription tells every client `status` (`redis: down`),
  retries with backoff, and on recovery sends `status` (`redis: ok`, `resync: true`) plus a
  fresh snapshot of every subscribed topic.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from app.core.config import Settings
from app.core.logging import get_logger
from app.services import dhan_feed_health as fh
from app.services import market_live as ml

log = get_logger(__name__)

TopicKind = Literal["quote", "candle", "rankings", "screener"]
CONNS_KEY = "market:ws:conns:{user}"
CONN_TTL = 120.0


@dataclass(frozen=True, slots=True)
class Topic:
    kind: TopicKind
    name: str  # what the client said: "quote:42"
    route: str  # what the channels carry: "quote:NSE_EQ:2885"
    instrument_id: int | None = None
    ref: str | None = None
    tf: str | None = None
    universe: str | None = None


def route_of(channel: str, msg: dict[str, Any]) -> str | None:
    """The internal route a published message belongs to."""
    if channel == ml.CH_QUOTES:
        return f"quote:{msg.get('key') or msg.get('ref')}"
    if channel == ml.CH_CANDLES:
        return f"candle:{msg.get('timeframe')}:{msg.get('key')}"
    if channel == ml.CH_RANKINGS:
        return f"rankings:{msg.get('universe')}"
    if channel == ml.CH_SCREENER:
        return f"screener:{msg.get('timeframe')}"
    return None


class Connection:
    """One browser socket: its topics, its outbox and its sender."""

    def __init__(
        self,
        user_id: Any,
        send: Callable[[str], Awaitable[None]],
        *,
        outbox_max: int,
        send_timeout: float,
        max_topics: int,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.user_id = user_id
        self._send = send
        self.outbox_max = outbox_max
        self.send_timeout = send_timeout
        self.max_topics = max_topics
        self.topics: dict[str, Topic] = {}  # route -> topic
        self.queue: deque[dict[str, Any]] = deque()
        self.latest: dict[str, dict[str, Any]] = {}
        self.pending: set[str] = set()
        self.held: dict[str, list[tuple[dict[str, Any], str | None]]] = {}
        self.wake = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_reason: str | None = None
        self.draining = False
        self.seq = 0
        self.sent = 0
        self.coalesced = 0

    def close(self, reason: str, *, drain: bool = False) -> None:
        """Stop the connection. `drain=True` lets the sender deliver what is already queued
        first (a final `bye`); a slow consumer is never drained."""
        if self.close_reason is None:
            self.close_reason = reason
            self.draining = drain
        self.closed.set()
        self.wake.set()

    def offer(self, msg: dict[str, Any], coalesce: str | None = None) -> bool:
        """Queue a message. `coalesce` = a key under which only the newest is kept."""
        if self.closed.is_set():
            return False
        if coalesce is not None:
            if coalesce in self.latest:
                self.coalesced += 1
                del self.latest[coalesce]  # re-insert at the end: keeps arrival order
            self.latest[coalesce] = msg
        else:
            if len(self.queue) >= self.outbox_max:
                self.close("slow_consumer")
                return False
            if msg.get("type") == "candle" and msg.get("closed"):
                # A closed bar supersedes a queued forming version of the same bar.
                key = f"candle:{msg.get('topic')}"
                old = self.latest.get(key)
                if old is not None and str(old.get("ts")) <= str(msg.get("ts")):
                    del self.latest[key]
            self.queue.append(msg)
        self.wake.set()
        return True

    def deliver(self, route: str, msg: dict[str, Any], coalesce: str | None) -> None:
        """Hold updates for a topic whose snapshot is still being read."""
        if route in self.pending:
            held = self.held.setdefault(route, [])
            if coalesce is not None:
                held[:] = [(m, c) for m, c in held if c != coalesce]
            held.append((msg, coalesce))
            del held[: -self.outbox_max]
            return
        self.offer(msg, coalesce)

    def release(self, route: str) -> None:
        self.pending.discard(route)
        for msg, c in self.held.pop(route, []):
            self.offer(msg, c)

    def _next(self) -> dict[str, Any] | None:
        if self.queue:
            return self.queue.popleft()
        if self.latest:
            k = next(iter(self.latest))
            return self.latest.pop(k)
        return None

    async def run_sender(self) -> None:
        """Send until closed. Never raises: a failure closes the connection."""
        try:
            while True:
                await self.wake.wait()
                self.wake.clear()
                while True:
                    if self.closed.is_set() and not self.draining:
                        return
                    msg = self._next()
                    if msg is None:
                        break
                    self.seq += 1
                    body = json.dumps({**msg, "seq": self.seq}, default=str, separators=(",", ":"))
                    try:
                        await asyncio.wait_for(self._send(body), self.send_timeout)
                    except TimeoutError:
                        self.close("slow_consumer")
                        return
                    except Exception:
                        self.close("send_failed")
                        return
                    self.sent += 1
                if self.closed.is_set():
                    return  # drained
        except asyncio.CancelledError:
            raise


SnapshotFn = Callable[[Topic], Awaitable[dict[str, Any]]]


class LiveHub:
    def __init__(
        self,
        redis: Any,
        settings: Settings,
        *,
        snapshot: SnapshotFn | None = None,
        retry_unit: float = 1.0,
    ) -> None:
        self.redis = redis
        self.settings = settings
        self.snapshot_fn = snapshot or self.default_snapshot
        self.unit = retry_unit
        self.conns: dict[str, Connection] = {}
        self.routes: dict[str, set[Connection]] = {}
        self.redis_ok: bool | None = None
        self.resyncs = 0
        self.dispatched = 0
        self.dropped_slow = 0
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._subscribed = asyncio.Event()

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self.pubsub_loop(), name="live-hub-pubsub"),
            asyncio.create_task(self.demand_loop(), name="live-hub-demand"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for c in list(self.conns.values()):
            c.close("server_shutdown")
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []

    def stats(self) -> dict[str, Any]:
        return {
            "connections": len(self.conns),
            "topics": len(self.routes),
            "redis_ok": self.redis_ok,
            "resyncs": self.resyncs,
            "dispatched": self.dispatched,
            "slow_consumers": self.dropped_slow,
        }

    # --- connections ------------------------------------------------------------------------

    async def add(self, conn: Connection) -> bool:
        """Register a connection unless the user already has MARKET_WS_MAX_CONNECTIONS_PER_USER
        (counted across API processes in Redis; per process when Redis is unavailable)."""
        cap = self.settings.MARKET_WS_MAX_CONNECTIONS_PER_USER
        local = sum(1 for c in self.conns.values() if c.user_id == conn.user_id)
        key = CONNS_KEY.format(user=conn.user_id)
        now = time.time()
        try:
            await self.redis.zremrangebyscore(key, "-inf", now)
            n = len(await self.redis.zrangebyscore(key, now, "+inf"))
            if n >= cap:
                return False
            await self.redis.zadd(key, {conn.id: now + CONN_TTL})
            await self.redis.expire(key, int(CONN_TTL * 2))
        except Exception:
            if local >= cap:
                return False
        self.conns[conn.id] = conn
        return True

    async def remove(self, conn: Connection) -> None:
        conn.close(conn.close_reason or "closed")
        if conn.close_reason == "slow_consumer":
            self.dropped_slow += 1
        self.conns.pop(conn.id, None)
        for route in list(conn.topics):
            subs = self.routes.get(route)
            if subs is not None:
                subs.discard(conn)
                if not subs:
                    del self.routes[route]
        with contextlib.suppress(Exception):
            await self.redis.zrem(CONNS_KEY.format(user=conn.user_id), conn.id)

    async def subscribe(
        self, conn: Connection, topics: list[Topic]
    ) -> tuple[list[str], list[dict[str, str]]]:
        accepted: list[Topic] = []
        rejected: list[dict[str, str]] = []
        for t in topics:
            if t.route in conn.topics:
                accepted.append(t)  # idempotent: a re-subscribe gets a fresh snapshot
                continue
            if len(conn.topics) >= conn.max_topics:
                rejected.append({"topic": t.name, "reason": "topic_limit"})
                continue
            conn.topics[t.route] = t
            self.routes.setdefault(t.route, set()).add(conn)
            accepted.append(t)
        refs = [t.ref for t in accepted if t.ref]
        if refs:
            await self.add_demand(refs)
        for t in accepted:
            await self.send_snapshot(conn, t)
        return [t.name for t in accepted], rejected

    async def unsubscribe(self, conn: Connection, names: list[str]) -> list[str]:
        out = []
        for route, t in list(conn.topics.items()):
            if t.name in names:
                del conn.topics[route]
                conn.pending.discard(route)
                conn.held.pop(route, None)
                conn.latest.pop(f"{t.kind}:{t.name}", None)
                subs = self.routes.get(route)
                if subs is not None:
                    subs.discard(conn)
                    if not subs:
                        del self.routes[route]
                out.append(t.name)
        return out

    async def send_snapshot(self, conn: Connection, t: Topic) -> None:
        conn.pending.add(t.route)
        try:
            data = await self.snapshot_fn(t)
            conn.offer({"type": "snapshot", "topic": t.name, "kind": t.kind, **data})
        except Exception as exc:
            conn.offer(
                {
                    "type": "snapshot",
                    "topic": t.name,
                    "kind": t.kind,
                    "available": False,
                    "error": f"snapshot unavailable ({type(exc).__name__})",
                }
            )
        finally:
            conn.release(t.route)

    # --- Redis → connections ----------------------------------------------------------------

    def dispatch(self, channel: str, raw: Any) -> int:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return 0
        if not isinstance(msg, dict):
            return 0
        route = route_of(channel, msg)
        subs = self.routes.get(route or "")
        if not subs:
            return 0
        n = 0
        for conn in list(subs):
            t = conn.topics.get(route or "")
            if t is None:
                continue
            out = {**msg, "topic": t.name}
            if t.instrument_id is not None:
                out["instrument_id"] = t.instrument_id
            coalesce: str | None = None
            kind = msg.get("type")
            if kind == "quote" or kind == "rankings":
                coalesce = f"{t.kind}:{t.name}"
            elif kind == "candle" and not msg.get("closed"):
                coalesce = f"candle:{t.name}"
            conn.deliver(t.route, out, coalesce)
            n += 1
        self.dispatched += n
        return n

    def broadcast(self, msg: dict[str, Any]) -> None:
        for c in list(self.conns.values()):
            c.offer(msg)

    async def resync(self) -> None:
        """After a Redis outage: a fresh snapshot of every topic of every connection."""
        self.resyncs += 1
        for c in list(self.conns.values()):
            for t in list(c.topics.values()):
                await self.send_snapshot(c, t)

    async def pubsub_loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            pubsub = None
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(*ml.CHANNELS)
                was = self.redis_ok
                self.redis_ok = True
                self._subscribed.set()
                backoff = 1.0
                if was is False:
                    self.broadcast({"type": "status", "redis": "ok", "resync": True})
                    await self.resync()
                    log.info("live_hub_redis_recovered")
                while not self._stop.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if not msg or msg.get("type") != "message":
                        continue
                    ch = msg.get("channel")
                    self.dispatch(ch.decode() if isinstance(ch, bytes) else str(ch), msg["data"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._subscribed.clear()
                if self.redis_ok is not False:
                    log.warning("live_hub_redis_lost", error=type(exc).__name__)
                    self.broadcast({"type": "status", "redis": "down", "resync": False})
                self.redis_ok = False
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), backoff * self.unit)
                backoff = min(30.0, backoff * 2)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()

    # --- demand for the feed ----------------------------------------------------------------

    def watched_refs(self) -> list[str]:
        refs: dict[str, None] = {}
        for c in self.conns.values():
            for t in c.topics.values():
                if t.ref:
                    refs[t.ref] = None
        return list(refs)

    async def add_demand(self, refs: list[str]) -> bool:
        if not refs:
            return True
        until = time.time() + self.settings.DHAN_FEED_DEMAND_TTL_SECONDS
        try:
            await self.redis.zadd(fh.DEMAND_KEY, dict.fromkeys(refs, until))
        except Exception:
            return False
        return True

    async def demand_loop(self) -> None:
        """Keep watched instruments' demand (and this process's connection records) alive."""
        every = max(1.0, self.settings.DHAN_FEED_DEMAND_TTL_SECONDS / 3)
        while not self._stop.is_set():
            await self.add_demand(self.watched_refs())
            now = time.time()
            for c in list(self.conns.values()):
                with contextlib.suppress(Exception):
                    await self.redis.zadd(CONNS_KEY.format(user=c.user_id), {c.id: now + CONN_TTL})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), every * self.unit)

    # --- snapshots ----------------------------------------------------------------------------

    async def default_snapshot(self, t: Topic) -> dict[str, Any]:
        now = datetime.now(UTC)
        s = self.settings
        if t.kind == "quote":
            assert t.ref is not None
            got = await ml.read_quotes(self.redis, [t.ref], now=now, settings=s)
            view = got.get(t.ref)
            return {"available": view is not None, "data": view, "instrument_id": t.instrument_id}
        if t.kind == "candle":
            from app.agents.trading.types import Timeframe

            assert t.ref is not None and t.tf is not None
            closed, forming = await ml.read_bars(self.redis, t.ref, Timeframe(t.tf), limit=100)
            return {
                "available": bool(closed or forming),
                "instrument_id": t.instrument_id,
                "timeframe": t.tf,
                "closed": [b.as_json() for b in closed],
                "forming": forming.as_json() if forming else None,
                "note": "today's live bars only; load history from GET /api/trading/chart",
            }
        if t.kind == "rankings":
            snap = await ml.read_json(self.redis, ml.MOVERS_KEY.format(universe=t.universe))
            if not isinstance(snap, dict):
                return {"available": False, "universe": t.universe}
            return {"available": True, **ml.default_movers(snap, s)}
        snap = await ml.read_json(self.redis, ml.SCREENER_KEY.format(tf=t.tf))
        if not isinstance(snap, dict):
            return {"available": False, "timeframe": t.tf, "basis": "live"}
        rows = [r for r in snap.get("rows") or [] if r.get("state") != "UNAVAILABLE"]
        return {
            "available": True,
            "timeframe": t.tf,
            "basis": "live",
            "computed_at": snap.get("computed_at"),
            "market": snap.get("market"),
            "evaluated": len(snap.get("rows") or []),
            "rows": rows,
        }


async def get_hub(app: Any) -> LiveHub:
    """The process's hub, started on first use."""
    hub: LiveHub | None = getattr(app.state, "live_hub", None)
    if hub is None:
        hub = LiveHub(app.state.redis, app.state.settings)
        app.state.live_hub = hub
        hub.start()
    return hub


async def stop_hub(app: Any) -> None:
    hub: LiveHub | None = getattr(app.state, "live_hub", None)
    if hub is not None:
        await hub.stop()
        app.state.live_hub = None
