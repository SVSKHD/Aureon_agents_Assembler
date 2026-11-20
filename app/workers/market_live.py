"""Market-live worker: live candles, quotes, movers and the live EMA/VWAP state (docs/38).

    python -m app.workers.market_live

Read-only and deterministic: it consumes the dhan-feed worker's `dhan:ticks` channel and the
stored candles, and writes only to Redis. It never calls Dhan (the feed worker owns the one
upstream socket), never writes trading.market_candles, signals or notifications, and never
places, drafts or changes an order. No AI model is involved.

Each cycle:

- **Ticks → quotes and bars.** Every feed packet updates the instrument's quote (LTP, day
  OHLC, cumulative volume, ATP, Dhan's previous close) and the live bars of
  MARKET_LIVE_TIMEFRAMES (lm.CandleBook). Bars close on a later packet or on the clock.
- **Publish** (every MARKET_LIVE_QUOTE_THROTTLE_MS, coalesced per instrument): quote views to
  `market:quote:<ref>` + channel `market:quotes`, forming bars to `market:bar:…`, closed bars
  appended to `market:bars:…`, both on `market:candles`.
- **Rankings** (every MARKET_LIVE_RANK_SECONDS): one row per universe instrument (quote view +
  average turnover + time-of-day relative volume) to `market:movers:<universe>`, and the top
  movers on `market:rankings`. The API re-ranks the rows with the caller's filters.
- **Live screener** (every MARKET_LIVE_SCREENER_SECONDS): the Stock Signals state of the bar
  that is still forming, from stored broker candles + today's live bars, to
  `market:screener:<tf>`; changed states on `market:screener` with `basis: live`. A finished
  Stock Signals run (confirmed, closed candles) is announced on the same channel with
  `basis: confirmed`. Live states are never stored as signals and never notify anyone.
- **Universe → feed:** the instruments of MARKET_LIVE_UNIVERSES are published to
  `dhan:feed:universe` for the feed worker's managed subscriptions (DHAN_FEED_DYNAMIC).
- **Continuity:** a feed reconnect (its `connects` / `last_connected_at` change) or a lost
  Redis subscription marks the forming bars partial and re-sets volume baselines.
- **One leader:** `market:live:leader` (Redis lock) keeps a second copy on standby.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.registry import AgentRegistry, AgentSpec, Heartbeat
from app.agents.research import live_market as lm
from app.agents.research import stock_rankings as rank
from app.agents.research import stock_signals as engine
from app.agents.trading.types import Timeframe
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.db.session import build_engine, build_sessionmaker
from app.models.stock_signals import StockSignalRun, StockSignalState
from app.models.trading import Instrument
from app.services import app_settings
from app.services import dhan_feed_health as fh
from app.services import market_live as ml
from app.services import screener as screener_svc
from app.services.market_session import candle_close
from app.services.research import tf_delta

log = get_logger("worker.market_live")
MARKET_LIVE = AgentSpec(
    "market-live", "Live market agent (quotes, live candles, movers)", "trading"
)
TICK_CHANNEL = "dhan:ticks"
STATUS_EVERY_S = 5.0
CONFIRMED_EVERY_S = 10.0
FEED_WATCH_S = 2.0
EQUITY_SEGMENTS = ("NSE_EQ", "BSE_EQ")
TOP_N = 10
HISTORY_RELOAD = timedelta(seconds=60)


@dataclass
class Counters:
    packets: int = 0
    bad_packets: int = 0
    quotes_published: int = 0
    candles_published: int = 0
    closed_published: int = 0
    rankings: int = 0
    screener_runs: int = 0
    screener_changes: int = 0
    confirmed_announced: int = 0
    redis_errors: int = 0
    db_errors: int = 0
    resubscribes: int = 0
    continuity_resets: int = 0
    last_error: str | None = None


@dataclass
class Reference:
    """What the worker knows about instruments, refreshed every
    MARKET_LIVE_REFERENCE_REFRESH_SECONDS from the database."""

    inst: dict[str, ml.InstInfo] = field(default_factory=dict)
    universes: dict[str, list[str]] = field(default_factory=dict)
    universe_total: dict[str, int] = field(default_factory=dict)
    daily: dict[str, dict[date, Decimal]] = field(default_factory=dict)
    avg_turnover: dict[str, float] = field(default_factory=dict)
    profiles: dict[str, list[float] | None] = field(default_factory=dict)
    loaded_at: datetime | None = None


class LiveRuntime:
    def __init__(
        self,
        settings: Settings,
        redis: Any,
        maker: async_sessionmaker[AsyncSession] | None,
        shutdown: asyncio.Event,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep_unit: float = 1.0,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.maker = maker
        self.shutdown = shutdown
        self.clock = clock or (lambda: datetime.now(UTC))
        self.unit = sleep_unit
        self.instance = f"{os.getpid()}-{secrets.token_hex(4)}"
        self.tfs = [Timeframe(t) for t in settings.MARKET_LIVE_TIMEFRAMES]
        self.screener_tfs = [Timeframe(t) for t in settings.MARKET_LIVE_SCREENER_TIMEFRAMES]
        # The live screener needs live bars of its timeframes.
        for tf in self.screener_tfs:
            if tf not in self.tfs:
                self.tfs.append(tf)
        self.book = lm.CandleBook(self.tfs, grace_seconds=settings.MARKET_LIVE_CLOSE_GRACE_SECONDS)
        self.quotes: dict[str, lm.LiveQuote] = {}
        self.ref = Reference()
        self.dirty_quotes: set[str] = set()
        self.dirty_bars: dict[tuple[str, Timeframe], lm.LiveBar] = {}
        self.closed_out: list[lm.LiveBar] = []
        # Today's closed live bars per (ref, timeframe), for the live screener.
        self.live_closed: dict[tuple[str, Timeframe], list[lm.LiveBar]] = {}
        self.history: dict[Timeframe, dict[str, list[Any]]] = {}
        self.history_at: dict[Timeframe, datetime] = {}
        self.screener_prev: dict[tuple[str, str], str] = {}
        self.confirmed_seen: dict[str, str] = {}
        self.feed_epoch: tuple[Any, Any] | None = None
        self.feed_status: dict[str, Any] | None = None
        self.counters = Counters()
        self.leader = False
        self.paused = False
        self.state_name = "starting"
        self.last_flush_at: datetime | None = None
        self.last_rank_at: datetime | None = None
        self.last_screener_at: datetime | None = None
        self._tasks: list[asyncio.Task[Any]] = []

    # --- ticks ----------------------------------------------------------------------------

    def handle_packet(self, packet: dict[str, Any], now: datetime | None = None) -> None:
        """One `dhan:ticks` message (the feed worker's FeedPacket JSON)."""
        now = now or self.clock()
        seg, sid = packet.get("segment"), str(packet.get("security_id") or "")
        if not seg or not sid.isdigit():
            self.counters.bad_packets += 1
            return
        key = f"{seg}:{sid}"
        self.counters.packets += 1
        received = lm.ts_of(packet.get("received_at")) or now
        q = self.quotes.get(key)
        if q is None:
            q = self.quotes[key] = lm.LiveQuote(key)
        if not q.merge(packet, received):
            return
        self.dirty_quotes.add(key)
        if packet.get("kind") not in ("ticker", "quote", "full"):
            return
        full = packet.get("kind") in ("quote", "full")
        bars = self.book.apply(
            key,
            ltt=lm.ts_of(packet.get("ltt")),
            ltp=lm.dec(packet.get("ltp")),
            cum_volume=lm.dec(packet.get("volume")),
            day_open=lm.dec(packet.get("open")) if full else None,
            day_high=lm.dec(packet.get("high")) if full else None,
            day_low=lm.dec(packet.get("low")) if full else None,
        )
        self._take(bars)

    def _take(self, bars: list[lm.LiveBar]) -> None:
        for b in bars:
            k = (b.key, b.tf)
            if b.closed:
                self.closed_out.append(b)
                self.dirty_bars.pop(k, None)
                lst = self.live_closed.setdefault(k, [])
                lst.append(b)
                del lst[: -ml.BARS_KEEP]
            else:
                self.dirty_bars[k] = b

    def close_due(self, now: datetime | None = None) -> int:
        closed = self.book.close_due(now or self.clock())
        self._take(closed)
        return len(closed)

    def reset_continuity(self, why: str) -> None:
        self.book.reset_continuity()
        self.counters.continuity_resets += 1
        for b in self.book.forming.values():
            self.dirty_bars[(b.key, b.tf)] = b
        log.info("market_live_continuity_reset", reason=why)

    # --- publishing -----------------------------------------------------------------------

    def _with_inst(self, key: str, data: dict[str, Any]) -> dict[str, Any]:
        inst = self.ref.inst.get(key)
        return {**data, **(inst.as_json() if inst else {"instrument_id": None}), "ref": key}

    def quote_view(self, key: str, now: datetime) -> dict[str, Any]:
        q = self.quotes[key]
        m = fh.market_state(now, self.settings.DHAN_MARKET_HOLIDAYS)
        view = lm.quote_view(
            q,
            now,
            daily_closes=self.ref.daily.get(key, {}),
            market_open=bool(m["open"]),
            stale_after=self.settings.DHAN_FEED_STALE_SECONDS,
            holidays=self.settings.DHAN_MARKET_HOLIDAYS,
        )
        return self._with_inst(key, view)

    async def flush(self, now: datetime | None = None) -> int:
        """Write and publish what changed since the last flush. On a Redis error everything is
        kept for the next attempt (closed bars bounded to the newest 5000)."""
        now = now or self.clock()
        closed, bars, quotes = self.closed_out, self.dirty_bars, self.dirty_quotes
        if not (closed or bars or quotes):
            return 0
        n = 0
        try:
            async with self.redis.pipeline(transaction=False) as pipe:
                for b in closed:
                    data = self._with_inst(b.key, b.as_json())
                    lkey = ml.BARS_KEY.format(tf=b.tf.value, ref=b.key)
                    pipe.rpush(lkey, json.dumps(b.as_json()))
                    pipe.ltrim(lkey, -ml.BARS_KEEP, -1)
                    pipe.expire(lkey, ml.TTL)
                    pipe.publish(ml.CH_CANDLES, json.dumps({"type": "candle", **data}))
                for (key, tf), b in bars.items():
                    data = self._with_inst(key, b.as_json())
                    pipe.set(
                        ml.BAR_KEY.format(tf=tf.value, ref=key), json.dumps(b.as_json()), ex=ml.TTL
                    )
                    pipe.publish(ml.CH_CANDLES, json.dumps({"type": "candle", **data}))
                for key in quotes:
                    view = self.quote_view(key, now)
                    pipe.set(ml.QUOTE_KEY.format(ref=key), json.dumps(view), ex=ml.TTL)
                    pipe.publish(ml.CH_QUOTES, json.dumps({"type": "quote", **view}))
                await pipe.execute()
        except Exception as exc:
            self.counters.redis_errors += 1
            self.counters.last_error = type(exc).__name__
            del self.closed_out[:-5000]
            log.warning("market_live_flush_failed", error=type(exc).__name__)
            return 0
        n = len(closed) + len(bars) + len(quotes)
        self.counters.closed_published += len(closed)
        self.counters.candles_published += len(closed) + len(bars)
        self.counters.quotes_published += len(quotes)
        self.closed_out, self.dirty_bars, self.dirty_quotes = [], {}, set()
        self.last_flush_at = now
        return n

    # --- rankings ---------------------------------------------------------------------------

    def ranking_rows(self, name: str, now: datetime) -> list[dict[str, Any]]:
        rows = []
        for key in self.ref.universes.get(name, []):
            inst = self.ref.inst.get(key)
            if key in self.quotes:
                view = self.quote_view(key, now)
            else:
                view = self._with_inst(key, {"key": key, "ltp": None, "status": "no_data"})
            q = self.quotes.get(key)
            rvol = lm.live_rvol(q.volume if q else None, self.ref.profiles.get(key), now)
            view["avg_turnover"] = self.ref.avg_turnover.get(key)
            view["rvol"] = round(rvol, 4) if rvol is not None else None
            view["exchange"] = inst.exchange if inst else None
            rows.append(view)
        return rows

    async def compute_rankings(self, now: datetime | None = None) -> int:
        now = now or self.clock()
        m = ml.market(now, self.settings.DHAN_MARKET_HOLIDAYS)
        done = 0
        for name, keys in self.ref.universes.items():
            rows = self.ranking_rows(name, now)
            ltts = [r["ltt"] for r in rows if r.get("ltt")]
            total = self.ref.universe_total.get(name, len(keys))
            snap = {
                "universe": name,
                "computed_at": now.isoformat(),
                "market": m,
                "universe_size": len(keys),
                "universe_total": total,
                "capped": total > len(keys),
                "subscribed": sum(1 for k in keys if k in self.quotes),
                "quotes_as_of": max(ltts) if ltts else None,
                "rows": rows,
            }
            event = {"type": "rankings", **ml.default_movers(snap, self.settings, TOP_N)}
            try:
                await self.redis.set(
                    ml.MOVERS_KEY.format(universe=name), json.dumps(snap), ex=ml.TTL
                )
                await self.redis.publish(ml.CH_RANKINGS, json.dumps(event))
            except Exception as exc:
                self.counters.redis_errors += 1
                self.counters.last_error = type(exc).__name__
                continue
            done += 1
        self.counters.rankings += done
        self.last_rank_at = now
        return done

    # --- the live screener ----------------------------------------------------------------

    def screener_keys(self) -> list[str]:
        keys: dict[str, None] = {}
        for refs in self.ref.universes.values():
            for k in refs:
                inst = self.ref.inst.get(k)
                if inst is not None and inst.segment in EQUITY_SEGMENTS:
                    keys[k] = None
        return list(keys)

    async def load_history(self, tf: Timeframe, now: datetime) -> None:
        """Stored broker candles of `tf` for the screener universe (closed ones only). Reloaded
        at most every HISTORY_RELOAD (or one bar, if shorter) so the history the Stock Signals
        lane fetches after each close is picked up soon after it lands."""
        if self.maker is None:
            return
        at = self.history_at.get(tf)
        if at is not None and now - at < min(tf_delta(tf), HISTORY_RELOAD):
            return
        keys = self.screener_keys()
        ids = {self.ref.inst[k].id: k for k in keys if k in self.ref.inst}
        need = max(engine.DEFAULT.needed, self.settings.STOCK_SIGNALS_LOOKBACK_BARS) + 10
        out: dict[str, list[Any]] = {}
        id_list = list(ids)
        try:
            for i in range(0, len(id_list), 500):
                async with self.maker() as db:
                    got = await screener_svc.load_bars(db, id_list[i : i + 500], tf, need, now)
                for iid, bars in got.items():
                    # Only candles that had closed: the forming one comes from the ticks.
                    out[ids[iid]] = [b for b in bars if candle_close(b.ts, tf) <= now]
        except Exception as exc:
            self.counters.db_errors += 1
            self.counters.last_error = type(exc).__name__
            log.warning("market_live_history_failed", tf=tf.value, error=type(exc).__name__)
            return
        self.history[tf] = out
        self.history_at[tf] = now

    def evaluate_live(self, tf: Timeframe, now: datetime) -> list[dict[str, Any]]:
        """CPU work (run in a thread): the live state per screener instrument."""
        from app.services.stock_signals import params as signal_params

        p = signal_params(self.settings)
        weights = self.settings.STOCK_SIGNALS_SCORE_WEIGHTS
        hist = self.history.get(tf, {})
        rows = []
        for key in self.screener_keys():
            inst = self.ref.inst.get(key)
            s = lm.merge_series(
                hist.get(key, []),
                self.live_closed.get((key, tf), []),
                self.book.get(key, tf),
                tf,
                self.settings.DHAN_MARKET_HOLIDAYS,
            )
            reading, flags = lm.live_reading(s, tf, now, p, self.settings.DHAN_MARKET_HOLIDAYS)
            score = None
            if reading.state is not engine.State.UNAVAILABLE and not s.frame.empty:
                f = rank.features(
                    s.frame,
                    ema_fast=float(reading.ema_fast) if reading.ema_fast is not None else None,
                    ema_slow=float(reading.ema_slow) if reading.ema_slow is not None else None,
                    vwap=float(reading.vwap) if reading.vwap is not None else None,
                    weights=weights,
                )
                score = f.score
            rows.append(
                {
                    **(inst.as_json() if inst else {"instrument_id": None}),
                    "ref": key,
                    "timeframe": tf.value,
                    "basis": "live",
                    "state": reading.state.value,
                    "last_price": lm._s(reading.last),
                    "ema_fast": lm._s(reading.ema_fast),
                    "ema_slow": lm._s(reading.ema_slow),
                    "vwap": lm._s(reading.vwap),
                    "trend_score": score,
                    "crossed_at": reading.crossed_at.isoformat() if reading.crossed_at else None,
                    "confirmed_at": (
                        reading.confirmed_at.isoformat() if reading.confirmed_at else None
                    ),
                    "candle_ts": reading.candle_ts.isoformat() if reading.candle_ts else None,
                    "reason": reading.reason,
                    **reading.metrics(),
                    **flags,
                }
            )
        return rows

    async def compute_screener(self, now: datetime | None = None) -> int:
        now = now or self.clock()
        changes = 0
        for tf in self.screener_tfs:
            await self.load_history(tf, now)
            rows = await asyncio.to_thread(self.evaluate_live, tf, now)
            changed = []
            for r in rows:
                k = (r["ref"], tf.value)
                if self.screener_prev.get(k) != r["state"]:
                    if k in self.screener_prev:
                        changed.append({**r, "previous_state": self.screener_prev[k]})
                    self.screener_prev[k] = r["state"]
            snap = {
                "timeframe": tf.value,
                "basis": "live",
                "computed_at": now.isoformat(),
                "market": ml.market(now, self.settings.DHAN_MARKET_HOLIDAYS),
                "universes": sorted(self.ref.universes),
                "rows": rows,
            }
            try:
                await self.redis.set(
                    ml.SCREENER_KEY.format(tf=tf.value), json.dumps(snap), ex=ml.TTL
                )
                if changed:
                    await self.redis.publish(
                        ml.CH_SCREENER,
                        json.dumps(
                            {
                                "type": "screener",
                                "basis": "live",
                                "timeframe": tf.value,
                                "computed_at": now.isoformat(),
                                "changes": changed[:500],
                            }
                        ),
                    )
            except Exception as exc:
                self.counters.redis_errors += 1
                self.counters.last_error = type(exc).__name__
                continue
            changes += len(changed)
        self.counters.screener_runs += 1
        self.counters.screener_changes += changes
        self.last_screener_at = now
        return changes

    async def announce_confirmed(self) -> int:
        """A newly finished Stock Signals run → one `screener` event with basis `confirmed`
        and the instruments whose confirmed state changed in it."""
        if self.maker is None:
            return 0
        sent = 0
        async with self.maker() as db:
            for tf in self.screener_tfs:
                run = await db.scalar(
                    select(StockSignalRun)
                    .where(StockSignalRun.timeframe == tf.value, StockSignalRun.status == "done")
                    .order_by(StockSignalRun.boundary.desc())
                    .limit(1)
                )
                if run is None or self.confirmed_seen.get(tf.value) == str(run.id):
                    continue
                first = tf.value not in self.confirmed_seen
                self.confirmed_seen[tf.value] = str(run.id)
                if first:
                    continue  # don't replay the run that was already there at startup
                rows = (
                    await db.execute(
                        select(StockSignalState, Instrument)
                        .join(Instrument, Instrument.id == StockSignalState.instrument_id)
                        .where(
                            StockSignalState.run_id == run.id,
                            StockSignalState.timeframe == tf.value,
                            StockSignalState.state_changed_at >= run.started_at,
                        )
                        .limit(500)
                    )
                ).all()
                changes = [
                    {
                        "instrument_id": inst.id,
                        "ref": inst.broker_instrument_id,
                        "symbol": inst.trading_symbol or inst.symbol,
                        "state": st.state,
                        "previous_state": st.previous_state,
                        "candle_close_at": (
                            st.candle_close_at.isoformat() if st.candle_close_at else None
                        ),
                        "event_id": st.event_id,
                    }
                    for st, inst in rows
                ]
                await self.redis.publish(
                    ml.CH_SCREENER,
                    json.dumps(
                        {
                            "type": "screener",
                            "basis": "confirmed",
                            "timeframe": tf.value,
                            "run_id": str(run.id),
                            "boundary": run.boundary.isoformat(),
                            "evaluated": run.evaluated,
                            "unavailable": run.unavailable,
                            "changes": changes,
                        }
                    ),
                )
                sent += 1
        self.counters.confirmed_announced += sent
        return sent

    # --- reference data -------------------------------------------------------------------

    async def refresh_reference(self, now: datetime | None = None) -> None:
        if self.maker is None:
            return
        now = now or self.clock()
        cap = self.settings.MARKET_LIVE_MAX_UNIVERSE
        ref = Reference(loaded_at=now)
        async with self.maker() as db:
            for name in self.settings.MARKET_LIVE_UNIVERSES:
                items, total = await ml.universe(db, name, cap)
                ref.universes[name] = [i.ref for i in items]
                ref.universe_total[name] = total
                ref.inst.update({i.ref: i for i in items})
            # Instruments seen on the feed but outside the universes (a browser watches them).
            extra = [k for k in self.quotes if k not in ref.inst]
            ref.inst.update(await ml.instruments_by_ref(db, extra))
            by_id = {i.id: k for k, i in ref.inst.items()}
            closes, turnover = await ml.daily_reference(db, list(by_id), now)
            universe_ids = {ref.inst[k].id for refs in ref.universes.values() for k in refs}
            profiles = await ml.volume_profiles(db, sorted(universe_ids), now)
        ref.daily = {by_id[i]: v for i, v in closes.items() if i in by_id}
        ref.avg_turnover = {by_id[i]: v for i, v in turnover.items() if i in by_id}
        ref.profiles = {by_id[i]: v for i, v in profiles.items() if i in by_id}
        self.ref = ref
        today = lm.session_day(now)
        for k in list(self.live_closed):
            self.live_closed[k] = [b for b in self.live_closed[k] if lm.session_day(b.ts) == today]
        await self.publish_universe()
        # Every quote's change may now resolve against a newly stored daily close.
        self.dirty_quotes |= set(self.quotes)

    async def publish_universe(self) -> None:
        keys = sorted({k for refs in self.ref.universes.values() for k in refs})
        try:
            async with self.redis.pipeline(transaction=False) as pipe:
                pipe.delete(fh.UNIVERSE_KEY)
                if keys:
                    pipe.sadd(fh.UNIVERSE_KEY, *keys)
                    pipe.expire(fh.UNIVERSE_KEY, ml.TTL)
                await pipe.execute()
        except Exception as exc:
            self.counters.redis_errors += 1
            self.counters.last_error = type(exc).__name__

    async def restore(self, now: datetime | None = None) -> int:
        """After a restart: today's closed live bars and the forming bars (kept as partial)."""
        now = now or self.clock()
        n = 0
        today = lm.session_day(now)
        for key in {k for refs in self.ref.universes.values() for k in refs}:
            for tf in self.tfs:
                try:
                    closed, forming = await ml.read_bars(self.redis, key, tf)
                except Exception:
                    return n
                today_bars = [b for b in closed if lm.session_day(b.ts) == today]
                if today_bars:
                    self.live_closed[(key, tf)] = today_bars
                    self.book.last_closed[(key, tf)] = today_bars[-1].ts
                    n += len(today_bars)
                if forming is not None and forming.end > now:
                    self.book.restore(forming)
                    n += 1
        return n

    # --- feed continuity ------------------------------------------------------------------

    async def watch_feed_once(self) -> None:
        raw = await self.redis.get(fh.STATUS_KEY)
        st = fh.parse_status(raw)
        self.feed_status = st
        if st is None:
            return
        epoch = (st.get("connects"), st.get("last_connected_at"))
        if not st.get("connected"):
            epoch = ("down", None)
        if self.feed_epoch is not None and epoch != self.feed_epoch:
            self.reset_continuity("feed_reconnected")
        self.feed_epoch = epoch

    # --- status -----------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        c = self.book.counters
        return {
            "state": self.state_name,
            "leader": self.leader,
            "instance": self.instance,
            "universes": {k: len(v) for k, v in self.ref.universes.items()},
            "instruments_with_quotes": len(self.quotes),
            "forming_bars": len(self.book.forming),
            "timeframes": [tf.value for tf in self.tfs],
            "screener_timeframes": [tf.value for tf in self.screener_tfs],
            "reference_loaded_at": self.ref.loaded_at.isoformat() if self.ref.loaded_at else None,
            "last_flush_at": self.last_flush_at.isoformat() if self.last_flush_at else None,
            "last_rank_at": self.last_rank_at.isoformat() if self.last_rank_at else None,
            "last_screener_at": (
                self.last_screener_at.isoformat() if self.last_screener_at else None
            ),
            "book": dict(c.__dict__),
            "counters": dict(self.counters.__dict__),
            "feed_connected": bool(self.feed_status and self.feed_status.get("connected")),
            "updated_at": time.time(),
        }

    def heartbeat_state(self) -> tuple[str, dict[str, Any]]:
        details = self.status()
        if self.state_name in ("standby", "paused", "starting", "disabled"):
            return "idle", details
        return "busy" if self.leader else "idle", details

    async def publish_status(self) -> None:
        with contextlib.suppress(Exception):
            if not self.leader and await self.redis.get(ml.LEADER_KEY) is not None:
                return
            await self.redis.set(ml.STATUS_KEY, json.dumps(self.status()), ex=ml.STATUS_TTL)

    # --- leadership -------------------------------------------------------------------------

    async def acquire_or_renew(self) -> bool:
        try:
            got = await self.redis.set(ml.LEADER_KEY, self.instance, nx=True, ex=ml.LEADER_TTL)
            if got:
                return True
            cur = await self.redis.get(ml.LEADER_KEY)
            mine = (cur.decode() if isinstance(cur, bytes) else cur) == self.instance
            if mine:
                await self.redis.expire(ml.LEADER_KEY, ml.LEADER_TTL)
            return mine
        except Exception:
            return self.leader  # a Redis blip keeps the current role (the lock has a TTL)

    async def release(self) -> None:
        with contextlib.suppress(Exception):
            cur = await self.redis.get(ml.LEADER_KEY)
            if (cur.decode() if isinstance(cur, bytes) else cur) == self.instance:
                await self.redis.delete(ml.LEADER_KEY)

    # --- loops ------------------------------------------------------------------------------

    async def _wait(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.shutdown.wait(), seconds * self.unit)

    async def ticks_loop(self) -> None:
        backoff = 1.0
        first = True
        while not self.shutdown.is_set():
            pubsub = None
            try:
                pubsub = self.redis.pubsub()
                await pubsub.subscribe(TICK_CHANNEL)
                if not first:
                    # Messages published while we were not subscribed are lost.
                    self.counters.resubscribes += 1
                    self.reset_continuity("tick_subscription_restored")
                first, backoff = False, 1.0
                while not self.shutdown.is_set():
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if not msg or not self.leader or self.paused:
                        continue
                    try:
                        packet = json.loads(msg["data"])
                    except (ValueError, TypeError):
                        self.counters.bad_packets += 1
                        continue
                    if isinstance(packet, dict):
                        self.handle_packet(packet)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.counters.redis_errors += 1
                self.counters.last_error = type(exc).__name__
                log.warning("market_live_tick_subscription_lost", error=type(exc).__name__)
                await self._wait(backoff)
                backoff = min(30.0, backoff * 2)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()

    async def _every(self, seconds: float, fn: Callable[[], Any], name: str) -> None:
        while not self.shutdown.is_set():
            if self.leader and not self.paused:
                try:
                    await fn()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.counters.last_error = type(exc).__name__
                    log.warning("market_live_step_failed", step=name, error=type(exc).__name__)
            await self._wait(seconds)

    async def clock_step(self) -> None:
        self.close_due()
        await self.flush()

    async def feed_step(self) -> None:
        await self.watch_feed_once()

    async def reference_step(self) -> None:
        await self.refresh_reference()

    async def run(self) -> int:
        if not self.settings.MARKET_LIVE_ENABLED:
            self.state_name = "disabled"
            while not self.shutdown.is_set():
                await self.publish_status()
                await self._wait(STATUS_EVERY_S)
            return 0
        s = self.settings
        throttle = max(s.MARKET_LIVE_QUOTE_THROTTLE_MS, 50) / 1000
        self._tasks = [
            asyncio.create_task(self.ticks_loop(), name="market-live-ticks"),
            asyncio.create_task(self._every(throttle, self.clock_step, "flush"), name="ml-flush"),
            asyncio.create_task(self._every(FEED_WATCH_S, self.feed_step, "feed"), name="ml-feed"),
            asyncio.create_task(
                self._every(s.MARKET_LIVE_RANK_SECONDS, self.compute_rankings, "rankings"),
                name="ml-rank",
            ),
            asyncio.create_task(
                self._every(s.MARKET_LIVE_SCREENER_SECONDS, self.compute_screener, "screener"),
                name="ml-screener",
            ),
            asyncio.create_task(
                self._every(CONFIRMED_EVERY_S, self.announce_confirmed, "confirmed"),
                name="ml-confirmed",
            ),
            asyncio.create_task(
                self._every(
                    s.MARKET_LIVE_REFERENCE_REFRESH_SECONDS, self.reference_step, "reference"
                ),
                name="ml-reference",
            ),
        ]
        restored = False
        try:
            while not self.shutdown.is_set():
                was = self.leader
                self.leader = await self.acquire_or_renew()
                if self.leader and not was:
                    log.info("market_live_leader")
                    if not restored:
                        with contextlib.suppress(Exception):
                            await self.refresh_reference()
                            await self.restore()
                        restored = True
                self.state_name = (
                    "paused" if self.paused else ("running" if self.leader else "standby")
                )
                await self.publish_status()
                await self._wait(STATUS_EVERY_S)
        finally:
            for t in self._tasks:
                t.cancel()
            for t in self._tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            with contextlib.suppress(Exception):
                await self.flush()
            self.state_name = "stopped"
            await self.publish_status()
            await self.release()
        return 0


async def main() -> int:
    settings: Settings = await app_settings.worker_settings()
    configure_logging(settings.LOG_LEVEL)
    db_engine = build_engine(settings)
    maker = build_sessionmaker(db_engine)
    registry = AgentRegistry(maker)
    redis = Redis.from_url(settings.REDIS_URL)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown.set)
    app_settings.start_worker_watch(settings, maker, shutdown, ("MARKET_LIVE_", "DHAN_"), redis)
    rt = LiveRuntime(settings, redis, maker, shutdown)

    def on_paused(p: bool) -> None:
        rt.paused = p

    hb = Heartbeat(
        registry, MARKET_LIVE, settings.AGENT_HEARTBEAT_SECONDS, rt.heartbeat_state, on_paused
    )
    hb.start()
    try:
        return await rt.run()
    finally:
        await hb.stop()
        await redis.aclose()
        await db_engine.dispose()
        log.info("market_live_stopped")


if __name__ == "__main__":
    app_settings.run_worker(main, "app.workers.market_live")
