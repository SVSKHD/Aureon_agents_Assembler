"""The EMA 8/32 cross training set, computed from mt5.bars (docs/36-mt5-records.md §dataset).

Pure functions: no database, no clock, no I/O. The API loads the bars and calls `cross_rows`.

Definitions (the same as the bot's labels.py, aureon_mt5_2026 1.7.x):

- EMA 8 / 32 / 200 of the close, `ewm(span, adjust=False)`: the first close seeds each EMA.
  They are recomputed here from the stored closes (the bot's own ema columns are kept on the
  bars for display; recomputing makes the set reproducible from OHLC alone).
- Wilder ATR(14) on CLEANED bars. Bad-tick guard: a lower wick longer than 2.0 x the ATR of the
  bars BEFORE it (below the candle body), or an upper wick longer than 2.5 x that ATR (above the
  body), ends at the body edge. The ATR seeds as the mean of the first 14 true ranges.
- gap = ema8 - ema32. A cross is a sign change of the gap on a closed bar, counted from the bar
  after the first valid ATR. The first trend of the series is not a cross (it was not seen
  starting). side = +1 long, -1 short; g = side * gap; ga = g / ATR.

Columns known AT the cross are features. Columns known only after it carry the `y_` prefix
(outcomes) so nobody trains on them by accident. The last, still-open cross is excluded: its
outcome is not known yet.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

DEFINITIONS_VERSION = "mt5-crosses-1 (labels.py 1.7: EMA 8/32/200, Wilder ATR14, badtick 2.0/2.5)"

EMA_FAST, EMA_SLOW, EMA_TREND, ATR_PERIOD = 8, 32, 200, 14
BADTICK_LOW_ATR, BADTICK_HIGH_ATR = 2.0, 2.5
STEEPNESS_BARS = 3
WIDENING_ATR, WIDENING_RISES = 1.0, 3  # "rising on 4 candles in a row" = 3 rises
WIDE_GAP_ATR = 2.5

FEATURES = (
    "symbol",
    "timeframe",
    "cross_ts",
    "side",
    "price",
    "atr",
    "gap_atr",
    "above_200",
    "with_ema200_side",
    "with_weekly_vwap",
    "hour_utc",
    "weekday",
    "steepness",
    "bars_since_prev_cross",
)
OUTCOMES = (
    "y_bars_to_opposite_cross",
    "y_pts_to_opposite_cross",
    "y_widened",
    "y_reached_wide_gap",
    "y_max_gap_atr",
    "y_max_favourable",
    "y_max_adverse",
)
COLUMNS = FEATURES + OUTCOMES


@dataclass(frozen=True, slots=True)
class Bar:
    ts: datetime  # bar open time, UTC
    open: float
    high: float
    low: float
    close: float
    tick_volume: int | None = None


@dataclass(frozen=True, slots=True)
class Prepared:
    bars: Sequence[Bar]
    clean_high: list[float]
    clean_low: list[float]
    cleaned: list[bool]
    atr: list[float | None]
    ema8: list[float]
    ema32: list[float]
    ema200: list[float]

    @property
    def gap(self) -> list[float]:
        return [f - s for f, s in zip(self.ema8, self.ema32, strict=True)]


def ema(values: Sequence[float], span: int) -> list[float]:
    """pandas `ewm(span=span, adjust=False).mean()`: y0 = x0, y = a*x + (1-a)*y_prev."""
    if not values:
        return []
    a = 2.0 / (span + 1)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(a * float(v) + (1 - a) * out[-1])
    return out


def clean_and_atr(
    bars: Sequence[Bar], period: int = ATR_PERIOD
) -> tuple[list[float], list[float], list[bool], list[float | None]]:
    """Cleaned highs/lows, the cleaned flag and Wilder ATR, exactly as labels.prepare()."""
    n = len(bars)
    ch = [b.high for b in bars]
    cl = [b.low for b in bars]
    cleaned = [False] * n
    atr: list[float | None] = [None] * n
    trs: list[float] = []
    a: float | None = None
    for i, b in enumerate(bars):
        body_lo, body_hi = min(b.open, b.close), max(b.open, b.close)
        ref = atr[i - 1] if i > 0 else None
        if ref is not None:
            if body_lo - b.low > BADTICK_LOW_ATR * ref:
                cl[i], cleaned[i] = body_lo, True
            if b.high - body_hi > BADTICK_HIGH_ATR * ref:
                ch[i], cleaned[i] = body_hi, True
        if i == 0:
            tr = ch[i] - cl[i]
        else:
            pc = bars[i - 1].close
            tr = max(ch[i] - cl[i], abs(ch[i] - pc), abs(cl[i] - pc))
        if a is None:
            trs.append(tr)
            if len(trs) == period:
                a = sum(trs) / period
        else:
            a = (a * (period - 1) + tr) / period  # Wilder smoothing
        atr[i] = a
    return ch, cl, cleaned, atr


def prepare(bars: Sequence[Bar]) -> Prepared:
    ch, cl, cleaned, atr = clean_and_atr(bars)
    closes = [b.close for b in bars]
    return Prepared(
        bars=bars,
        clean_high=ch,
        clean_low=cl,
        cleaned=cleaned,
        atr=atr,
        ema8=ema(closes, EMA_FAST),
        ema32=ema(closes, EMA_SLOW),
        ema200=ema(closes, EMA_TREND),
    )


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def find_crosses(p: Prepared) -> list[tuple[int, int]]:
    """(bar index, side) of every EMA 8/32 cross, as labels.run() sees them."""
    n = len(p.bars)
    valid = [i for i, a in enumerate(p.atr) if a is not None]
    if not valid:
        return []
    first = max(1, valid[0] + 1)
    gap = p.gap
    side = 0
    out: list[tuple[int, int]] = []
    for i in range(first, n):
        a = p.atr[i]
        if a is None or a <= 0:
            continue
        s = _sign(gap[i])
        if s != 0 and s != side:
            if side != 0:
                out.append((i, s))
            side = s
    return out


def week_start(ts: datetime) -> datetime:
    """Monday 00:00 UTC of the week holding `ts`."""
    day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=ts.weekday())


def weekly_vwap(p: Prepared) -> list[float | None]:
    """Running VWAP of the typical price (cleaned high + cleaned low + close) / 3, weighted by
    tick_volume, reset every Monday 00:00 UTC; up to and including each bar (known at its
    close). None while the week has no volume."""
    out: list[float | None] = []
    wk: datetime | None = None
    pv = vol = 0.0
    for i, b in enumerate(p.bars):
        w = week_start(b.ts)
        if w != wk:
            wk, pv, vol = w, 0.0, 0.0
        v = float(b.tick_volume or 0)
        pv += v * (p.clean_high[i] + p.clean_low[i] + b.close) / 3.0
        vol += v
        out.append(pv / vol if vol > 0 else None)
    return out


def _r(x: float | None, nd: int = 6) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return round(x, nd)


def _rising(g: Sequence[float], i: int, n: int) -> bool:
    return i >= n and all(g[i - k] > g[i - k - 1] for k in range(n))


def cross_rows(
    bars: Sequence[Bar],
    symbol: str,
    timeframe: str,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    *,
    include_open: bool = False,
) -> list[dict[str, Any]]:
    """One row per COMPLETED cross with cross_ts in [date_from, date_to]. `bars` are sorted by
    ts and may start before date_from (warm-up for the EMAs and the ATR); the caller loads no
    bar after date_to, so a cross whose opposite cross comes later is still open: excluded.

    `include_open` (diagnostics and tests only; the API never sets it) also returns the open
    cross, with its outcomes so far (up to the last bar) and no opposite-cross columns."""
    p = prepare(bars)
    crosses = find_crosses(p)
    gap = p.gap
    vwap = weekly_vwap(p)
    rows: list[dict[str, Any]] = []
    last = len(p.bars) - 1
    for k, (i, side) in enumerate(crosses):
        is_open = k + 1 >= len(crosses)
        if is_open and not include_open:
            break  # the last cross is still open
        b = p.bars[i]
        if date_from is not None and b.ts < date_from:
            continue
        if date_to is not None and b.ts > date_to:
            continue
        nxt = last if is_open else crosses[k + 1][0]
        end = last + 1 if is_open else nxt  # ga is looked at up to (not on) the opposite cross
        atr = p.atr[i]
        assert atr is not None and atr > 0  # find_crosses only emits bars with a valid ATR
        g = [side * x for x in gap]
        above = b.close > p.ema200[i]
        vw = vwap[i]
        # outcomes: the bars after the cross, up to the opposite cross
        gas = [g[j] / a for j in range(i + 1, end) if (a := p.atr[j]) is not None and a > 0]
        widened = any(
            g[j] / a >= WIDENING_ATR and _rising(g, j, WIDENING_RISES)
            for j in range(i + 1, end)
            if (a := p.atr[j]) is not None and a > 0
        )
        after = range(i + 1, nxt + 1) if nxt > i else range(i, i + 1)
        if side > 0:
            fav = max(p.clean_high[j] for j in after) - b.close
            adv = b.close - min(p.clean_low[j] for j in after)
        else:
            fav = b.close - min(p.clean_low[j] for j in after)
            adv = max(p.clean_high[j] for j in after) - b.close
        rows.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "cross_ts": b.ts.isoformat(),
                "side": side,
                "price": _r(b.close),
                "atr": _r(atr),
                "gap_atr": _r(g[i] / atr),
                "above_200": above,
                "with_ema200_side": above if side > 0 else b.close < p.ema200[i],
                "with_weekly_vwap": None if vw is None else (side * (b.close - vw) > 0),
                "hour_utc": b.ts.hour,
                "weekday": b.ts.weekday(),
                "steepness": (
                    _r((g[i] - g[i - STEEPNESS_BARS]) / atr) if i >= STEEPNESS_BARS else None
                ),
                "bars_since_prev_cross": i - crosses[k - 1][0] if k > 0 else None,
                "y_bars_to_opposite_cross": None if is_open else nxt - i,
                "y_pts_to_opposite_cross": (
                    None if is_open else _r(side * (p.bars[nxt].close - b.close))
                ),
                "y_widened": widened,
                "y_reached_wide_gap": bool(gas) and max(gas) >= WIDE_GAP_ATR,
                "y_max_gap_atr": _r(max(gas)) if gas else None,
                "y_max_favourable": _r(max(fav, 0.0)),
                "y_max_adverse": _r(max(adv, 0.0)),
            }
        )
    return rows


# --- output formats ---------------------------------------------------------------------------


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def to_csv(rows: Iterable[dict[str, Any]], columns: Sequence[str] = COLUMNS) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in columns])
    return buf.getvalue()


def to_jsonl(rows: Iterable[dict[str, Any]], columns: Sequence[str] = COLUMNS) -> str:
    return "".join(
        json.dumps({c: r.get(c) for c in columns}, separators=(",", ":")) + "\n" for r in rows
    )


__all__ = [
    "COLUMNS",
    "DEFINITIONS_VERSION",
    "FEATURES",
    "OUTCOMES",
    "Bar",
    "Prepared",
    "clean_and_atr",
    "cross_rows",
    "ema",
    "find_crosses",
    "prepare",
    "to_csv",
    "to_jsonl",
    "week_start",
    "weekly_vwap",
]
