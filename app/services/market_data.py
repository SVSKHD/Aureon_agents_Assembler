"""Market data plumbing for the terminal (docs/35-market-data.md).

- **Candle history queue** (trading.candle_backfill_requests): users and the
  `dhan-universe-backfill` scheduler job enqueue (instrument, timeframe, days); the research
  worker's `candle-backfill` lane works it one request at a time through the Dhan adapter, so
  every call goes through the shared Redis rate limiter (data APIs: 5/s, 100 000/day). A
  request only fetches what is missing: from the newest stored candle when the stored history
  already reaches back far enough, else the full window.
- **What Dhan can serve** is checked before anything is queued: intervals 1m 5m 15m 1h 1d
  (no 30m / 4h), segments NSE_EQ BSE_EQ NSE_FNO BSE_FNO IDX_I MCX_COMM, and only active
  instruments (the chart APIs do not serve expired derivative contracts).
- **Instrument master status** from dhan.instrument_sync_runs.
- **On-demand indicators** computed with the same code and the same candle window as the
  screener, so a chart study and a screener value agree to the digit.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.research import indicators as ind
from app.agents.trading.base import BrokerAdapter, NotSupported
from app.agents.trading.types import Timeframe
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.brokers import DhanInstrumentSyncRun
from app.models.trading import (
    CandleBackfillRequest,
    Instrument,
    MarketCandle,
    WatchlistItem,
)
from app.schemas.market import BackfillQueued, BackfillRejected, InstrumentStatus
from app.services import research
from app.services.research import tf_delta
from app.services.screener import load_bars

log = get_logger(__name__)

DHAN_HISTORY_TIMEFRAMES = frozenset(
    {Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.D1}
)
DHAN_HISTORY_SEGMENTS = frozenset({"NSE_EQ", "BSE_EQ", "NSE_FNO", "BSE_FNO", "IDX_I", "MCX_COMM"})
OPEN = ("queued", "running")


class MarketDataError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --- candle history queue -----------------------------------------------------------------


def max_days(tf: Timeframe, settings: Settings) -> int:
    if tf is Timeframe.D1:
        return settings.CANDLE_BACKFILL_MAX_DAYS_DAILY
    return settings.CANDLE_BACKFILL_MAX_DAYS_INTRADAY


def check_request(tf: Timeframe, days: int, settings: Settings) -> None:
    if tf not in DHAN_HISTORY_TIMEFRAMES:
        raise MarketDataError(
            422,
            "timeframe_unsupported",
            f"Dhan has no {tf.value} history (it serves 1m, 5m, 15m, 1h and 1d)",
        )
    cap = max_days(tf, settings)
    if days > cap:
        raise MarketDataError(
            422, "too_many_days", f"at most {cap} days of {tf.value} history per request"
        )


def _reject_reason(inst: Instrument | None) -> str | None:
    if inst is None:
        return "unknown_instrument"
    if inst.broker != "dhan":
        return "not_a_dhan_instrument"
    if not inst.is_active:
        return f"inactive ({inst.inactive_reason or 'inactive'}): Dhan serves no history for it"
    seg = inst.segment or inst.broker_instrument_id.split(":", 1)[0]
    if seg not in DHAN_HISTORY_SEGMENTS:
        return f"segment {seg} has no Dhan chart history"
    return None


async def enqueue_backfill(
    db: AsyncSession,
    instrument_ids: Sequence[int],
    tf: Timeframe,
    days: int,
    settings: Settings,
    *,
    source: Literal["user", "universe", "system"] = "user",
    requested_by: uuid.UUID | None = None,
) -> BackfillQueued:
    """Queue history requests (one per instrument). An instrument with an open request for the
    same timeframe is counted as `already_open`, never duplicated."""
    check_request(tf, days, settings)
    wanted = list(dict.fromkeys(instrument_ids))
    insts = {
        i.id: i
        for i in (await db.scalars(select(Instrument).where(Instrument.id.in_(wanted)))).all()
    }
    rejected: list[BackfillRejected] = []
    ok: list[int] = []
    for iid in wanted:
        reason = _reject_reason(insts.get(iid))
        if reason:
            rejected.append(BackfillRejected(instrument_id=iid, reason=reason))
        else:
            ok.append(iid)
    if requested_by is not None and ok:
        open_now = int(
            await db.scalar(
                select(func.count())
                .select_from(CandleBackfillRequest)
                .where(
                    CandleBackfillRequest.requested_by == requested_by,
                    CandleBackfillRequest.status.in_(OPEN),
                )
            )
            or 0
        )
        if open_now + len(ok) > settings.CANDLE_BACKFILL_USER_OPEN_MAX:
            raise MarketDataError(
                429,
                "too_many_open_requests",
                f"{open_now} history requests already open; at most "
                f"{settings.CANDLE_BACKFILL_USER_OPEN_MAX} (CANDLE_BACKFILL_USER_OPEN_MAX)",
            )
    queued = 0
    for i in range(0, len(ok), 1000):
        stmt = (
            insert(CandleBackfillRequest)
            .values(
                [
                    {
                        "instrument_id": iid,
                        "timeframe": tf.value,
                        "days": days,
                        "source": source,
                        "requested_by": requested_by,
                    }
                    for iid in ok[i : i + 1000]
                ]
            )
            .on_conflict_do_nothing(
                index_elements=["instrument_id", "timeframe"],
                index_where=CandleBackfillRequest.status.in_(OPEN),
            )
            .returning(CandleBackfillRequest.id)
        )
        queued += len((await db.execute(stmt)).all())
    return BackfillQueued(queued=queued, already_open=len(ok) - queued, rejected=rejected)


async def claim_backfill(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID | None:
    """Oldest queued request → running (SKIP LOCKED). A request left running for over an hour
    by a stopped worker goes back to the queue first."""
    async with maker() as db, db.begin():
        await db.execute(
            update(CandleBackfillRequest)
            .where(
                CandleBackfillRequest.status == "running",
                CandleBackfillRequest.started_at < func.now() - timedelta(hours=1),
            )
            .values(status="queued")
        )
        row = await db.scalar(
            select(CandleBackfillRequest)
            .where(CandleBackfillRequest.status == "queued")
            .order_by(CandleBackfillRequest.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        row.status, row.started_at = "running", datetime.now(UTC)
        row.attempts += 1
        return row.id


async def backfill_start(
    db: AsyncSession, instrument_id: int, tf: Timeframe, days: int, now: datetime
) -> datetime:
    """Only what is missing: from the newest stored real candle when stored history already
    reaches the window start, else the window start."""
    target = now - timedelta(days=days)
    first, last = (
        await db.execute(
            select(func.min(MarketCandle.ts), func.max(MarketCandle.ts)).where(
                MarketCandle.instrument_id == instrument_id,
                MarketCandle.timeframe == tf.value,
                MarketCandle.is_gap.is_(False),
            )
        )
    ).one()
    if first is not None and last is not None and first <= target + tf_delta(tf):
        newest: datetime = last
        return max(newest, target)
    return target


async def run_backfill(
    maker: async_sessionmaker[AsyncSession],
    adapter: BrokerAdapter | None,
    request_id: uuid.UUID,
    settings: Settings,
    now: datetime | None = None,
) -> str:
    """Work one claimed request. Returns the new status."""
    now = now or datetime.now(UTC)
    async with maker() as db:
        req = await db.get(CandleBackfillRequest, request_id)
        if req is None:
            return "missing"
        inst = await db.get(Instrument, req.instrument_id)
        tf = Timeframe(req.timeframe)
        start = await backfill_start(db, req.instrument_id, tf, req.days, now)
        attempts = req.attempts

    async def finish(status: str, **values: Any) -> str:
        async with maker() as db, db.begin():
            await db.execute(
                update(CandleBackfillRequest)
                .where(CandleBackfillRequest.id == request_id)
                .values(status=status, finished_at=func.now(), **values)
            )
        return status

    reason = _reject_reason(inst)
    if reason or inst is None:
        return await finish("failed", error=reason)
    if adapter is None:
        return await finish("failed", error="Dhan is not configured in the research worker")
    try:
        rows = await adapter.candles(inst.broker_instrument_id, tf, start, now)
    except NotSupported as exc:
        return await finish("failed", error=str(exc)[:1000])
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:1000]
        # Auth, rate-limit and network failures alike: retried up to the attempt cap (an
        # expired Dhan session fails the request visibly instead of retrying forever).
        if attempts < settings.CANDLE_BACKFILL_MAX_ATTEMPTS:
            async with maker() as db, db.begin():
                await db.execute(
                    update(CandleBackfillRequest)
                    .where(CandleBackfillRequest.id == request_id)
                    .values(status="queued", error=err)
                )
            return "queued"
        return await finish("failed", error=err)
    async with maker() as db, db.begin():
        n = await research.upsert_candles(db, inst.id, tf, rows, "broker")
        if rows:
            await research.mark_gaps(db, inst.id, tf, since=start)
    return await finish("done", candles=n, error=None)


# --- universes for the scheduler ----------------------------------------------------------


async def universe_ids(db: AsyncSession, name: str) -> list[int]:
    """DHAN_UNIVERSE_BACKFILL names → active instrument ids."""
    base = select(Instrument.id).where(Instrument.broker == "dhan", Instrument.is_active.is_(True))
    if name == "nse_equities":
        q = base.where(Instrument.segment == "NSE_EQ", Instrument.instrument_type == "EQUITY")
    elif name == "bse_equities":
        q = base.where(Instrument.segment == "BSE_EQ", Instrument.instrument_type == "EQUITY")
    elif name == "indices":
        q = base.where(Instrument.segment == "IDX_I")
    elif name == "fno_stocks":
        underlyings = (
            select(Instrument.underlying)
            .where(
                Instrument.broker == "dhan",
                Instrument.is_active.is_(True),
                Instrument.segment == "NSE_FNO",
                Instrument.instrument_type == "FUTSTK",
            )
            .distinct()
        )
        q = base.where(
            Instrument.segment == "NSE_EQ",
            Instrument.instrument_type == "EQUITY",
            Instrument.trading_symbol.in_(underlyings),
        )
    elif name == "watchlists":
        q = (
            select(Instrument.id)
            .join(WatchlistItem, WatchlistItem.instrument_id == Instrument.id)
            .where(Instrument.is_active.is_(True))
            .distinct()
        )
    else:
        raise ValueError(f"unknown universe {name!r}")
    return list((await db.scalars(q)).all())


async def enqueue_universes(
    maker: async_sessionmaker[AsyncSession], settings: Settings
) -> dict[str, Any]:
    """The `dhan-universe-backfill` job: queue every configured universe x timeframe (capped at
    SCREENER_MAX_UNIVERSE instruments each). The queue dedupes, and each request then fetches
    only the bars after the newest stored one."""
    out: dict[str, Any] = {}
    for name in settings.DHAN_UNIVERSE_BACKFILL:
        async with maker() as db, db.begin():
            ids = (await universe_ids(db, name))[: settings.SCREENER_MAX_UNIVERSE]
            for tf_s in settings.DHAN_UNIVERSE_BACKFILL_TIMEFRAMES:
                tf = Timeframe(tf_s)
                days = min(settings.DHAN_UNIVERSE_BACKFILL_DAYS, max_days(tf, settings))
                res = await enqueue_backfill(db, ids, tf, days, settings, source="universe")
                out[f"{name}:{tf.value}"] = {
                    "instruments": len(ids),
                    "queued": res.queued,
                    "already_open": res.already_open,
                    "rejected": len(res.rejected),
                }
    return out


# --- instrument master status -------------------------------------------------------------


async def instrument_status(db: AsyncSession, settings: Settings) -> InstrumentStatus:
    last_ok = await db.scalar(
        select(func.max(DhanInstrumentSyncRun.finished_at)).where(
            DhanInstrumentSyncRun.status == "succeeded"
        )
    )
    last = await db.scalar(
        select(DhanInstrumentSyncRun).order_by(DhanInstrumentSyncRun.started_at.desc()).limit(1)
    )
    rows = (
        await db.execute(
            select(Instrument.segment, func.count())
            .where(Instrument.broker == "dhan", Instrument.is_active.is_(True))
            .group_by(Instrument.segment)
        )
    ).all()
    by_segment = {str(seg or "UNKNOWN"): int(n) for seg, n in rows}
    max_age = settings.DHAN_INSTRUMENT_MAX_AGE_HOURS
    stale = last_ok is None or datetime.now(UTC) - last_ok > timedelta(hours=max_age)
    return InstrumentStatus(
        last_synced_at=last_ok,
        count=sum(by_segment.values()),
        by_segment=by_segment,
        last_error=last.error if last is not None and last.status == "failed" else None,
        last_run_status=last.status if last is not None else None,
        last_run_started_at=last.started_at if last is not None else None,
        stale=stale,
        max_age_hours=max_age,
    )


# --- candles for display + on-demand indicators -------------------------------------------


def is_closed(ts: datetime, tf: Timeframe | str, now: datetime) -> bool:
    return ts + tf_delta(tf) <= now


def vwap_tz_for(inst: Instrument) -> str:
    return "Asia/Kolkata" if inst.broker == "dhan" else "UTC"


async def compute_on_demand(
    db: AsyncSession,
    inst: Instrument,
    tf: Timeframe,
    specs: Sequence[ind.Spec],
    *,
    limit: int,
    candle: Literal["confirmed", "live"],
    settings: Settings,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """{name, ts, values} for the newest `limit` bars, computed over exactly the window the
    screener uses (the newest max(SCREENER_LOOKBACK_BARS, warm-up) real candles up to the same
    cutoff), so the last value equals the screener's to the digit."""
    now = now or datetime.now(UTC)
    lookback = max(settings.SCREENER_LOOKBACK_BARS, *(s.warmup for s in specs))
    cutoff = now - tf_delta(tf) if candle == "confirmed" else now
    bars = (await load_bars(db, [inst.id], tf, lookback, cutoff))[inst.id]
    if not bars:
        return []
    df = ind.frame(bars)
    tz = vwap_tz_for(inst)
    out: list[dict[str, Any]] = []
    tail = df.index[-limit:]
    for spec in specs:
        fr = ind.compute_spec(df, spec, tz).loc[tail]
        for ts, rec in zip(fr.index, fr.to_dict("records"), strict=True):
            values = {str(k): ind.to_dec(v) for k, v in rec.items()}
            if any(v is not None for v in values.values()):
                out.append({"name": spec.name, "ts": ts.to_pydatetime(), "values": values})
    return out


async def backfill_counts(db: AsyncSession) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(CandleBackfillRequest)
            .where(CandleBackfillRequest.status == "queued")
        )
        or 0
    )


async def candles_as_of(db: AsyncSession, tfs: Sequence[Timeframe]) -> dict[str, datetime | None]:
    """Newest real Dhan candle per timeframe (uses ix_trading_market_candles_timeframe_ts)."""
    out: dict[str, datetime | None] = {}
    for tf in tfs:
        out[tf.value] = await db.scalar(
            text(
                "SELECT max(ts) FROM trading.market_candles "
                "WHERE timeframe = :tf AND is_gap = false"
            ),
            {"tf": tf.value},
        )
    return out
