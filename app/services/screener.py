"""Screener runs against stored candles (docs/35-market-data.md "Screener").

- **No broker calls.** A run reads trading.market_candles only. It never asks Dhan for a
  candle, so a 6000-instrument scan costs zero Dhan requests; history comes from the
  candle-backfill queue (app/services/market_data.py) under the shared Dhan rate limit.
- **Bounded work.** The universe is capped (SCREENER_MAX_UNIVERSE), candles are read
  SCREENER_CHUNK instruments at a time with one indexed LATERAL query per chunk (the newest
  `lookback` bars per instrument via pk_market_candles), CPU work runs in a thread, and a run
  stops at SCREENER_RUN_TIMEOUT_SECONDS. Small universes (<= SCREENER_INLINE_MAX) run inside
  the request; larger ones are queued for the research worker's `screener` lane.
- **Persisted.** trading.screener_runs holds the request, progress and counts; matches are
  stored in trading.screener_results, so pages and sorts never recompute. A confirmed-candle
  request repeated within SCREENER_CACHE_SECONDS *and the same bar period* is served from the
  stored run.
- **Truthful coverage.** Instruments without candles, with too few bars, or whose newest bar
  lags the universe's newest bar by more than SCREENER_STALE_BARS periods are counted
  (no_data / insufficient_data / stale) and never reported as matches or non-matches.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Numeric, and_, delete, func, insert, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.research import screener as engine
from app.agents.trading.dhan.parse import IST
from app.agents.trading.types import Timeframe
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.trading import (
    Instrument,
    ScreenerResult,
    ScreenerRun,
    Watchlist,
    WatchlistItem,
)
from app.schemas.market import (
    ScreenerCoverage,
    ScreenerItem,
    ScreenerRequest,
    ScreenerResponse,
    ScreenerSort,
)
from app.schemas.trading import InstrumentOut
from app.services.research import TF_SECONDS, tf_delta

log = get_logger(__name__)

# Dhan serves 1m 5m 15m 1h (intraday) and 1d; nothing ever stores 30m / 4h NSE/BSE candles.
SUPPORTED_TIMEFRAMES = frozenset(
    {Timeframe.M1, Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.D1}
)
EQUITY_TYPES = ("EQUITY",)
FUTURE_TYPES = ("FUTIDX", "FUTSTK")
ACTIVE = ("queued", "running")


class ScreenerError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# --- universe -----------------------------------------------------------------------------


async def resolve_universe(
    db: AsyncSession, req: ScreenerRequest, owner_id: uuid.UUID, settings: Settings
) -> list[int]:
    """Active instrument ids of the universe, ascending. Raises ScreenerError on a foreign or
    unknown watchlist (404) or an oversized universe (422)."""
    u = req.universe
    cap = settings.SCREENER_MAX_UNIVERSE
    if u.watchlist_id is not None:
        wl = await db.get(Watchlist, uuid.UUID(u.watchlist_id))
        if wl is None or wl.owner_user_id != owner_id:
            raise ScreenerError(404, "watchlist_not_found", "watchlist not found")
        stmt = (
            select(Instrument.id)
            .join(WatchlistItem, WatchlistItem.instrument_id == Instrument.id)
            .where(WatchlistItem.watchlist_id == wl.id, Instrument.is_active.is_(True))
        )
    else:
        today = datetime.now(UTC).astimezone(IST).date()
        conds = []
        if "equity" in u.kind:
            conds.append(
                and_(
                    Instrument.segment.in_([f"{e}_EQ" for e in u.exchange]),
                    Instrument.instrument_type.in_(EQUITY_TYPES),
                )
            )
        if "index" in u.kind:
            conds.append(and_(Instrument.segment == "IDX_I", Instrument.exchange.in_(u.exchange)))
        if "future" in u.kind:
            conds.append(
                and_(
                    Instrument.segment.in_([f"{e}_FNO" for e in u.exchange]),
                    Instrument.instrument_type.in_(FUTURE_TYPES),
                    or_(Instrument.expiry.is_(None), Instrument.expiry >= today),
                )
            )
        stmt = select(Instrument.id).where(
            Instrument.broker == "dhan", Instrument.is_active.is_(True), or_(*conds)
        )
    ids = list((await db.scalars(stmt.order_by(Instrument.id).limit(cap + 1))).all())
    if len(ids) > cap:
        raise ScreenerError(
            422,
            "universe_too_large",
            f"the universe has more than {cap} instruments (SCREENER_MAX_UNIVERSE); "
            "narrow it to one exchange or kind, or screen a watchlist",
        )
    return ids


# --- candles ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime
    open: Any
    high: Any
    low: Any
    close: Any
    volume: Any


_BARS_SQL = text(
    """
    SELECT i.id AS instrument_id, c.ts, c.open, c.high, c.low, c.close, c.volume
    FROM unnest(CAST(:ids AS bigint[])) AS i(id)
    CROSS JOIN LATERAL (
        SELECT ts, open, high, low, close, volume
        FROM trading.market_candles m
        WHERE m.instrument_id = i.id AND m.timeframe = :tf AND m.is_gap = false
          AND m.ts <= :cutoff
        ORDER BY m.ts DESC
        LIMIT :n
    ) c
    """
)
_LATEST_SQL = text(
    """
    SELECT max(c.ts) FROM unnest(CAST(:ids AS bigint[])) AS i(id)
    CROSS JOIN LATERAL (
        SELECT ts FROM trading.market_candles m
        WHERE m.instrument_id = i.id AND m.timeframe = :tf AND m.is_gap = false
          AND m.ts <= :cutoff
        ORDER BY m.ts DESC LIMIT 1
    ) c
    """
)


async def load_bars(
    db: AsyncSession, ids: Sequence[int], tf: Timeframe, n: int, cutoff: datetime
) -> dict[int, list[Bar]]:
    """The newest `n` real (non-gap) candles with ts <= cutoff per instrument, ascending."""
    out: dict[int, list[Bar]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = await db.execute(_BARS_SQL, {"ids": list(ids), "tf": tf.value, "cutoff": cutoff, "n": n})
    for r in rows:
        out[int(r.instrument_id)].append(Bar(r.ts, r.open, r.high, r.low, r.close, r.volume))
    for bars in out.values():
        bars.sort(key=lambda b: b.ts)
    return out


async def universe_latest(
    db: AsyncSession, ids: Sequence[int], tf: Timeframe, cutoff: datetime
) -> datetime | None:
    if not ids:
        return None
    latest: datetime | None = None
    for i in range(0, len(ids), 2000):
        got = await db.scalar(
            _LATEST_SQL, {"ids": list(ids[i : i + 2000]), "tf": tf.value, "cutoff": cutoff}
        )
        if got is not None and (latest is None or got > latest):
            latest = got
    return latest


def cutoff_for(req: ScreenerRequest, now: datetime) -> datetime:
    """Confirmed: only candles whose period ended by `now`. Live: anything up to `now`."""
    return now - tf_delta(req.timeframe) if req.candle == "confirmed" else now


# --- requests and runs --------------------------------------------------------------------


def request_hash(req: ScreenerRequest, now: datetime) -> str:
    """Identity of the *scan*: universe + timeframe + candle mode + filters, plus the bar
    period for confirmed scans (a newly closed bar is a new scan). Sort and paging are not part
    of it: they are applied to the stored results."""
    body = req.model_dump(mode="json", include={"universe", "timeframe", "candle", "filters"})
    if req.candle == "confirmed":
        body["bar"] = int(now.timestamp()) // TF_SECONDS[req.timeframe]
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def check_supported(req: ScreenerRequest) -> None:
    if req.timeframe not in SUPPORTED_TIMEFRAMES:
        raise ScreenerError(
            422,
            "timeframe_unsupported",
            f"Dhan has no {req.timeframe.value} candles (it serves 1m, 5m, 15m, 1h and 1d), so "
            "NSE/BSE instruments cannot be screened on it",
        )


async def find_reusable(
    db: AsyncSession, owner_id: uuid.UUID, h: str, settings: Settings, now: datetime
) -> ScreenerRun | None:
    """An active run of the same scan, or (confirmed only, hash includes the bar) a done run
    younger than SCREENER_CACHE_SECONDS."""
    active = await db.scalar(
        select(ScreenerRun)
        .where(
            ScreenerRun.owner_user_id == owner_id,
            ScreenerRun.request_hash == h,
            ScreenerRun.status.in_(ACTIVE),
        )
        .order_by(ScreenerRun.created_at.desc())
        .limit(1)
    )
    if active is not None or settings.SCREENER_CACHE_SECONDS <= 0:
        return active
    cached: ScreenerRun | None = await db.scalar(
        select(ScreenerRun)
        .where(
            ScreenerRun.owner_user_id == owner_id,
            ScreenerRun.request_hash == h,
            ScreenerRun.status == "done",
            ScreenerRun.created_at >= now - timedelta(seconds=settings.SCREENER_CACHE_SECONDS),
            ScreenerRun.request["candle"].astext == "confirmed",
        )
        .order_by(ScreenerRun.created_at.desc())
        .limit(1)
    )
    return cached


async def active_runs(db: AsyncSession, owner_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(ScreenerRun)
            .where(ScreenerRun.owner_user_id == owner_id, ScreenerRun.status.in_(ACTIVE))
        )
        or 0
    )


def _scan_request(req: ScreenerRequest) -> dict[str, Any]:
    return req.model_dump(mode="json")


async def create_run(
    db: AsyncSession,
    req: ScreenerRequest,
    owner_id: uuid.UUID,
    h: str,
    universe_size: int,
    *,
    status: str,
) -> ScreenerRun:
    run = ScreenerRun(
        owner_user_id=owner_id,
        timeframe=req.timeframe.value,
        request=_scan_request(req),
        request_hash=h,
        status=status,
        universe_size=universe_size,
        started_at=func.now() if status == "running" else None,
    )
    db.add(run)
    await db.flush()
    return run


def _evaluate_chunk(
    bars: dict[int, list[Bar]],
    req: ScreenerRequest,
    now: datetime,
    stale_before: datetime | None,
) -> list[tuple[int, str, engine.Evaluation | None]]:
    step = tf_delta(req.timeframe)
    out: list[tuple[int, str, engine.Evaluation | None]] = []
    for iid, rows in bars.items():
        if not rows:
            out.append((iid, "no_data", None))
            continue
        if stale_before is not None and rows[-1].ts < stale_before:
            out.append((iid, "stale", None))
            continue
        ev = engine.evaluate(
            rows,
            req.filters,
            step=step,
            mode=req.candle,
            now=now,
            day_tz=IST,
            vwap_tz="Asia/Kolkata",
        )
        out.append((iid, ev.outcome, ev))
    return out


async def execute_run(
    maker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> str:
    """Evaluate a run that is already `running`. Returns the final status. Never raises for a
    data problem: failures are recorded on the run."""
    started = time.monotonic()
    now = now or datetime.now(UTC)
    async with maker() as db:
        run = await db.get(ScreenerRun, run_id)
        if run is None:
            return "missing"
        req = ScreenerRequest.model_validate(run.request)
        owner = run.owner_user_id
    try:
        async with maker() as db:
            ids = await resolve_universe(db, req, owner, settings)
            cutoff = cutoff_for(req, now)
            ref = await universe_latest(db, ids, req.timeframe, cutoff)
        step = tf_delta(req.timeframe)
        stale_before = ref - step * settings.SCREENER_STALE_BARS if ref is not None else None
        lookback = max(settings.SCREENER_LOOKBACK_BARS, engine.lookback_needed(req.filters))
        counts = ScreenerCoverage()
        async with maker() as db, db.begin():
            await db.execute(
                update(ScreenerRun)
                .where(ScreenerRun.id == run_id)
                .values(universe_size=len(ids), data_as_of=ref)
            )
        for i in range(0, len(ids), settings.SCREENER_CHUNK):
            if time.monotonic() - started > settings.SCREENER_RUN_TIMEOUT_SECONDS:
                raise TimeoutError(
                    f"run exceeded SCREENER_RUN_TIMEOUT_SECONDS "
                    f"({settings.SCREENER_RUN_TIMEOUT_SECONDS}s) after {counts.scanned} "
                    f"of {len(ids)} instruments"
                )
            chunk = ids[i : i + settings.SCREENER_CHUNK]
            async with maker() as db:
                if await db.scalar(
                    select(ScreenerRun.cancel_requested).where(ScreenerRun.id == run_id)
                ):
                    await _finish(maker, run_id, "cancelled", counts)
                    return "cancelled"
                bars = await load_bars(db, chunk, req.timeframe, lookback, cutoff)
            evals = await asyncio.to_thread(_evaluate_chunk, bars, req, now, stale_before)
            matches = []
            for iid, outcome, ev in evals:
                counts.scanned += 1
                if outcome == "match" and ev is not None:
                    counts.matched += 1
                    matches.append(
                        {
                            "run_id": run_id,
                            "instrument_id": iid,
                            "outcome": "match",
                            "candle_ts": ev.candle_ts,
                            "metrics": ev.metrics(),
                            "detail": ev.detail,
                        }
                    )
                elif outcome in ("no_data", "insufficient_data", "stale"):
                    setattr(counts, outcome, getattr(counts, outcome) + 1)
            async with maker() as db, db.begin():
                if matches:
                    await db.execute(insert(ScreenerResult), matches)
                await db.execute(
                    update(ScreenerRun)
                    .where(ScreenerRun.id == run_id)
                    .values(scanned=counts.scanned, matched=counts.matched)
                )
        await _finish(maker, run_id, "done", counts)
        return "done"
    except ScreenerError as exc:
        await _finish(maker, run_id, "failed", ScreenerCoverage(), error=exc.message)
        return "failed"
    except Exception as exc:  # recorded on the run; never crashes the caller
        log.warning("screener_run_failed", run_id=str(run_id), error=type(exc).__name__)
        await _finish(
            maker, run_id, "failed", ScreenerCoverage(), error=f"{type(exc).__name__}: {exc}"
        )
        return "failed"


async def _finish(
    maker: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    status: str,
    counts: ScreenerCoverage,
    *,
    error: str | None = None,
) -> None:
    values: dict[str, Any] = {"status": status, "finished_at": func.now()}
    if status != "failed":
        values["skipped"] = {
            "no_data": counts.no_data,
            "insufficient_data": counts.insufficient_data,
            "stale": counts.stale,
        }
        values["scanned"], values["matched"] = counts.scanned, counts.matched
    if error is not None:
        values["error"] = error[:1000]
    async with maker() as db, db.begin():
        await db.execute(update(ScreenerRun).where(ScreenerRun.id == run_id).values(**values))


async def claim_run(
    maker: async_sessionmaker[AsyncSession], settings: Settings
) -> uuid.UUID | None:
    """Oldest queued run → running (SKIP LOCKED, safe with several workers). A run left
    `running` by a crashed worker for over 2x the run timeout is failed first."""
    async with maker() as db, db.begin():
        await db.execute(
            update(ScreenerRun)
            .where(
                ScreenerRun.status == "running",
                ScreenerRun.started_at
                < func.now() - timedelta(seconds=2 * settings.SCREENER_RUN_TIMEOUT_SECONDS),
            )
            .values(status="failed", error="abandoned (worker stopped)", finished_at=func.now())
        )
        run = await db.scalar(
            select(ScreenerRun)
            .where(ScreenerRun.status == "queued")
            .order_by(ScreenerRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if run is None:
            return None
        if run.cancel_requested:
            run.status, run.finished_at = "cancelled", datetime.now(UTC)
            return None
        run.status, run.started_at = "running", datetime.now(UTC)
        return run.id


async def purge_old(maker: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    async with maker() as db, db.begin():
        res = await db.execute(
            delete(ScreenerRun).where(
                ScreenerRun.created_at
                < func.now() - timedelta(days=settings.SCREENER_RETENTION_DAYS),
                ScreenerRun.status.notin_(ACTIVE),
            )
        )
        return int(res.rowcount or 0)  # type: ignore[attr-defined]


# --- reading results ----------------------------------------------------------------------


def _sort_clause(sort: ScreenerSort) -> list[Any]:
    m = ScreenerResult.metrics
    expr: Any
    if sort.field == "change_pct":
        expr = m["change_pct"].astext.cast(Numeric)
    elif sort.field == "last":
        expr = m["last"].astext.cast(Numeric)
    elif sort.field == "crossed_at":
        expr = m["crossed_at"].astext
    else:
        expr = func.coalesce(Instrument.trading_symbol, Instrument.symbol)
    ordered = expr.desc() if sort.dir == "desc" else expr.asc()
    return [ordered.nulls_last(), Instrument.id]


async def response_for(
    db: AsyncSession,
    run: ScreenerRun,
    sort: ScreenerSort,
    limit: int,
    offset: int,
    *,
    cached: bool = False,
) -> ScreenerResponse:
    req = ScreenerRequest.model_validate(run.request)
    items: list[ScreenerItem] = []
    if run.status == "done":
        rows = (
            await db.execute(
                select(ScreenerResult, Instrument)
                .join(Instrument, Instrument.id == ScreenerResult.instrument_id)
                .where(ScreenerResult.run_id == run.id, ScreenerResult.outcome == "match")
                .order_by(*_sort_clause(sort))
                .limit(limit)
                .offset(offset)
            )
        ).all()
        for res, inst in rows:
            m = res.metrics or {}
            items.append(
                ScreenerItem(
                    instrument=InstrumentOut.model_validate(inst),
                    last=m.get("last"),
                    change_pct=m.get("change_pct"),
                    indicators=m.get("indicators") or {},
                    signal=m.get("signal"),
                    crossed_at=m.get("crossed_at"),
                    data_as_of=res.candle_ts,
                    candle_closed=bool(m.get("candle_closed", True)),
                )
            )
    skipped = run.skipped or {}
    detail = None
    if run.status in ACTIVE:
        detail = f"scan {run.status}: poll GET /api/trading/screener/runs/{run.id}"
    elif run.status == "failed":
        detail = run.error
    elif run.universe_size and run.data_as_of is None:
        detail = (
            "no stored candles for this universe and timeframe: queue history with "
            "POST /api/trading/candles/backfill or enable DHAN_UNIVERSE_BACKFILL"
        )
    return ScreenerResponse(
        as_of=run.finished_at or run.started_at or run.created_at,
        candle=req.candle,
        timeframe=req.timeframe,
        universe_size=run.universe_size,
        total=run.matched if run.status == "done" else 0,
        items=items,
        run_id=run.id,
        status=run.status,
        coverage=ScreenerCoverage(
            scanned=run.scanned,
            matched=run.matched,
            no_data=int(skipped.get("no_data", 0)),
            insufficient_data=int(skipped.get("insufficient_data", 0)),
            stale=int(skipped.get("stale", 0)),
        ),
        universe_data_as_of=run.data_as_of,
        cached=cached,
        detail=detail,
    )
