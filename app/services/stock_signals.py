"""Stock Signals runs (docs/37-stock-signals.md): universe → fresh candles → engine → storage.

A run evaluates one timeframe at one candle close (its `boundary`):

1. The universe: STOCK_SIGNALS_UNIVERSES (NSE/BSE equities only), capped.
2. History: one `system` request per instrument on the existing candle-backfill queue (the
   research worker's candle-backfill lane does the rate-limited Dhan calls and fetches only
   bars newer than the newest stored one; a person's own chart request still goes first).
   The run waits for those, up to STOCK_SIGNALS_FETCH_TIMEOUT_SECONDS, then evaluates what is
   stored: an instrument Dhan did not deliver is UNAVAILABLE (stale), never guessed.
3. The engine (app/agents/research/stock_signals.py) on candles that closed by the boundary.
4. Storage: the latest state per (instrument, timeframe), and one event per crossover
   (unique key; a VWAP confirmation updates it, it is never a second event).

Runs are rows (trading.stock_signal_runs, unique per timeframe and boundary), claimed with
SKIP LOCKED; a run left `running` by a stopped worker is requeued after an hour, and a failed
run is retried up to STOCK_SIGNALS_MAX_ATTEMPTS. Read-only market data: nothing here places,
changes or cancels an order.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import and_, case, delete, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.research import indicators as ind
from app.agents.research import stock_signals as engine
from app.agents.trading.types import Timeframe
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.stock_signals import StockSignalEvent, StockSignalRun, StockSignalState
from app.models.trading import CandleBackfillRequest, Instrument
from app.services import market_data, market_session
from app.services.screener import Bar, load_bars

log = get_logger("stock_signals")

EQUITY_SEGMENTS = ("NSE_EQ", "BSE_EQ")
BARS_PER_DAY = {Timeframe.M5: 75, Timeframe.M15: 25, Timeframe.H1: 7, Timeframe.D1: 1}
CHUNK = 200
POLL_SECONDS = 3.0
STUCK = timedelta(hours=1)
RUN_RETENTION = timedelta(days=14)


@dataclass
class RunResult:
    """What a run changed: the input for notifications (docs/37 §5)."""

    status: str
    new_events: list[int] = field(default_factory=list)
    confirmed_events: list[int] = field(default_factory=list)
    # (instrument_id, timeframe, previous state, new state) where the state changed
    changes: list[tuple[int, str, str, str]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def params(settings: Settings) -> engine.Params:
    return engine.Params(
        fast=settings.STOCK_SIGNALS_EMA_FAST,
        slow=settings.STOCK_SIGNALS_EMA_SLOW,
        confirm_bars=settings.STOCK_SIGNALS_CONFIRM_BARS,
        stale_bars=settings.STOCK_SIGNALS_STALE_BARS,
    )


def timeframes(settings: Settings) -> list[Timeframe]:
    return [Timeframe(t) for t in settings.STOCK_SIGNALS_TIMEFRAMES]


def history_days(tf: Timeframe, settings: Settings) -> int:
    """Calendar days of history covering the lookback (weekends included), within Dhan's cap."""
    sessions = settings.STOCK_SIGNALS_LOOKBACK_BARS / BARS_PER_DAY[tf]
    days = math.ceil(sessions * 7 / 5) + 3
    return max(1, min(days, market_data.max_days(tf, settings)))


# --- universe ---------------------------------------------------------------------------------


async def universe(db: AsyncSession, settings: Settings) -> list[int]:
    """The configured universes, NSE/BSE equities only, deduplicated and capped."""
    ids: list[int] = []
    for name in settings.STOCK_SIGNALS_UNIVERSES:
        ids += await market_data.universe_ids(db, name)
    if not ids:
        return []
    keep = (
        await db.scalars(
            select(Instrument.id)
            .where(
                Instrument.id.in_(set(ids)),
                Instrument.broker == "dhan",
                Instrument.is_active.is_(True),
                Instrument.segment.in_(EQUITY_SEGMENTS),
                Instrument.instrument_type == "EQUITY",
            )
            .order_by(Instrument.id)
        )
    ).all()
    return list(keep)[: settings.STOCK_SIGNALS_MAX_UNIVERSE]


# --- runs -------------------------------------------------------------------------------------


def due_boundary(tf: Timeframe, now: datetime, settings: Settings) -> datetime | None:
    """The candle close a run should evaluate now, once STOCK_SIGNALS_RUN_DELAY has passed."""
    hol = settings.DHAN_MARKET_HOLIDAYS
    delay = timedelta(seconds=settings.STOCK_SIGNALS_RUN_DELAY_SECONDS)
    if tf is Timeframe.D1:
        day = now.astimezone(market_session.IST).date()
        if not market_session.is_trading_day(day, hol):
            return None
        close = market_session.session_bounds(day)[1]
        return close if now >= close + delay else None
    b = market_session.latest_boundary(now - delay, tf, hol)
    return b


async def ensure_run(
    maker: async_sessionmaker[AsyncSession], tf: Timeframe, boundary: datetime, trigger: str
) -> uuid.UUID | None:
    """Queue the run for (tf, boundary) unless one exists; returns the new id or None."""
    async with maker() as db, db.begin():
        return await db.scalar(
            insert(StockSignalRun)
            .values(timeframe=tf.value, boundary=boundary, trigger=trigger)
            .on_conflict_do_nothing(constraint="uq_stock_signal_runs_tf_boundary")
            .returning(StockSignalRun.id)
        )


async def queue_due_runs(
    maker: async_sessionmaker[AsyncSession], settings: Settings, now: datetime | None = None
) -> list[uuid.UUID]:
    """The candle-close trigger: one run per configured timeframe whose newest close has no
    run yet. Called by the worker every few seconds; idempotent."""
    if not settings.STOCK_SIGNALS_ENABLED:
        return []
    now = now or datetime.now(UTC)
    out = []
    for tf in timeframes(settings):
        b = due_boundary(tf, now, settings)
        if b is not None:
            rid = await ensure_run(maker, tf, b, "candle-close")
            if rid is not None:
                out.append(rid)
    return out


async def claim_run(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID | None:
    """Next queued run → running (SKIP LOCKED); a run stuck running for an hour is requeued."""
    async with maker() as db, db.begin():
        await db.execute(
            update(StockSignalRun)
            .where(
                StockSignalRun.status == "running",
                StockSignalRun.started_at < func.now() - STUCK,
            )
            .values(status="queued")
        )
        row = await db.scalar(
            select(StockSignalRun)
            .where(StockSignalRun.status == "queued")
            .order_by(StockSignalRun.boundary, StockSignalRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        row.status, row.started_at = "running", datetime.now(UTC)
        row.attempts += 1
        return row.id


async def _request_history(
    maker: async_sessionmaker[AsyncSession],
    ids: Sequence[int],
    tf: Timeframe,
    settings: Settings,
) -> int:
    days = history_days(tf, settings)
    queued = 0
    async with maker() as db, db.begin():
        for i in range(0, len(ids), 1000):
            res = await market_data.enqueue_backfill(
                db, ids[i : i + 1000], tf, days, settings, source="system"
            )
            queued += res.queued
    return queued


async def _open_requests(
    maker: async_sessionmaker[AsyncSession], ids: Sequence[int], tf: Timeframe
) -> int:
    async with maker() as db:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(CandleBackfillRequest)
                .where(
                    CandleBackfillRequest.instrument_id.in_(ids),
                    CandleBackfillRequest.timeframe == tf.value,
                    CandleBackfillRequest.status.in_(market_data.OPEN),
                )
            )
            or 0
        )


def _frame(bars: Sequence[Bar]) -> pd.DataFrame:
    return ind.frame(bars) if bars else ind.frame([])


def _evaluate_chunk(
    bars: dict[int, list[Bar]],
    intraday: dict[int, list[Bar]] | None,
    tf: Timeframe,
    boundary: datetime,
    p: engine.Params,
    holidays: Sequence[Any],
) -> dict[int, engine.Reading]:
    out = {}
    for iid, rows in bars.items():
        vw = None
        if intraday is not None and intraday.get(iid):
            vw = engine.daily_vwap(_frame(intraday[iid]))
        out[iid] = engine.evaluate(_frame(rows), tf, boundary, p, day_vwap=vw, holidays=holidays)
    return out


async def _store(
    db: AsyncSession,
    run_id: uuid.UUID,
    insts: dict[int, Instrument],
    tf: Timeframe,
    readings: dict[int, engine.Reading],
    result: RunResult,
) -> None:
    now = datetime.now(UTC)
    for iid, r in readings.items():
        result.counts[r.state.value] = result.counts.get(r.state.value, 0) + 1
        event_id = None
        if r.is_cross and r.crossed_ts is not None and r.crossed_at is not None:
            inst = insts[iid]
            values = {
                "instrument_id": iid,
                "exchange": (inst.exchange or inst.segment or "")[:16],
                "timeframe": tf.value,
                "direction": r.direction,
                "state": r.state.value,
                "crossed_ts": r.crossed_ts,
                "crossed_at": r.crossed_at,
                "confirmed_at": r.confirmed_at,
                "ema_fast": r.ema_fast,
                "ema_slow": r.ema_slow,
                "vwap": r.vwap,
                "last_price": r.cross_close,
                "confirm_price": r.confirm_close,
                "confirm_vwap": r.confirm_vwap,
                "candle_close_at": r.candle_close_at,
                "data_as_of": r.candle_close_at,
                "metrics": r.metrics(),
                "run_id": run_id,
            }
            ins = insert(StockSignalEvent).values(values)
            ev = StockSignalEvent.__table__.c
            stmt: Any = ins.on_conflict_do_update(
                constraint="uq_stock_signal_events_cross",
                set_={
                    "state": ins.excluded.state,
                    "confirmed_at": ins.excluded.confirmed_at,
                    "confirm_price": ins.excluded.confirm_price,
                    "confirm_vwap": ins.excluded.confirm_vwap,
                    "candle_close_at": ins.excluded.candle_close_at,
                    "data_as_of": ins.excluded.data_as_of,
                    "updated_at": now,
                },
                # Only a pending cross changes; a confirmed one is final.
                where=ev.confirmed_at.is_(None),
            ).returning(StockSignalEvent.id, literal_column("(xmax = 0)").label("inserted"))
            row = (await db.execute(stmt)).first()
            if row is not None:
                event_id = int(row.id)
                if row.inserted:
                    result.new_events.append(event_id)
                elif r.confirmed_at is not None:
                    result.confirmed_events.append(event_id)
            else:
                event_id = await db.scalar(
                    select(StockSignalEvent.id).where(
                        StockSignalEvent.instrument_id == iid,
                        StockSignalEvent.timeframe == tf.value,
                        StockSignalEvent.direction == r.direction,
                        StockSignalEvent.crossed_ts == r.crossed_ts,
                    )
                )

        prev = await db.scalar(
            select(StockSignalState.state).where(
                StockSignalState.instrument_id == iid, StockSignalState.timeframe == tf.value
            )
        )
        ins = insert(StockSignalState).values(
            instrument_id=iid,
            timeframe=tf.value,
            state=r.state.value,
            candle_ts=r.candle_ts,
            candle_close_at=r.candle_close_at,
            last_price=r.last,
            ema_fast=r.ema_fast,
            ema_slow=r.ema_slow,
            vwap=r.vwap,
            vwap_applicable=r.vwap_applicable,
            event_id=event_id,
            reason=(r.reason or None) and r.reason[:200],
            metrics=r.metrics(),
            evaluated_at=now,
            state_changed_at=now,
            run_id=run_id,
        )
        st = StockSignalState.__table__.c
        changed = st.state != ins.excluded.state
        await db.execute(
            ins.on_conflict_do_update(
                constraint="pk_stock_signal_states",
                set_={
                    "previous_state": case((changed, st.state), else_=st.previous_state),
                    "state_changed_at": case(
                        (changed, ins.excluded.evaluated_at), else_=st.state_changed_at
                    ),
                    "state": ins.excluded.state,
                    "candle_ts": ins.excluded.candle_ts,
                    "candle_close_at": ins.excluded.candle_close_at,
                    "last_price": ins.excluded.last_price,
                    "ema_fast": ins.excluded.ema_fast,
                    "ema_slow": ins.excluded.ema_slow,
                    "vwap": ins.excluded.vwap,
                    "vwap_applicable": ins.excluded.vwap_applicable,
                    "event_id": ins.excluded.event_id,
                    "reason": ins.excluded.reason,
                    "metrics": ins.excluded.metrics,
                    "evaluated_at": ins.excluded.evaluated_at,
                    "run_id": ins.excluded.run_id,
                },
                # A late retry of an older close never overwrites a newer reading.
                where=or_(
                    st.candle_ts.is_(None),
                    ins.excluded.candle_ts.is_(None),
                    ins.excluded.candle_ts >= st.candle_ts,
                ),
            )
        )
        if prev is not None and prev != r.state.value:
            result.changes.append((iid, tf.value, prev, r.state.value))


async def execute_run(
    maker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    settings: Settings,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunResult:
    """Work one claimed run. Never raises for data problems: they are counted per instrument."""
    async with maker() as db:
        run = await db.get(StockSignalRun, run_id)
        if run is None:
            return RunResult("missing")
        tf, boundary, attempts = Timeframe(run.timeframe), run.boundary, run.attempts
    result = RunResult("done")
    try:
        async with maker() as db:
            ids = await universe(db, settings)
        requested = await _request_history(maker, ids, tf, settings) if ids else 0
        deadline = clock() + timedelta(seconds=settings.STOCK_SIGNALS_FETCH_TIMEOUT_SECONDS)
        waited_out = False
        while ids and await _open_requests(maker, ids, tf):
            if clock() >= deadline:
                waited_out = True
                break
            await sleep(POLL_SECONDS)

        p = params(settings)
        hol = list(settings.DHAN_MARKET_HOLIDAYS)
        lookback = settings.STOCK_SIGNALS_LOOKBACK_BARS
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i : i + CHUNK]
            async with maker() as db:
                bars = await load_bars(db, chunk, tf, lookback, boundary)
                intraday = None
                if tf is Timeframe.D1:
                    # Each day's closing session VWAP, from stored 15m candles.
                    n = BARS_PER_DAY[Timeframe.M15] * (p.confirm_bars + 1)
                    intraday = await load_bars(db, chunk, Timeframe.M15, n, boundary)
                insts = {
                    x.id: x
                    for x in (
                        await db.scalars(select(Instrument).where(Instrument.id.in_(chunk)))
                    ).all()
                }
            readings = await asyncio.to_thread(
                _evaluate_chunk, bars, intraday, tf, boundary, p, hol
            )
            async with maker() as db, db.begin():
                await _store(db, run_id, insts, tf, readings, result)

        c = result.counts
        async with maker() as db, db.begin():
            await db.execute(
                update(StockSignalRun)
                .where(StockSignalRun.id == run_id)
                .values(
                    status="done",
                    universe_size=len(ids),
                    history_requested=requested,
                    evaluated=sum(c.values()),
                    unavailable=c.get("UNAVAILABLE", 0),
                    events_new=len(result.new_events),
                    confirmations=len(result.confirmed_events),
                    counts={**c, "history_wait_timed_out": waited_out},
                    error=None,
                    finished_at=func.now(),
                )
            )
        return result
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:1000]
        log.warning("stock_signals_run_failed", run_id=str(run_id), error=type(exc).__name__)
        retry = attempts < settings.STOCK_SIGNALS_MAX_ATTEMPTS
        async with maker() as db, db.begin():
            await db.execute(
                update(StockSignalRun)
                .where(StockSignalRun.id == run_id)
                .values(
                    status="queued" if retry else "failed",
                    error=err,
                    finished_at=None if retry else func.now(),
                )
            )
        return RunResult("queued" if retry else "failed")


async def purge(maker: async_sessionmaker[AsyncSession], settings: Settings) -> dict[str, int]:
    """Events past STOCK_SIGNALS_RETENTION_DAYS and finished runs older than 14 days."""
    now = datetime.now(UTC)
    async with maker() as db, db.begin():
        ev = await db.execute(
            delete(StockSignalEvent).where(
                StockSignalEvent.created_at
                < now - timedelta(days=settings.STOCK_SIGNALS_RETENTION_DAYS)
            )
        )
        runs = await db.execute(
            delete(StockSignalRun).where(
                and_(
                    StockSignalRun.status.in_(("done", "failed")),
                    StockSignalRun.created_at < now - RUN_RETENTION,
                )
            )
        )
    return {"events": ev.rowcount or 0, "runs": runs.rowcount or 0}  # type: ignore[attr-defined]
