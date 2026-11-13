"""Research worker (Trading Phase D): the cooperating analysis agents in one process.

    market-data ─▶ technical-analysis ─▶ signal ─▶ risk
    backtest   (queued trading.backtest_runs)
    screener   (queued trading.screener_runs; stored candles only, never a Dhan call)
    candle-backfill (trading.candle_backfill_requests → Dhan history, shared rate limit)
    ai-research (Redis list research:ai:queue → Claude gateway → trading.ai_analyses)

Each agent has its own bounded asyncio queue (RESEARCH_QUEUE_MAX). A full queue drops the
new item and counts it; the next tick re-enqueues the same work, so nothing piles up.
Every agent registers in agents.agents (kind "trading"), heartbeats its counters, and can
be paused by an admin. This process is separate from the API, imaging and chat inference;
it never places, modifies or cancels orders.

Run: python -m app.workers.research   (compose service `research-worker`, profile `research`)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.registry import AgentRegistry, AgentSpec, Heartbeat
from app.agents.research import ai, strategies
from app.agents.trading.base import BrokerAdapter
from app.agents.trading.dhan import build_dhan_adapter
from app.agents.trading.mt5 import build_mt5_adapter
from app.agents.trading.types import Timeframe
from app.ai.gateway import ClaudeGateway
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.db.session import build_engine, build_sessionmaker
from app.models.trading import Instrument
from app.services import app_settings, market_data, research, screener

log = get_logger("worker.research")

MARKET_DATA = AgentSpec("market-data", "Market data agent", "trading")
TECHNICAL = AgentSpec("technical-analysis", "Technical analysis agent", "trading")
SIGNAL = AgentSpec("signal", "Signal agent", "trading")
RISK = AgentSpec("risk", "Risk agent", "trading")
BACKTEST = AgentSpec("backtest", "Research / backtesting agent", "trading")
AI_RESEARCH = AgentSpec("ai-research", "AI research agent (Claude, explanations only)", "trading")
SCREENER = AgentSpec("screener", "Screener agent (stored candles, no broker calls)", "trading")
BACKFILL = AgentSpec("candle-backfill", "Candle history agent (Dhan, rate limited)", "trading")
SPECS = (MARKET_DATA, TECHNICAL, SIGNAL, RISK, BACKTEST, AI_RESEARCH, SCREENER, BACKFILL)
SPECS_BY_ID = {s.id: s for s in SPECS}
TICK_CHANNEL = "dhan:ticks"
# Settings this worker re-execs on. Its Dhan and MT5 adapters are built once, at startup, from
# the values then: without DHAN_ here a Dhan login made after the worker started (the session
# token is a managed setting, renewed daily) never reaches it, and every chart-history request
# stays queued ("Dhan is not configured") or runs on yesterday's expired token.
RELOAD_PREFIXES = ("DHAN_", "MT5_", "ANTHROPIC_", "AI_")


@dataclass
class AgentLane:
    spec: AgentSpec
    queue: asyncio.Queue[Any]
    paused: bool = False
    running: asyncio.Event = field(default_factory=asyncio.Event)
    busy: bool = False
    processed: int = 0
    dropped: int = 0
    errors: int = 0
    last_error: str | None = None
    last_ok_at: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.running.set()

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused:
            self.running.clear()
        else:
            self.running.set()

    def offer(self, item: Any) -> bool:
        try:
            self.queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            return False

    def state(self) -> tuple[str, dict[str, Any]]:
        return ("busy" if self.busy else "idle"), {
            "queue": self.queue.qsize(),
            "queue_max": self.queue.maxsize,
            "processed": self.processed,
            "dropped": self.dropped,
            "errors": self.errors,
            "last_error": self.last_error,
            **self.extra,
        }


class ResearchWorker:
    def __init__(
        self,
        settings: Settings,
        maker: async_sessionmaker[AsyncSession],
        adapters: dict[str, BrokerAdapter],
        *,
        redis: Redis | None = None,
        gateway: ClaudeGateway | None = None,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.maker = maker
        self.adapters = adapters
        self.redis = redis
        self.registry = AgentRegistry(maker)
        qmax = settings.RESEARCH_QUEUE_MAX
        self.lanes = {s.id: AgentLane(s, asyncio.Queue(maxsize=qmax)) for s in SPECS}
        self.watch: dict[tuple[int, str], list[research.WatchItem]] = {}
        self.ticks = research.TickAggregator()

    def lane(self, spec: AgentSpec) -> AgentLane:
        return self.lanes[spec.id]

    # --- the pipeline --------------------------------------------------------------------

    async def tick(self) -> int:
        """Rebuild the watch list from the strategy versions and enqueue market data."""
        async with self.maker() as db:
            items = await research.watch_items(db)
        watch: dict[tuple[int, str], list[research.WatchItem]] = {}
        for it in items:
            watch.setdefault((it.instrument.id, it.timeframe.value), []).append(it)
        self.watch = watch
        md = self.lane(MARKET_DATA)
        md.extra["watching"] = len(watch)
        for (_, _tf), its in watch.items():
            md.offer((its[0].instrument, its[0].timeframe))
        return len(watch)

    async def do_market_data(self, item: tuple[Instrument, Timeframe]) -> None:
        inst, tf = item
        out = await research.refresh_candles(
            self.maker, self.adapters.get(inst.broker), inst, tf, self.settings
        )
        if not out.get("supported"):
            self.lane(MARKET_DATA).extra.setdefault("unsupported", {})[
                f"{inst.broker}:{inst.symbol}:{tf.value}"
            ] = str(out.get("detail"))[:200]
        async with self.maker() as db:
            fresh = await research.freshness(db, inst.id, tf, self.settings)
        stale = self.lane(MARKET_DATA).extra.setdefault("stale", [])
        key = f"{inst.symbol}:{tf.value}"
        if fresh.stale and key not in stale:
            stale.append(key)
            await self.registry.log(
                MARKET_DATA.id, "warning", f"stale data {key}", **fresh.as_dict()
            )
        elif not fresh.stale and key in stale:
            stale.remove(key)
        self.lane(TECHNICAL).offer((inst, tf))

    async def do_technical(self, item: tuple[Instrument, Timeframe]) -> None:
        inst, tf = item
        its = self.watch.get((inst.id, tf.value), [])
        extra: set[int] = set()
        for it in its:
            extra |= strategies.ema_periods(it.version.params or {})
        await research.compute_indicators(self.maker, inst, tf, self.settings, extra)
        for it in its:
            self.lane(SIGNAL).offer(it)

    async def do_signal(self, item: research.WatchItem) -> None:
        out = await research.evaluate_signal(self.maker, item, self.settings)
        if out.get("signal_id") is not None and out.get("side") in ("BUY", "SELL"):
            self.lane(RISK).offer(out["signal_id"])

    async def do_risk(self, signal_id: uuid.UUID) -> None:
        await research.assess_signal(self.maker, signal_id, self.adapters, self.settings)

    async def do_backtest(self, run_id: uuid.UUID) -> None:
        await research.run_backtest(self.maker, run_id, self.settings)

    async def do_screener(self, run_id: uuid.UUID) -> None:
        status = await screener.execute_run(self.maker, run_id, self.settings)
        lane = self.lane(SCREENER)
        lane.extra[status] = int(lane.extra.get(status, 0)) + 1

    async def do_backfill(self, request_id: uuid.UUID) -> None:
        status = await market_data.run_backfill(
            self.maker, self.adapters.get("dhan"), request_id, self.settings
        )
        lane = self.lane(BACKFILL)
        lane.extra[status] = int(lane.extra.get(status, 0)) + 1

    async def do_ai(self, req: dict[str, Any]) -> None:
        why = ai.unavailable_reason(self.settings)
        if why is not None or self.gateway is None:
            raise RuntimeError(why or "no Claude gateway")
        await ai.explain(self.maker, self.gateway, req["kind"], uuid.UUID(req["id"]))

    # --- loops ---------------------------------------------------------------------------

    async def consume(self, spec: AgentSpec, handler: Callable[[Any], Awaitable[None]]) -> None:
        lane = self.lane(spec)
        while True:
            item = await lane.queue.get()
            await lane.running.wait()  # paused by an admin: hold the item until resumed
            lane.busy = True
            try:
                await handler(item)
                lane.processed += 1
                lane.last_ok_at = time.time()
            except Exception as exc:  # one bad item never stops the agent
                lane.errors += 1
                lane.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("agent_item_failed", agent=spec.id, error=lane.last_error)
                with contextlib.suppress(Exception):
                    await self.registry.log(spec.id, "error", lane.last_error)
            finally:
                lane.busy = False
                lane.queue.task_done()

    async def tick_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not self.lane(MARKET_DATA).paused:
                try:
                    await self.tick()
                except Exception as exc:
                    log.warning("tick_failed", error=type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self.settings.RESEARCH_TICK_SECONDS)

    async def backtest_loop(self, stop: asyncio.Event) -> None:
        lane = self.lane(BACKTEST)
        while not stop.is_set():
            run_id = None
            if not lane.paused and lane.queue.qsize() < lane.queue.maxsize:
                run_id = await research.claim_backtest(self.maker)
            if run_id is not None:
                lane.offer(run_id)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 5)

    async def screener_loop(self, stop: asyncio.Event) -> None:
        """Claim queued scans one at a time; purge runs past SCREENER_RETENTION_DAYS hourly."""
        lane = self.lane(SCREENER)
        last_purge = 0.0
        while not stop.is_set():
            if time.monotonic() - last_purge > 3600:
                last_purge = time.monotonic()
                with contextlib.suppress(Exception):
                    lane.extra["purged"] = await screener.purge_old(self.maker, self.settings)
            run_id = None
            if not lane.paused and lane.queue.empty() and not lane.busy:
                try:
                    run_id = await screener.claim_run(self.maker, self.settings)
                except Exception as exc:
                    log.warning("screener_claim_failed", error=type(exc).__name__)
            if run_id is not None:
                lane.offer(run_id)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 2)

    async def backfill_loop(self, stop: asyncio.Event) -> None:
        """One history request at a time: the Dhan data bucket (5/s, shared across processes
        through Redis) is the real throttle; this lane never runs requests in parallel."""
        lane = self.lane(BACKFILL)
        if "dhan" not in self.adapters:
            lane.extra["disabled"] = "Dhan is not configured: history requests stay queued"
            return
        while not stop.is_set():
            req_id = None
            if not lane.paused and lane.queue.empty() and not lane.busy:
                try:
                    req_id = await market_data.claim_backfill(self.maker)
                except Exception as exc:
                    log.warning("backfill_claim_failed", error=type(exc).__name__)
            if req_id is not None:
                lane.offer(req_id)
                await asyncio.sleep(0.2)
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 5)

    async def ai_loop(self, stop: asyncio.Event) -> None:
        lane = self.lane(AI_RESEARCH)
        assert self.redis is not None
        while not stop.is_set():
            if lane.paused or lane.queue.full():
                await asyncio.sleep(2)
                continue
            try:
                got = await self.redis.brpop([ai.QUEUE_KEY], timeout=5)
            except RedisTimeoutError:  # redis-py >= 8: an empty blocking pop (idle)
                continue
            if got:
                lane.offer(json.loads(got[1]))

    async def tick_bars_loop(self, stop: asyncio.Event) -> None:
        """Dhan feed ticks → 1m candles (only when the dhan-feed worker publishes).

        Never ends on its own (docs/28): a malformed packet is skipped and counted
        (`tick_errors`), a Redis error re-subscribes with backoff (`tick_resubscribes`), and
        a database error keeps the finished bars for the next attempt instead of losing them.
        The counters are in the market-data lane's heartbeat details."""
        assert self.redis is not None
        md = self.lane(MARKET_DATA)
        held: list[tuple[str, Any]] = []
        backoff = 1.0

        def bump(name: str) -> None:
            md.extra[name] = int(md.extra.get(name, 0)) + 1

        while not stop.is_set():
            pubsub = None
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(TICK_CHANNEL)
                backoff = 1.0
                while not stop.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5)
                    if held:
                        held = await self._store_bars(held)
                    if not msg or md.paused:
                        continue
                    try:
                        packet = json.loads(msg["data"])
                        bars = self.ticks.add(packet) if isinstance(packet, dict) else []
                    except (ValueError, TypeError, ArithmeticError):
                        bump("tick_errors")
                        continue
                    if bars:
                        held = await self._store_bars(held + bars)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Redis gone: resubscribe with backoff
                bump("tick_resubscribes")
                log.warning("tick_bars_resubscribe", error=type(exc).__name__)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), backoff)
                backoff = min(30.0, backoff * 2)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def _store_bars(self, bars: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
        """Store finished bars; on a DB error keep them (bounded) for the next attempt."""
        md = self.lane(MARKET_DATA)
        try:
            n = await research.store_tick_bars(self.maker, bars)
        except Exception as exc:
            md.extra["tick_store_errors"] = int(md.extra.get("tick_store_errors", 0)) + 1
            log.warning("tick_bars_store_failed", error=type(exc).__name__, held=len(bars))
            return bars[-5000:]
        md.extra["tick_bars"] = int(md.extra.get("tick_bars", 0)) + n
        return []

    def handlers(self) -> dict[str, Callable[[Any], Awaitable[None]]]:
        return {
            MARKET_DATA.id: self.do_market_data,
            TECHNICAL.id: self.do_technical,
            SIGNAL.id: self.do_signal,
            RISK.id: self.do_risk,
            BACKTEST.id: self.do_backtest,
            AI_RESEARCH.id: self.do_ai,
            SCREENER.id: self.do_screener,
            BACKFILL.id: self.do_backfill,
        }


async def main() -> int:
    settings = await app_settings.worker_settings()
    configure_logging(settings.LOG_LEVEL)
    engine = build_engine(settings)
    maker = build_sessionmaker(engine)
    redis = Redis.from_url(settings.REDIS_URL)
    adapters: dict[str, BrokerAdapter] = {}
    dhan = build_dhan_adapter(settings, redis, maker)
    if dhan is not None:
        adapters["dhan"] = dhan
    mt5 = build_mt5_adapter(settings)
    if mt5 is not None:
        adapters["mt5"] = mt5
    gateway = ClaudeGateway.build(settings, maker)
    worker = ResearchWorker(settings, maker, adapters, redis=redis, gateway=gateway)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    # A change to its keys from Spasta re-execs this worker (docs/settings.md).
    app_settings.start_worker_watch(settings, maker, stop, RELOAD_PREFIXES, redis)

    beats = []
    for spec in SPECS:
        lane = worker.lane(spec)

        def on_paused(p: bool, lane: AgentLane = lane) -> None:
            lane.set_paused(p)

        hb = Heartbeat(
            worker.registry, spec, settings.AGENT_HEARTBEAT_SECONDS, lane.state, on_paused
        )
        await hb.beat()
        hb.start()
        beats.append(hb)
    tasks = [
        asyncio.create_task(worker.consume(SPECS_BY_ID[k], h)) for k, h in worker.handlers().items()
    ]
    tasks += [
        asyncio.create_task(worker.tick_loop(stop)),
        asyncio.create_task(worker.backtest_loop(stop)),
        asyncio.create_task(worker.ai_loop(stop)),
        asyncio.create_task(worker.screener_loop(stop)),
        asyncio.create_task(worker.backfill_loop(stop)),
    ]
    if dhan is not None:
        tasks.append(asyncio.create_task(worker.tick_bars_loop(stop)))
    log.info("research_worker_started", adapters=sorted(adapters))
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for hb in beats:
            await hb.stop()
        if dhan is not None:
            await dhan.aclose()
        if mt5 is not None:
            await mt5.aclose()
        await gateway.aclose()
        await redis.aclose()
        await engine.dispose()
        log.info("research_worker_stopped")
    return 0


if __name__ == "__main__":
    app_settings.run_worker(main, "app.workers.research")
