"""Live market data shared by the market-live worker, the API and the browser hub (docs/38).

Redis layout (all written by the market-live worker unless noted):

| Key / channel | What |
| --- | --- |
| `market:quote:<SEG:ID>` | JSON quote view (LTP, previous close and its source, change, status) |
| `market:bar:<tf>:<SEG:ID>` | JSON forming live bar |
| `market:bars:<tf>:<SEG:ID>` | list of today's closed live bars (JSON, oldest first, capped) |
| `market:movers:<universe>` | JSON rows for the live rankings of one universe |
| `market:screener:<tf>` | JSON live EMA/VWAP states for the live universes |
| `market:live:status` | the worker's status record |
| `market:quotes`, `market:candles`, `market:rankings`, `market:screener` | pub/sub channels |
| `dhan:feed:demand` | (API) sorted set of watched instruments → expiry, read by the feed |
| `dhan:feed:universe` | set of instruments the market-live universes need, read by the feed |

Tick-built bars stay in Redis: they are never written to trading.market_candles, so confirmed
analysis (screener, Stock Signals, backtests) only ever reads broker candles.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.research import live_market as lm
from app.agents.trading.types import Timeframe
from app.core.config import Settings
from app.models.trading import Instrument, MarketCandle
from app.services import dhan_feed_health as fh
from app.services import market_data
from app.services.research import tf_delta

QUOTE_KEY = "market:quote:{ref}"
BAR_KEY = "market:bar:{tf}:{ref}"
BARS_KEY = "market:bars:{tf}:{ref}"
MOVERS_KEY = "market:movers:{universe}"
SCREENER_KEY = "market:screener:{tf}"
STATUS_KEY = "market:live:status"
LEADER_KEY = "market:live:leader"
CH_QUOTES = "market:quotes"
CH_CANDLES = "market:candles"
CH_RANKINGS = "market:rankings"
CH_SCREENER = "market:screener"
CHANNELS = (CH_QUOTES, CH_CANDLES, CH_RANKINGS, CH_SCREENER)
TTL = 86_400
BARS_KEEP = 500
STATUS_TTL = 60
LEADER_TTL = 30
WARMUP_BARS = 200


def _s(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def market(now: datetime, holidays: Iterable[date] = ()) -> dict[str, Any]:
    st = fh.market_state(now, list(holidays))
    return {"open": bool(st["open"]), "session": st["session"], "now": now.isoformat()}


# --- reading Redis -----------------------------------------------------------------------------


async def read_json(redis: Any, key: str) -> Any:
    raw = await redis.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def quote_from_hash(ref: str, h: Mapping[Any, Any]) -> tuple[lm.LiveQuote, bool]:
    """A LiveQuote from the feed worker's `dhan:tick:<ref>` hash (fallback when the market-live
    worker has not written a quote view), and the feed's own stale flag."""
    d = {_s(k): _s(v) for k, v in h.items()}
    q = lm.LiveQuote(ref)
    received = lm.ts_of(d.get("received_at")) or datetime.now(UTC)
    q.merge({**d, "kind": "quote"}, received)
    pc_at = lm.ts_of(d.get("prev_close_received_at"))
    if d.get("prev_close") and pc_at is not None:
        q.merge({"kind": "prev_close", "prev_close": d["prev_close"]}, pc_at)
    return q, d.get("stale") == "1"


async def read_quotes(
    redis: Any,
    refs: Sequence[str],
    *,
    now: datetime,
    settings: Settings,
) -> dict[str, dict[str, Any]]:
    """The quote view per ref: the market-live worker's (with the stored previous close) when
    present, else built from the feed hash (feed previous close only, and said so)."""
    out: dict[str, dict[str, Any]] = {}
    if not refs:
        return out
    raws = await redis.mget([QUOTE_KEY.format(ref=r) for r in refs])
    m = market(now, settings.DHAN_MARKET_HOLIDAYS)
    for ref, raw in zip(refs, raws, strict=True):
        view: dict[str, Any] | None = None
        if raw is not None:
            try:
                view = json.loads(raw)
            except ValueError:
                view = None
        if view is not None:
            # Freshness is re-judged now: a view written a while ago may have gone stale.
            q = lm.LiveQuote(ref)
            q.ltp = lm.dec(view.get("ltp"))
            q.received_at = lm.ts_of(view.get("received_at"))
            view["status"] = lm.quote_status(
                q, now, market_open=m["open"], stale_after=settings.DHAN_FEED_STALE_SECONDS
            )
            if q.received_at is not None:
                view["age_seconds"] = round((now - q.received_at).total_seconds(), 3)
            view["computed_by"] = "market_live"
            out[ref] = view
            continue
        h = await redis.hgetall(f"dhan:tick:{ref}")
        if not h:
            continue
        q, flag = quote_from_hash(ref, h)
        view = lm.quote_view(
            q,
            now,
            daily_closes={},
            market_open=m["open"],
            stale_after=settings.DHAN_FEED_STALE_SECONDS,
            holidays=settings.DHAN_MARKET_HOLIDAYS,
            stale_flag=flag,
        )
        view["computed_by"] = "feed_hash"
        out[ref] = view
    return out


async def read_bars(
    redis: Any, ref: str, tf: Timeframe, *, limit: int = BARS_KEEP
) -> tuple[list[lm.LiveBar], lm.LiveBar | None]:
    """Today's closed live bars (oldest first) and the forming one."""
    raw = await redis.lrange(BARS_KEY.format(tf=tf.value, ref=ref), -limit, -1)
    closed = []
    for r in raw:
        try:
            b = lm.LiveBar.from_json(json.loads(r))
        except ValueError:
            b = None
        if b is not None:
            closed.append(b)
    f = await read_json(redis, BAR_KEY.format(tf=tf.value, ref=ref))
    forming = lm.LiveBar.from_json(f) if isinstance(f, dict) else None
    if forming is not None and closed and forming.ts <= closed[-1].ts:
        forming = None  # an old forming record left behind by a restart
    return closed, forming


# --- movers ------------------------------------------------------------------------------------

MOVER_KINDS: tuple[lm.MoverKind, ...] = ("gainers", "losers", "unusual_volume")


def scope(snap: Mapping[str, Any], cov: lm.MoverCoverage) -> dict[str, Any]:
    """What the ranking covers. `market_wide` only when the universe is every NSE equity, it was
    not capped, and every one of them had a quote: otherwise the list is a subset and is never
    labelled market-wide."""
    name = snap.get("universe")
    wide = (
        name == "nse_equities"
        and not snap.get("capped")
        and cov.universe_size > 0
        and cov.with_quote == cov.universe_size
    )
    return {
        "universe": name,
        "market_wide": bool(wide),
        "capped": bool(snap.get("capped")),
        "universe_total": snap.get("universe_total", snap.get("universe_size")),
        "label": (
            "all NSE equities"
            if wide
            else f"{snap.get('universe')} universe ({cov.with_quote} of "
            f"{cov.universe_size} instruments with a live quote)"
        ),
    }


def movers_summary(
    snap: Mapping[str, Any],
    *,
    kinds: Sequence[lm.MoverKind] = MOVER_KINDS,
    limit: int,
    min_price: float,
    min_avg_turnover: float,
    min_rvol: float,
    include_stale: bool = False,
    exchange: str | None = None,
) -> dict[str, Any]:
    """Rank a `market:movers:<universe>` snapshot with the caller's filters. The same function
    serves the worker's published event, the REST endpoint and the WebSocket snapshot."""
    rows = list(snap.get("rows") or [])
    if exchange:
        rows = [r for r in rows if (r.get("exchange") or "").upper() == exchange.upper()]
    liq = lm.Liquidity(min_price=Decimal(str(min_price)), min_avg_turnover=min_avg_turnover)
    top: dict[str, list[Any]] = {}
    cov = lm.MoverCoverage(universe_size=len(rows))
    for kind in kinds:
        items, cov = lm.rank_movers(
            rows,
            kind,
            limit=limit,
            liquidity=liq,
            min_rvol=min_rvol,
            universe_size=len(rows),
            include_stale=include_stale,
        )
        top[kind] = list(items)
    return {
        "universe": snap.get("universe"),
        "computed_at": snap.get("computed_at"),
        "quotes_as_of": snap.get("quotes_as_of"),
        "market": snap.get("market"),
        "coverage": cov.as_dict(),
        "scope": scope(snap, cov),
        "filters": {
            "min_price": min_price,
            "min_avg_turnover": min_avg_turnover,
            "min_rvol": min_rvol,
            "include_stale": include_stale,
            "exchange": exchange,
        },
        "top": top,
    }


def default_movers(snap: Mapping[str, Any], settings: Settings, limit: int = 10) -> dict[str, Any]:
    return movers_summary(
        snap,
        limit=limit,
        min_price=settings.MARKET_LIVE_MIN_PRICE,
        min_avg_turnover=settings.MARKET_LIVE_MIN_AVG_TURNOVER,
        min_rvol=settings.STOCK_SIGNALS_UNUSUAL_RVOL,
    )


BULLISH = ("BULLISH_CONFIRMED", "BULLISH_PENDING", "BULLISH_TREND")
BEARISH = ("BEARISH_CONFIRMED", "BEARISH_PENDING", "BEARISH_TREND")


async def confirmed_states(
    db: AsyncSession, ids: Sequence[int], tf: str
) -> dict[int, tuple[Any, Instrument]]:
    """The stored (closed-candle) Stock Signals state per instrument for `tf`."""
    from app.models.stock_signals import StockSignalState

    if not ids:
        return {}
    rows = await db.execute(
        select(StockSignalState, Instrument)
        .join(Instrument, Instrument.id == StockSignalState.instrument_id)
        .where(StockSignalState.instrument_id.in_(list(ids)), StockSignalState.timeframe == tf)
    )
    return {st.instrument_id: (st, inst) for st, inst in rows.all()}


def _blank(row: Mapping[str, Any], tf: str) -> dict[str, Any]:
    return {
        "instrument_id": row.get("instrument_id"),
        "symbol": row.get("symbol") or row.get("ref") or "",
        "name": row.get("name"),
        "exchange": row.get("exchange"),
        "segment": row.get("segment"),
        "timeframe": tf,
        "state": "UNAVAILABLE",
        "last_price": None,
        "change_pct": None,
        "trend_score": None,
        "rvol": None,
        "rsi": None,
        "adx": None,
        "ema_fast": None,
        "ema_slow": None,
        "vwap": None,
        "vwap_distance_pct": None,
        "ema_spread_pct": None,
        "relative_pct": None,
        "missing_features": [],
        "in_holdings": False,
        "event_id": None,
        "candle_close_at": None,
        "reason": f"no confirmed {tf} signal state for this instrument",
    }


def live_mover_item(
    row: Mapping[str, Any], confirmed: tuple[Any, Instrument] | None, tf: str
) -> dict[str, Any]:
    """A live mover in the StockSignalStateOut shape: live price/change/volume, plus the
    instrument's confirmed (closed-candle) signal state when it has one."""
    from app.services import stock_rankings

    base = stock_rankings.row(*confirmed) if confirmed else _blank(row, tf)
    if confirmed:
        base["reason"] = confirmed[0].reason
    return {
        **base,
        "basis": "live",
        "last_price": row.get("ltp"),
        "change_pct": row.get("change_pct"),
        "rvol": row.get("rvol"),
        "prev_close": row.get("prev_close"),
        "prev_close_source": row.get("prev_close_source"),
        "volume": row.get("volume"),
        "turnover": row.get("turnover"),
        "avg_turnover": row.get("avg_turnover"),
        "quote_status": row.get("status"),
        "quote_age_seconds": row.get("age_seconds"),
        "last_trade_at": row.get("ltt"),
    }


def live_trending(
    rows: Sequence[Mapping[str, Any]],
    direction: str,
    *,
    limit: int,
    exchange: str | None = None,
) -> list[Mapping[str, Any]]:
    """Live EMA/VWAP rows that agree with `direction`, by the live trend score (the same rule
    as the confirmed `trending`: state, EMA order and score all on the same side)."""
    picked = []
    for r in rows:
        if exchange and (r.get("exchange") or "").upper() != exchange.upper():
            continue
        score, ef, es = r.get("trend_score"), lm.dec(r.get("ema_fast")), lm.dec(r.get("ema_slow"))
        if score is None or ef is None or es is None:
            continue
        if direction == "bullish" and r.get("state") in BULLISH and ef > es and score > 50:
            picked.append(r)
        elif direction == "bearish" and r.get("state") in BEARISH and ef < es and score < 50:
            picked.append(r)
    sign = -1 if direction == "bullish" else 1
    picked.sort(key=lambda r: (sign * float(r["trend_score"]), int(r.get("instrument_id") or 0)))
    return picked[:limit]


def live_state_item(
    row: Mapping[str, Any], confirmed: tuple[Any, Instrument] | None
) -> dict[str, Any]:
    """A live EMA/VWAP state in the StockSignalStateOut shape."""
    base = _blank(row, str(row.get("timeframe")))
    candle_ts = lm.ts_of(row.get("candle_ts"))
    if candle_ts is not None:
        from app.services.market_session import candle_close

        base["candle_close_at"] = candle_close(candle_ts, Timeframe(str(row["timeframe"])))
    return {
        **base,
        "basis": "live",
        "state": row.get("state"),
        "last_price": row.get("last_price"),
        "trend_score": row.get("trend_score"),
        "ema_fast": row.get("ema_fast"),
        "ema_slow": row.get("ema_slow"),
        "vwap": row.get("vwap"),
        "vwap_distance_pct": row.get("vwap_distance_pct"),
        "ema_spread_pct": row.get("ema_spread_pct"),
        "in_holdings": bool(confirmed and confirmed[0].in_holdings),
        "reason": row.get("reason"),
        "candle_closed": row.get("candle_closed"),
        "partial_bars": row.get("partial_bars"),
    }


# --- the database ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstInfo:
    id: int
    ref: str
    symbol: str
    name: str | None
    exchange: str | None
    segment: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "instrument_id": self.id,
            "symbol": self.symbol,
            "name": self.name,
            "exchange": self.exchange,
            "segment": self.segment,
        }


def info(inst: Instrument) -> InstInfo:
    return InstInfo(
        inst.id,
        inst.broker_instrument_id,
        inst.trading_symbol or inst.symbol,
        inst.display_name or inst.name,
        inst.exchange,
        inst.segment,
    )


async def instruments_by_id(db: AsyncSession, ids: Sequence[int]) -> dict[int, Instrument]:
    if not ids:
        return {}
    rows = await db.scalars(
        select(Instrument).where(Instrument.id.in_(list(ids)), Instrument.broker == "dhan")
    )
    return {i.id: i for i in rows}


async def instruments_by_ref(db: AsyncSession, refs: Sequence[str]) -> dict[str, InstInfo]:
    out: dict[str, InstInfo] = {}
    refs = list(dict.fromkeys(refs))
    for i in range(0, len(refs), 2000):
        rows = await db.scalars(
            select(Instrument).where(
                Instrument.broker == "dhan", Instrument.broker_instrument_id.in_(refs[i : i + 2000])
            )
        )
        for r in rows:
            out[r.broker_instrument_id] = info(r)
    return out


async def universe(db: AsyncSession, name: str, cap: int) -> tuple[list[InstInfo], int]:
    """A live universe (same names as DHAN_UNIVERSE_BACKFILL), at most `cap` instruments,
    ordered by id so the cap is deterministic, and its size before the cap."""
    all_ids = sorted(await market_data.universe_ids(db, name))
    ids = all_ids[:cap]
    by_id: dict[int, Instrument] = {}
    for i in range(0, len(ids), 2000):
        by_id.update(await instruments_by_id(db, ids[i : i + 2000]))
    return [info(by_id[i]) for i in ids if i in by_id], len(all_ids)


async def daily_reference(
    db: AsyncSession, ids: Sequence[int], now: datetime, *, sessions: int = 20
) -> tuple[dict[int, dict[date, Decimal]], dict[int, float]]:
    """Per instrument: stored daily closes by IST date (the previous-close fallback), and the
    average traded value (close x volume) of the last `sessions` daily candles before today."""
    closes: dict[int, dict[date, Decimal]] = {}
    turnover: dict[int, list[float]] = {}
    today = lm.session_day(now)
    since = now - timedelta(days=sessions * 2 + 10)
    ids = list(ids)
    for i in range(0, len(ids), 2000):
        rows = await db.execute(
            select(
                MarketCandle.instrument_id, MarketCandle.ts, MarketCandle.close, MarketCandle.volume
            )
            .where(
                MarketCandle.instrument_id.in_(ids[i : i + 2000]),
                MarketCandle.timeframe == Timeframe.D1.value,
                MarketCandle.is_gap.is_(False),
                MarketCandle.ts >= since,
                MarketCandle.ts <= now,
            )
            .order_by(MarketCandle.instrument_id, MarketCandle.ts)
        )
        for iid, ts, close, vol in rows:
            d = lm.session_day(ts)
            closes.setdefault(iid, {})[d] = Decimal(close)
            if d < today and vol is not None:
                turnover.setdefault(iid, []).append(float(close) * float(vol))
    avg = {iid: sum(v[-sessions:]) / len(v[-sessions:]) for iid, v in turnover.items() if v}
    return closes, avg


async def volume_profiles(
    db: AsyncSession, ids: Sequence[int], now: datetime, *, sessions: int = 10
) -> dict[int, list[float] | None]:
    """Time-of-day volume profile per instrument from stored 15m candles (lm.volume_profile)."""
    bars: dict[int, list[tuple[datetime, float]]] = {}
    since = now - timedelta(days=sessions * 2 + 6)
    ids = list(ids)
    for i in range(0, len(ids), 1000):
        rows = await db.execute(
            select(MarketCandle.instrument_id, MarketCandle.ts, MarketCandle.volume).where(
                MarketCandle.instrument_id.in_(ids[i : i + 1000]),
                MarketCandle.timeframe == Timeframe.M15.value,
                MarketCandle.is_gap.is_(False),
                MarketCandle.ts >= since,
                MarketCandle.ts <= now,
            )
        )
        for iid, ts, vol in rows:
            if vol is not None:
                bars.setdefault(iid, []).append((ts, float(vol)))
    today = lm.session_day(now)
    return {iid: lm.volume_profile(b, today, sessions=sessions) for iid, b in bars.items()}


async def stored_candles(
    db: AsyncSession, instrument_id: int, tf: Timeframe, *, limit: int, now: datetime
) -> list[MarketCandle]:
    """The newest `limit` real (non-gap) stored candles up to now, ascending."""
    rows = (
        await db.scalars(
            select(MarketCandle)
            .where(
                MarketCandle.instrument_id == instrument_id,
                MarketCandle.timeframe == tf.value,
                MarketCandle.is_gap.is_(False),
                MarketCandle.ts <= now,
            )
            .order_by(MarketCandle.ts.desc())
            .limit(limit)
        )
    ).all()
    return list(reversed(rows))


# --- the chart ---------------------------------------------------------------------------------


def chart_rows(
    stored: Sequence[Any],
    live_closed: Sequence[lm.LiveBar],
    forming: lm.LiveBar | None,
    tf: Timeframe,
    now: datetime,
) -> list[dict[str, Any]]:
    """Stored candles, then live bars for periods after the newest stored *closed* candle.
    A stored candle that is still forming (Dhan serves the current bar) gives way to the live
    forming bar, which is fresher; it is kept when there is no live bar."""
    from app.services.market_session import candle_close

    rows: list[dict[str, Any]] = []
    for c in stored:
        closed = candle_close(c.ts, tf) <= now
        rows.append(
            {
                "ts": c.ts,
                "open": Decimal(c.open),
                "high": Decimal(c.high),
                "low": Decimal(c.low),
                "close": Decimal(c.close),
                "volume": Decimal(c.volume) if c.volume is not None else None,
                "is_closed": closed,
                "source": "broker" if c.source != "ticks" else "ticks",
                "partial": False,
            }
        )
    while rows and not rows[-1]["is_closed"] and (live_closed or forming):
        rows.pop()
    last: datetime | None = rows[-1]["ts"] if rows else None

    def newer(b: lm.LiveBar) -> bool:
        if last is None:
            return True
        if tf is Timeframe.D1:
            return lm.session_day(b.ts) > lm.session_day(last)
        return b.ts > last

    live = [b for b in live_closed if newer(b)]
    if forming is not None and newer(forming) and (not live or forming.ts > live[-1].ts):
        live.append(forming)
    for b in live:
        rows.append(
            {
                "ts": b.ts,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "is_closed": b.closed,
                "source": "ticks",
                "partial": b.partial,
            }
        )
    return rows


async def chart(
    db: AsyncSession,
    redis: Any,
    inst: Instrument,
    tf: Timeframe,
    *,
    limit: int,
    include_live: bool,
    settings: Settings,
    now: datetime,
    fast: int = 8,
    slow: int = 32,
) -> dict[str, Any]:
    """Candles + EMA fast/slow + session VWAP + crossover markers + session boundaries for one
    instrument. Indicators are computed over `limit` + WARMUP_BARS candles and the first
    WARMUP_BARS are cut, so the visible values do not depend on where the window starts."""
    stored = await stored_candles(db, inst.id, tf, limit=limit + WARMUP_BARS, now=now)
    live_closed: list[lm.LiveBar] = []
    forming: lm.LiveBar | None = None
    live_error = None
    if include_live and inst.broker == "dhan" and tf in lm.LIVE_TIMEFRAMES:
        try:
            live_closed, forming = await read_bars(redis, inst.broker_instrument_id, tf)
        except Exception as exc:  # Redis down: stored candles still serve
            live_error = f"live bars unavailable ({type(exc).__name__})"
    rows = chart_rows(stored, live_closed, forming, tf, now)
    ov = lm.chart_overlays(rows, tf, fast=fast, slow=slow)
    cut = max(0, len(rows) - limit)
    rows = rows[cut:]
    ema_f, ema_s, vw = ov["ema_fast"][cut:], ov["ema_slow"][cut:], ov["vwap"][cut:]
    first = rows[0]["ts"] if rows else None
    markers = [m for m in ov["markers"] if first is None or m["ts"] >= first]
    candles = []
    for i, r in enumerate(rows):
        candles.append(
            {
                **r,
                "ema_fast": ema_f[i],
                "ema_slow": ema_s[i],
                "vwap": vw[i],
            }
        )
    return {
        "instrument_id": inst.id,
        "ref": inst.broker_instrument_id,
        "symbol": inst.trading_symbol or inst.symbol,
        "timeframe": tf.value,
        "ema_fast_period": fast,
        "ema_slow_period": slow,
        "vwap_applicable": tf is not Timeframe.D1,
        "candles": candles,
        "markers": markers,
        "sessions": lm.session_marks(first, now, settings.DHAN_MARKET_HOLIDAYS) if first else [],
        "market": market(now, settings.DHAN_MARKET_HOLIDAYS),
        "live": {
            "included": include_live,
            "bars": sum(1 for r in rows if r["source"] == "ticks"),
            "partial_bars": sum(1 for r in rows if r["partial"]),
            "error": live_error,
        },
        "data_as_of": rows[-1]["ts"] if rows else None,
        "step_seconds": int(tf_delta(tf).total_seconds()),
        "computed_at": now,
    }


__all__ = [
    "BARS_KEY",
    "BAR_KEY",
    "CHANNELS",
    "CH_CANDLES",
    "CH_QUOTES",
    "CH_RANKINGS",
    "CH_SCREENER",
    "MOVERS_KEY",
    "QUOTE_KEY",
    "SCREENER_KEY",
    "STATUS_KEY",
    "InstInfo",
    "chart",
    "chart_rows",
    "daily_reference",
    "instruments_by_id",
    "instruments_by_ref",
    "market",
    "read_bars",
    "read_json",
    "read_quotes",
    "universe",
    "volume_profiles",
]
