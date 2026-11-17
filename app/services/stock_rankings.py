"""Market rankings from the latest finished Stock Signals run (docs/37 §3).

Every list reads the states the newest `done` run stored for a timeframe: no Dhan call per
request, the same numbers for everyone, and an explicit data time (the run's candle close).
Excluded from every list: UNAVAILABLE states (no data, insufficient or stale history) and
illiquid instruments (below STOCK_SIGNALS_MIN_DAILY_TURNOVER). `coverage()` says how many
were left out and why.

- gainers / losers: by the real % change from the previous session's last close
- trending bullish / bearish: by the 0-100 trend score, among instruments whose EMAs agree
  with the direction (a ranking, not a probability of profit)
- unusual volume: relative volume at or above STOCK_SIGNALS_UNUSUAL_RVOL
- recent crossovers: stock_signal_events, newest first
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import Numeric, Select, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock_signals import StockSignalEvent, StockSignalRun, StockSignalState
from app.models.trading import Instrument

Mover = Literal["gainers", "losers"]
Direction = Literal["bullish", "bearish"]
BULLISH = ("BULLISH_CONFIRMED", "BULLISH_PENDING", "BULLISH_TREND")
BEARISH = ("BEARISH_CONFIRMED", "BEARISH_PENDING", "BEARISH_TREND")


def _num(key: str) -> Any:
    return StockSignalState.metrics[key].astext.cast(Numeric)


async def latest_run(db: AsyncSession, tf: str) -> StockSignalRun | None:
    run: StockSignalRun | None = await db.scalar(
        select(StockSignalRun)
        .where(StockSignalRun.timeframe == tf, StockSignalRun.status == "done")
        .order_by(StockSignalRun.boundary.desc())
        .limit(1)
    )
    return run


async def coverage(db: AsyncSession, tf: str) -> dict[str, Any]:
    """What the rankings are built from: the run, its data time, and what was left out."""
    run = await latest_run(db, tf)
    if run is None:
        return {"timeframe": tf, "run_id": None, "data_as_of": None, "universe_size": 0}
    data_as_of = await db.scalar(
        select(func.max(StockSignalState.candle_close_at)).where(StockSignalState.run_id == run.id)
    )
    counts = run.counts or {}
    return {
        "timeframe": tf,
        "run_id": run.id,
        "boundary": run.boundary,
        "data_as_of": data_as_of,
        "universe_size": run.universe_size,
        "evaluated": run.evaluated,
        "unavailable": run.unavailable,
        "illiquid": int(counts.get("illiquid", 0)),
        "history_wait_timed_out": bool(counts.get("history_wait_timed_out", False)),
        "finished_at": run.finished_at,
    }


def _ranked(run_id: Any, tf: str) -> Select[Any]:
    return (
        select(StockSignalState, Instrument)
        .join(Instrument, Instrument.id == StockSignalState.instrument_id)
        .where(
            StockSignalState.run_id == run_id,
            StockSignalState.timeframe == tf,
            StockSignalState.state != "UNAVAILABLE",
            StockSignalState.metrics["liquid"].as_boolean().is_(True),
        )
    )


def row(st: StockSignalState, inst: Instrument) -> dict[str, Any]:
    m = st.metrics or {}

    def d(key: str) -> Decimal | None:
        v = m.get(key)
        return Decimal(v) if v is not None else None

    return {
        "instrument_id": inst.id,
        "symbol": inst.trading_symbol or inst.symbol,
        "name": inst.display_name or inst.name,
        "exchange": inst.exchange,
        "segment": inst.segment,
        "timeframe": st.timeframe,
        "state": st.state,
        "last_price": st.last_price,
        "change_pct": d("change_pct"),
        "trend_score": st.trend_score,
        "rvol": d("rvol"),
        "rsi": d("rsi"),
        "adx": d("adx"),
        "ema_fast": st.ema_fast,
        "ema_slow": st.ema_slow,
        "vwap": st.vwap,
        "vwap_distance_pct": d("vwap_distance_pct"),
        "ema_spread_pct": d("ema_spread_pct"),
        "relative_pct": d("relative_pct"),
        "missing_features": list(m.get("missing") or []),
        "in_holdings": st.in_holdings,
        "event_id": st.event_id,
        "candle_close_at": st.candle_close_at,
    }


def _exchange(q: Select[Any], exchange: str | None) -> Select[Any]:
    return q.where(Instrument.exchange == exchange.upper()) if exchange else q


async def movers(
    db: AsyncSession, tf: str, kind: Mover, *, limit: int = 10, exchange: str | None = None
) -> list[dict[str, Any]]:
    run = await latest_run(db, tf)
    if run is None:
        return []
    chg = _num("change_pct")
    q = _exchange(_ranked(run.id, tf), exchange).where(chg.is_not(None))
    q = q.where(chg > 0) if kind == "gainers" else q.where(chg < 0)
    q = q.order_by(chg.desc() if kind == "gainers" else chg.asc(), Instrument.id).limit(limit)
    return [row(st, inst) for st, inst in (await db.execute(q)).all()]


async def trending(
    db: AsyncSession,
    tf: str,
    direction: Direction,
    *,
    limit: int = 10,
    exchange: str | None = None,
) -> list[dict[str, Any]]:
    run = await latest_run(db, tf)
    if run is None:
        return []
    s = StockSignalState
    q = _exchange(_ranked(run.id, tf), exchange).where(s.trend_score.is_not(None))
    if direction == "bullish":
        q = q.where(s.state.in_(BULLISH), s.ema_fast > s.ema_slow, s.trend_score > 50)
        q = q.order_by(s.trend_score.desc(), Instrument.id)
    else:
        q = q.where(s.state.in_(BEARISH), s.ema_fast < s.ema_slow, s.trend_score < 50)
        q = q.order_by(s.trend_score.asc(), Instrument.id)
    return [row(st, inst) for st, inst in (await db.execute(q.limit(limit))).all()]


async def unusual_volume(
    db: AsyncSession,
    tf: str,
    min_rvol: float,
    *,
    limit: int = 10,
    exchange: str | None = None,
) -> list[dict[str, Any]]:
    run = await latest_run(db, tf)
    if run is None:
        return []
    rvol = _num("rvol")
    q = (
        _exchange(_ranked(run.id, tf), exchange)
        .where(rvol >= min_rvol)
        .order_by(rvol.desc(), Instrument.id)
        .limit(limit)
    )
    return [row(st, inst) for st, inst in (await db.execute(q)).all()]


def event_row(ev: StockSignalEvent, inst: Instrument) -> dict[str, Any]:
    return {
        "id": ev.id,
        "instrument_id": inst.id,
        "symbol": inst.trading_symbol or inst.symbol,
        "name": inst.display_name or inst.name,
        "exchange": ev.exchange,
        "timeframe": ev.timeframe,
        "direction": ev.direction,
        "state": ev.state,
        "crossed_at": ev.crossed_at,
        "confirmed_at": ev.confirmed_at,
        "ema_fast": ev.ema_fast,
        "ema_slow": ev.ema_slow,
        "vwap": ev.vwap,
        "last_price": ev.last_price,
        "confirm_price": ev.confirm_price,
        "confirm_vwap": ev.confirm_vwap,
        "trend_score": ev.trend_score,
        "in_holdings": ev.in_holdings,
        "data_source": ev.data_source,
        "data_as_of": ev.data_as_of,
        "metrics": ev.metrics or {},
        "created_at": ev.created_at,
        "updated_at": ev.updated_at,
    }


async def recent_crossovers(
    db: AsyncSession,
    tf: str,
    since: datetime,
    *,
    direction: Direction | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    q = (
        select(StockSignalEvent, Instrument)
        .join(Instrument, Instrument.id == StockSignalEvent.instrument_id)
        .where(and_(StockSignalEvent.timeframe == tf, StockSignalEvent.crossed_at >= since))
    )
    if direction:
        q = q.where(StockSignalEvent.direction == direction)
    q = q.order_by(StockSignalEvent.crossed_at.desc(), StockSignalEvent.id.desc()).limit(limit)
    return [event_row(ev, inst) for ev, inst in (await db.execute(q)).all()]
