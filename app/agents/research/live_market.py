"""Live NSE/BSE market computations (pure, no I/O): docs/38-live-market.md.

Everything here is deterministic and works on values the Dhan feed sent. Nothing is invented:
a bar with no trades does not exist, a missing previous close is `None`, and a bar the server
did not watch from its start is marked `partial`.

- **Buckets** are session-aligned in IST: 1m/5m/15m/1h bars start at 09:15 and the last one
  is cut at 15:30 (the 15:15 hourly bar closes at 15:30), exactly as `market_session` defines
  them for stored candles. A daily bar is the IST date.
- **CandleBook** turns feed updates (LTP, last trade time, cumulative day volume) into bars for
  several timeframes at once. Duplicates are ignored, a packet older than the newest one only
  widens the high/low of the bar it belongs to, and a packet for a bar that already closed is
  counted as late and dropped. Bars close when a later packet arrives or when the clock passes
  their end (plus a small grace), so the last bar of the session closes at 15:30 even if no
  packet follows.
- **Continuity:** the first packet for an instrument after a start or a feed reconnect only
  sets the volume baseline; the bar it lands in is `partial` (trades before it were not seen).
  Bars that start after it are complete.
- **Previous close:** Dhan's prev-close packet when it was received on the quote's session day,
  else the stored daily candle of the previous trading day, else `None`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.agents.research import indicators as ind
from app.agents.research.screener import _carried_sign
from app.agents.trading.dhan.parse import IST
from app.agents.trading.types import Timeframe
from app.services.market_session import is_trading_day, session_bounds

LIVE_TIMEFRAMES: tuple[Timeframe, ...] = (
    Timeframe.M1,
    Timeframe.M5,
    Timeframe.M15,
    Timeframe.H1,
    Timeframe.D1,
)
STEP: dict[Timeframe, timedelta] = {
    Timeframe.M1: timedelta(minutes=1),
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.H1: timedelta(hours=1),
}
VWAP_TZ = "Asia/Kolkata"
PCT = Decimal("0.0001")
SLOT = timedelta(minutes=15)
SLOTS = 25  # 09:15 → 15:30 in 15-minute slots

QuoteStatus = Literal["live", "stale", "closed", "no_data"]


def dec(v: Any) -> Decimal | None:
    """A finite Decimal, or None (never raises on feed strings)."""
    if v is None or v == "":
        return None
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def ts_of(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.astimezone(UTC) if v.tzinfo else None
    try:
        t = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return t.astimezone(UTC) if t.tzinfo else None


def session_day(ts: datetime) -> date:
    return ts.astimezone(IST).date()


def bucket(ts: datetime, tf: Timeframe) -> tuple[datetime, datetime] | None:
    """(start, end) of the bar that contains `ts`, or None outside the session (intraday).
    A daily bar starts at IST midnight and ends at that day's session close."""
    day = session_day(ts)
    lo, hi = session_bounds(day)
    if tf is Timeframe.D1:
        return datetime.combine(day, time(0), IST).astimezone(UTC), hi
    step = STEP.get(tf)
    if step is None:
        raise ValueError(f"no live bars for {tf.value}")
    if ts < lo or ts >= hi:
        return None
    start = lo + ((ts - lo) // step) * step
    return start, min(start + step, hi)


def previous_trading_day(day: date, holidays: Iterable[date] = ()) -> date:
    hol = set(holidays)
    d = day - timedelta(days=1)
    for _ in range(30):
        if is_trading_day(d, hol):
            return d
        d -= timedelta(days=1)
    return d


# --- bars ------------------------------------------------------------------------------------


@dataclass(slots=True)
class LiveBar:
    key: str
    tf: Timeframe
    ts: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    ticks: int = 0
    partial: bool = False
    closed: bool = False
    last_ltt: datetime | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "timeframe": self.tf.value,
            "ts": self.ts.isoformat(),
            "end": self.end.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume) if self.volume is not None else None,
            "ticks": self.ticks,
            "partial": self.partial,
            "closed": self.closed,
            "last_trade_at": self.last_ltt.isoformat() if self.last_ltt else None,
            "source": "ticks",
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> LiveBar | None:
        try:
            ts, end = ts_of(d["ts"]), ts_of(d["end"])
            o, h, lo, c = (dec(d[k]) for k in ("open", "high", "low", "close"))
            if None in (ts, end, o, h, lo, c):
                return None
            return cls(
                key=str(d["key"]),
                tf=Timeframe(d["timeframe"]),
                ts=ts,  # type: ignore[arg-type]
                end=end,  # type: ignore[arg-type]
                open=o,  # type: ignore[arg-type]
                high=h,  # type: ignore[arg-type]
                low=lo,  # type: ignore[arg-type]
                close=c,  # type: ignore[arg-type]
                volume=dec(d.get("volume")),
                ticks=int(d.get("ticks") or 0),
                partial=bool(d.get("partial")),
                closed=bool(d.get("closed")),
                last_ltt=ts_of(d.get("last_trade_at")),
            )
        except (KeyError, ValueError, TypeError):
            return None


@dataclass(slots=True)
class _KeyState:
    ltt: datetime
    ltp: Decimal
    cum: Decimal | None  # highest cumulative day volume seen today
    day: date
    epoch: int


@dataclass
class BookCounters:
    accepted: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    late: int = 0
    outside_session: int = 0
    ignored: int = 0
    closed_bars: int = 0


class CandleBook:
    """Feed updates → session-aligned bars for several timeframes."""

    def __init__(
        self, timeframes: Sequence[Timeframe] = LIVE_TIMEFRAMES, *, grace_seconds: float = 2.0
    ) -> None:
        for tf in timeframes:
            if tf is not Timeframe.D1 and tf not in STEP:
                raise ValueError(f"no live bars for {tf.value}")
        self.tfs = tuple(timeframes)
        self.grace = timedelta(seconds=grace_seconds)
        self.forming: dict[tuple[str, Timeframe], LiveBar] = {}
        self.last_closed: dict[tuple[str, Timeframe], datetime] = {}
        self.state: dict[str, _KeyState] = {}
        self.epoch = 0
        self.counters = BookCounters()

    def reset_continuity(self) -> None:
        """A feed reconnect or a lost subscription: packets may have been missed. Every bar that
        is forming now becomes partial, and each instrument's next packet only re-sets its volume
        baseline."""
        self.epoch += 1
        for b in self.forming.values():
            b.partial = True

    def restore(self, bar: LiveBar) -> None:
        """A forming bar saved before a restart: kept, but partial (packets in between were not
        seen)."""
        if bar.closed:
            return
        bar.partial = True
        self.forming[(bar.key, bar.tf)] = bar

    def apply(
        self,
        key: str,
        *,
        ltt: datetime | None,
        ltp: Decimal | None,
        cum_volume: Decimal | None = None,
        day_open: Decimal | None = None,
        day_high: Decimal | None = None,
        day_low: Decimal | None = None,
    ) -> list[LiveBar]:
        """One feed update. Returns the bars it changed: newly closed ones first, then the
        forming ones (same objects as in `forming`; serialise before mutating further)."""
        if ltt is None or ltp is None or not ltp.is_finite() or ltp <= 0:
            self.counters.ignored += 1
            return []
        st = self.state.get(key)
        day = session_day(ltt)
        if (
            st is not None
            and st.ltt == ltt
            and st.ltp == ltp
            and (cum_volume is None or st.cum == cum_volume)
        ):
            self.counters.duplicates += 1
            return []
        older = st is not None and ltt < st.ltt
        # Volume: the change in the day's cumulative volume since the last packet we saw in the
        # same connection epoch and day. Unknown (None) for a baseline packet.
        delta: Decimal | None = None
        cum_max: Decimal | None
        baseline = st is None or st.epoch != self.epoch or st.day != day
        if cum_volume is not None:
            if not baseline and st is not None and st.cum is not None:
                delta = max(cum_volume - st.cum, Decimal(0))
            cum_max = (
                cum_volume if baseline or st is None or st.cum is None else max(st.cum, cum_volume)
            )
        else:
            cum_max = None if baseline or st is None else st.cum
        if older:
            self.counters.out_of_order += 1
            assert st is not None
            st.cum = cum_max
        else:
            self.state[key] = _KeyState(ltt, ltp, cum_max, day, self.epoch)
        self.counters.accepted += 1

        out_closed: list[LiveBar] = []
        out_forming: list[LiveBar] = []
        for tf in self.tfs:
            span = bucket(ltt, tf)
            if span is None:
                self.counters.outside_session += 1
                continue
            start, end = span
            k = (key, tf)
            done = self.last_closed.get(k)
            if done is not None and start <= done:
                self.counters.late += 1
                continue
            cur = self.forming.get(k)
            if cur is not None and start < cur.ts:
                self.counters.late += 1
                continue
            if cur is not None and start > cur.ts:
                out_closed.append(self._close(cur))
                cur = None
            daily = tf is Timeframe.D1
            if cur is None:
                if older:
                    continue  # an old packet never opens a bar
                cur = LiveBar(
                    key=key,
                    tf=tf,
                    ts=start,
                    end=end,
                    open=ltp,
                    high=ltp,
                    low=ltp,
                    close=ltp,
                    volume=(cum_volume if daily else delta),
                    ticks=0,
                    partial=baseline,
                    last_ltt=ltt,
                )
                if daily and day_open is not None and day_high is not None and day_low is not None:
                    # The exchange's own day OHLC: complete whatever we missed.
                    cur.open, cur.high, cur.low = day_open, max(day_high, ltp), min(day_low, ltp)
                    cur.partial = cum_volume is None
                self.forming[k] = cur
            else:
                cur.high = max(cur.high, ltp)
                cur.low = min(cur.low, ltp)
                if not older:
                    cur.close = ltp
                    cur.last_ltt = ltt
                if daily:
                    if cum_volume is not None:
                        cur.volume = max(cur.volume or Decimal(0), cum_volume)
                    if day_high is not None:
                        cur.high = max(cur.high, day_high)
                    if day_low is not None:
                        cur.low = min(cur.low, day_low)
                elif delta is not None:
                    cur.volume = (cur.volume or Decimal(0)) + delta
                elif baseline:
                    cur.partial = True
            cur.ticks += 1
            out_forming.append(cur)
        return out_closed + out_forming

    def _close(self, bar: LiveBar) -> LiveBar:
        bar.closed = True
        self.forming.pop((bar.key, bar.tf), None)
        self.last_closed[(bar.key, bar.tf)] = bar.ts
        self.counters.closed_bars += 1
        return bar

    def close_due(self, now: datetime) -> list[LiveBar]:
        """Close every bar whose period ended (plus the grace) by `now`."""
        due = [b for b in self.forming.values() if b.end + self.grace <= now]
        return [self._close(b) for b in sorted(due, key=lambda b: (b.ts, b.key, b.tf.value))]

    def get(self, key: str, tf: Timeframe) -> LiveBar | None:
        return self.forming.get((key, tf))


# --- quotes and the previous close -------------------------------------------------------------


@dataclass(slots=True)
class LiveQuote:
    """The merged state of one instrument from feed packets."""

    key: str
    ltp: Decimal | None = None
    ltt: datetime | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    volume: Decimal | None = None
    atp: Decimal | None = None
    oi: int | None = None
    prev_close_feed: Decimal | None = None
    prev_close_feed_at: datetime | None = None
    received_at: datetime | None = None

    def merge(self, packet: Mapping[str, Any], received_at: datetime) -> bool:
        """Apply one `dhan:ticks` packet. Returns False when it was older than what we have."""
        kind = packet.get("kind")
        if kind == "prev_close":
            pc = dec(packet.get("prev_close"))
            if pc is not None and pc > 0:
                self.prev_close_feed, self.prev_close_feed_at = pc, received_at
            return True
        ltt = ts_of(packet.get("ltt"))
        if ltt is not None and self.ltt is not None and ltt < self.ltt:
            return False
        ltp = dec(packet.get("ltp"))
        if ltp is not None and ltp > 0:
            self.ltp = ltp
        if ltt is not None:
            self.ltt = ltt
        for name in ("open", "high", "low", "atp"):
            v = dec(packet.get(name))
            if v is not None and v > 0:
                setattr(self, name, v)
        vol = dec(packet.get("volume"))
        if vol is not None and vol >= 0:
            self.volume = vol
        if packet.get("oi") not in (None, ""):
            try:
                self.oi = int(packet["oi"])
            except (TypeError, ValueError):
                pass
        self.received_at = received_at
        return True


def resolve_prev_close(
    *,
    day: date,
    feed_value: Decimal | None,
    feed_at: datetime | None,
    daily_closes: Mapping[date, Decimal],
    holidays: Iterable[date] = (),
) -> tuple[Decimal | None, str | None, date]:
    """The close of the trading session before `day` (the quote's session): (value, source,
    previous trading day). Source is `feed` (Dhan's prev-close packet, received during `day`
    before its close), `daily_candle` (the stored daily candle of the previous trading day) or
    None when neither is known."""
    prev_day = previous_trading_day(day, holidays)
    if feed_value is not None and feed_at is not None:
        _, close_at = session_bounds(day)
        if session_day(feed_at) == day and feed_at < close_at:
            return feed_value, "feed", prev_day
    got = daily_closes.get(prev_day)
    if got is not None and got > 0:
        return got, "daily_candle", prev_day
    return None, None, prev_day


def quote_status(
    q: LiveQuote,
    now: datetime,
    *,
    market_open: bool,
    stale_after: float,
    stale_flag: bool = False,
) -> QuoteStatus:
    """`closed` outside the session (silence is expected), `stale` when the market is open but
    nothing arrived for `stale_after` seconds, `no_data` without a price."""
    if q.ltp is None:
        return "no_data"
    if not market_open:
        return "closed"
    if stale_flag or q.received_at is None:
        return "stale"
    return "stale" if (now - q.received_at).total_seconds() > stale_after else "live"


def pct(a: Decimal, b: Decimal) -> Decimal:
    return ((a - b) / b * 100).quantize(PCT)


def quote_view(
    q: LiveQuote,
    now: datetime,
    *,
    daily_closes: Mapping[date, Decimal],
    market_open: bool,
    stale_after: float,
    holidays: Iterable[date] = (),
    stale_flag: bool = False,
) -> dict[str, Any]:
    """The JSON a browser gets for one instrument (quote events and snapshots)."""
    status = quote_status(
        q, now, market_open=market_open, stale_after=stale_after, stale_flag=stale_flag
    )
    ref = q.ltt or q.received_at
    prev, src, prev_day = (None, None, None)
    if ref is not None:
        prev, src, prev_day = resolve_prev_close(
            day=session_day(ref),
            feed_value=q.prev_close_feed,
            feed_at=q.prev_close_feed_at,
            daily_closes=daily_closes,
            holidays=holidays,
        )
    change = change_pct = None
    if q.ltp is not None and prev is not None and prev > 0:
        change = q.ltp - prev
        change_pct = pct(q.ltp, prev)
    turnover = None
    if q.volume is not None and (q.atp or q.ltp) is not None:
        turnover = (q.volume * (q.atp or q.ltp)).quantize(Decimal(1))  # type: ignore[operator]
    age = (now - q.received_at).total_seconds() if q.received_at else None
    return {
        "key": q.key,
        "ltp": _s(q.ltp),
        "ltt": q.ltt.isoformat() if q.ltt else None,
        "session_date": session_day(ref).isoformat() if ref else None,
        "open": _s(q.open),
        "high": _s(q.high),
        "low": _s(q.low),
        "volume": _s(q.volume),
        "atp": _s(q.atp),
        "turnover": _s(turnover),
        "oi": q.oi,
        "prev_close": _s(prev),
        "prev_close_source": src,
        "prev_close_date": prev_day.isoformat() if prev_day and prev is not None else None,
        "change": _s(change),
        "change_pct": _s(change_pct),
        "status": status,
        "age_seconds": round(age, 3) if age is not None else None,
        "received_at": q.received_at.isoformat() if q.received_at else None,
        "source": "dhan_feed",
    }


def _s(v: Decimal | None) -> str | None:
    return None if v is None else format(v, "f")


# --- relative volume (time-of-day adjusted) ------------------------------------------------------


def volume_profile(
    bars: Sequence[tuple[datetime, float]],
    today: date,
    *,
    sessions: int = 10,
    min_sessions: int = 3,
) -> list[float] | None:
    """Average cumulative volume at the end of each 15-minute slot (25 slots, 09:15-15:30) over
    up to `sessions` earlier sessions, from stored 15m candles. A session counts when its data
    reaches the last slot; a slot with no candle had no trades (volume 0). None with fewer than
    `min_sessions` usable sessions."""
    by_day: dict[date, list[float]] = {}
    complete: set[date] = set()
    for ts, vol in bars:
        d = session_day(ts)
        if d >= today or not math.isfinite(vol) or vol < 0:
            continue
        lo, _ = session_bounds(d)
        k = int((ts - lo) // SLOT)
        if 0 <= k < SLOTS:
            by_day.setdefault(d, [0.0] * SLOTS)[k] += vol
            if k == SLOTS - 1:
                complete.add(d)
    usable = [np.cumsum(v) for d, v in sorted(by_day.items()) if d in complete][-sessions:]
    if len(usable) < min_sessions:
        return None
    prof = np.mean(np.vstack(usable), axis=0)
    return [float(x) for x in prof]


def expected_volume(profile: Sequence[float], now: datetime) -> float | None:
    """Expected cumulative volume by `now` on a normal session (linear within a slot)."""
    lo, hi = session_bounds(session_day(now))
    if now <= lo:
        return None
    if now >= hi:
        return float(profile[-1])
    span = (now - lo) / SLOT
    k = int(span)
    frac = span - k
    before = float(profile[k - 1]) if k > 0 else 0.0
    return before + frac * (float(profile[k]) - before)


def live_rvol(
    volume: Decimal | None,
    profile: Sequence[float] | None,
    now: datetime,
    *,
    min_elapsed: timedelta = SLOT,
) -> float | None:
    """Today's volume so far / the volume a normal session had traded by this time of day.
    None before `min_elapsed` of trading, without a profile, or without volume."""
    if volume is None or profile is None:
        return None
    lo, _ = session_bounds(session_day(now))
    if now - lo < min_elapsed:
        return None
    exp = expected_volume(profile, now)
    if exp is None or exp <= 0:
        return None
    return float(volume) / exp


# --- market movers -------------------------------------------------------------------------------

MoverKind = Literal["gainers", "losers", "unusual_volume"]


@dataclass(frozen=True, slots=True)
class Liquidity:
    min_price: Decimal = Decimal(0)
    min_avg_turnover: float = 0.0


@dataclass
class MoverCoverage:
    universe_size: int = 0
    with_quote: int = 0
    live: int = 0
    stale: int = 0
    closed: int = 0
    no_data: int = 0
    no_prev_close: int = 0
    illiquid: int = 0
    liquidity_unknown: int = 0
    evaluated: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def rank_movers(
    rows: Sequence[Mapping[str, Any]],
    kind: MoverKind,
    *,
    limit: int,
    liquidity: Liquidity,
    min_rvol: float,
    universe_size: int,
    include_stale: bool = False,
) -> tuple[list[Mapping[str, Any]], MoverCoverage]:
    """Rank live rows (quote_view + `instrument_id`, `avg_turnover`, `rvol`).

    Usable: a `live` quote (or `closed`: outside the session the last session's numbers are
    ranked, and the caller says so), `stale` only with include_stale. Liquid: price at least
    `min_price` and average daily turnover at least `min_avg_turnover` (unknown turnover is
    excluded when a minimum is set). Gainers: change > 0, largest first; losers: change < 0,
    smallest first; unusual volume: rvol >= min_rvol, largest first. Ties by instrument id."""
    cov = MoverCoverage(universe_size=universe_size)
    pool: list[Mapping[str, Any]] = []
    ok = {"live", "closed"} | ({"stale"} if include_stale else set())
    for r in rows:
        st = r.get("status")
        if r.get("ltp") is not None:
            cov.with_quote += 1
        if st in ("live", "stale", "closed", "no_data"):
            setattr(cov, st, getattr(cov, st) + 1)
        if st not in ok:
            continue
        ltp = dec(r.get("ltp"))
        if ltp is None or ltp < liquidity.min_price:
            cov.illiquid += 1
            continue
        if liquidity.min_avg_turnover > 0:
            at = r.get("avg_turnover")
            if at is None:
                cov.liquidity_unknown += 1
                continue
            if float(at) < liquidity.min_avg_turnover:
                cov.illiquid += 1
                continue
        if r.get("change_pct") is None:
            cov.no_prev_close += 1
            if kind != "unusual_volume":
                continue
        cov.evaluated += 1
        pool.append(r)

    def iid(r: Mapping[str, Any]) -> int:
        return int(r.get("instrument_id") or 0)

    if kind == "unusual_volume":
        picked = [r for r in pool if r.get("rvol") is not None and float(r["rvol"]) >= min_rvol]
        picked.sort(key=lambda r: (-float(r["rvol"]), iid(r)))
    else:
        sign = 1 if kind == "gainers" else -1
        picked = [
            r
            for r in pool
            if r.get("change_pct") is not None and sign * Decimal(r["change_pct"]) > 0
        ]
        picked.sort(key=lambda r: (-sign * Decimal(r["change_pct"]), iid(r)))
    return picked[:limit], cov


# --- chart overlays ------------------------------------------------------------------------------


def chart_overlays(
    rows: Sequence[Mapping[str, Any]],
    tf: Timeframe,
    *,
    fast: int = 8,
    slow: int = 32,
) -> dict[str, Any]:
    """EMA fast/slow, session VWAP (intraday) and EMA crossover markers for ascending chart
    rows (`ts`, `open`, `high`, `low`, `close`, `volume`, `is_closed`). Every value at bar i
    uses rows 0..i only, with the same functions as the screener and Stock Signals, so a
    closed bar's values match theirs over the same window. A marker on a bar that is still
    forming is `confirmed: false` (it can disappear before the bar closes)."""
    if not rows:
        return {"ema_fast": [], "ema_slow": [], "vwap": [], "markers": []}
    df = pd.DataFrame(
        {
            "open": [float(r["open"]) for r in rows],
            "high": [float(r["high"]) for r in rows],
            "low": [float(r["low"]) for r in rows],
            "close": [float(r["close"]) for r in rows],
            "volume": [float(r["volume"]) if r.get("volume") is not None else 0.0 for r in rows],
        },
        index=pd.DatetimeIndex([pd.Timestamp(r["ts"]) for r in rows]),
    )
    ef = ind.ema(df["close"], fast).to_numpy(dtype=float)
    es = ind.ema(df["close"], slow).to_numpy(dtype=float)
    if tf is Timeframe.D1:
        vw = np.full(len(df), np.nan)
    else:
        has_vol = [r.get("volume") is not None for r in rows]
        vw = ind.vwap(df, VWAP_TZ).to_numpy(dtype=float)
        vw = np.where(np.asarray(has_vol), vw, np.nan)
    sign = _carried_sign(ef - es)
    markers = []
    for i in range(1, len(rows)):
        # A cross needs a side on both bars: not during warm-up (NaN) or before any side (0).
        if not (np.isfinite(sign[i]) and np.isfinite(sign[i - 1])):
            continue
        if sign[i] == 0 or sign[i - 1] == 0:
            continue
        if sign[i] != sign[i - 1]:
            markers.append(
                {
                    "ts": rows[i]["ts"],
                    "kind": "bullish_cross" if sign[i] > 0 else "bearish_cross",
                    "price": str(rows[i]["close"]),
                    "ema_fast": ind.to_dec(float(ef[i])),
                    "ema_slow": ind.to_dec(float(es[i])),
                    "confirmed": bool(rows[i].get("is_closed", True)),
                }
            )

    def series(a: np.ndarray) -> list[str | None]:
        return [ind.to_dec(float(x)) if np.isfinite(x) else None for x in a]

    return {"ema_fast": series(ef), "ema_slow": series(es), "vwap": series(vw), "markers": markers}


def session_marks(
    start: datetime, end: datetime, holidays: Iterable[date] = ()
) -> list[dict[str, Any]]:
    """Every NSE/BSE session between two times: date, open and close (UTC)."""
    hol = set(holidays)
    out: list[dict[str, Any]] = []
    d = session_day(start)
    last = session_day(end)
    while d <= last:
        if is_trading_day(d, hol):
            lo, hi = session_bounds(d)
            out.append({"date": d.isoformat(), "open": lo.isoformat(), "close": hi.isoformat()})
        d += timedelta(days=1)
    return out


# --- live EMA/VWAP state (the screener's live side) ---------------------------------------------


@dataclass
class LiveSeries:
    """Stored (broker) closed candles + today's live bars after them + the forming bar."""

    frame: pd.DataFrame
    forming: LiveBar | None
    history_gap: bool = False
    partial_bars: int = 0
    live_bars: int = 0
    detail: list[str] = field(default_factory=list)


def _same_bar(a: datetime, b: datetime, tf: Timeframe) -> bool:
    return session_day(a) == session_day(b) if tf is Timeframe.D1 else a == b


def merge_series(
    stored: Sequence[Any],
    live_closed: Sequence[LiveBar],
    forming: LiveBar | None,
    tf: Timeframe,
    holidays: Iterable[date] = (),
) -> LiveSeries:
    """Join stored candles and live bars into one ascending frame. Live bars only extend the
    stored series (a stored candle always wins for the same period). `history_gap` is set when
    trading-time bars are missing between the newest stored candle and the first live bar: the
    EMAs would then be computed across a hole, so callers treat the reading as unavailable."""
    from app.services.market_session import candle_close, missed_bars

    rows: list[tuple[datetime, float, float, float, float, float]] = [
        (
            r.ts,
            float(r.open),
            float(r.high),
            float(r.low),
            float(r.close),
            float(r.volume) if r.volume is not None else 0.0,
        )
        for r in stored
    ]
    last = stored[-1].ts if stored else None
    extra = [b for b in live_closed if b.closed] + ([forming] if forming is not None else [])
    extra.sort(key=lambda b: b.ts)
    out = LiveSeries(frame=pd.DataFrame(), forming=None)
    added: list[LiveBar] = []
    for b in extra:
        if last is not None and (b.ts <= last or _same_bar(b.ts, last, tf)):
            continue
        added.append(b)
    if added and last is not None:
        missed = missed_bars(candle_close(last, tf), added[0].ts, tf, holidays)
        if missed > 0:
            out.history_gap = True
            out.detail.append(f"{missed:.0f} {tf.value} bars missing before the live bars")
    for b in added:
        rows.append(
            (
                b.ts,
                float(b.open),
                float(b.high),
                float(b.low),
                float(b.close),
                float(b.volume) if b.volume is not None else 0.0,
            )
        )
        out.partial_bars += int(b.partial)
    out.live_bars = len(added)
    out.forming = forming if forming is not None and forming in added else None
    out.frame = pd.DataFrame(
        [r[1:] for r in rows],
        columns=["open", "high", "low", "close", "volume"],
        index=pd.DatetimeIndex([r[0] for r in rows]),
    )
    return out


def live_reading(
    series: LiveSeries,
    tf: Timeframe,
    now: datetime,
    params: Any,
    holidays: Iterable[date] = (),
) -> tuple[Any, dict[str, Any]]:
    """The Stock Signals state of the newest bar *including the forming one*. The forming bar is
    evaluated as if it had closed at its end, with the same engine and parameters, so the live
    state uses exactly the confirmed rules; it is labelled live and never stored as a signal."""
    from app.agents.research import stock_signals as ss
    from app.services.market_session import candle_close

    flags: dict[str, Any] = {
        "candle_closed": series.forming is None,
        "partial_bars": series.partial_bars,
        "live_bars": series.live_bars,
        "history_gap": series.history_gap,
    }
    if series.history_gap:
        return (
            ss.Reading(ss.State.UNAVAILABLE, len(series.frame), reason="; ".join(series.detail)),
            flags,
        )
    at = candle_close(series.forming.ts, tf) if series.forming is not None else now
    return ss.evaluate(series.frame, tf, at, params, holidays=holidays), flags


__all__ = [
    "LIVE_TIMEFRAMES",
    "CandleBook",
    "Liquidity",
    "LiveBar",
    "LiveQuote",
    "MoverCoverage",
    "bucket",
    "chart_overlays",
    "dec",
    "expected_volume",
    "live_reading",
    "live_rvol",
    "merge_series",
    "previous_trading_day",
    "quote_status",
    "quote_view",
    "rank_movers",
    "resolve_prev_close",
    "session_day",
    "session_marks",
    "ts_of",
    "volume_profile",
]
