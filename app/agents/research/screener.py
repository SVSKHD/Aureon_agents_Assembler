"""Screener evaluation: one instrument's candles + the filters → an outcome (pure, no I/O).

Rules (docs/35-market-data.md "Screener"):
- **Confirmed vs live.** In `confirmed` mode a candle whose period has not ended by `now`
  (ts + timeframe > now) is dropped before anything is computed, so a still-forming bar can
  never trigger or cancel a match. In `live` mode it is kept and evaluated, and the result says
  `candle_closed=False`.
- **No lookahead.** Every indicator value at bar i uses rows 0..i only
  (app/agents/research/indicators.py); the filters read the evaluated (last) bar and, for
  crossovers, earlier bars only.
- **Crossovers.** EMA fast crosses above slow at bar i when diff = fast - slow is > 0 at i and
  the last non-zero diff before i was < 0 (a touch at exactly 0 that turns back is not a
  cross). `within_bars = N` looks at the last N bars and also requires the crossed side to
  still hold on the evaluated bar. `crossed_at` is the open time of the crossing bar.
- **Volume.** The evaluated bar's volume against the mean of the `avg_period` bars *before* it.
  Instruments that report no volume (indices) are `insufficient_data`, never a match.
- **change_pct** is against the previous trading day's last close found in the loaded bars
  (the previous bar for 1d); None when the loaded window does not reach the previous day.
- Missing values are never filled: too few bars (or an undefined value) is
  `insufficient_data`, no bars is `no_data`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.agents.research import indicators as ind
from app.schemas.market import (
    AdxFilter,
    EmaCrossFilter,
    EmaPositionFilter,
    MacdCrossFilter,
    PriceVsEmaFilter,
    RsiFilter,
    VolumeFilter,
    VwapPositionFilter,
)

Outcome = Literal["match", "no_match", "no_data", "insufficient_data"]
Filter = (
    EmaCrossFilter
    | EmaPositionFilter
    | PriceVsEmaFilter
    | RsiFilter
    | MacdCrossFilter
    | VolumeFilter
    | AdxFilter
    | VwapPositionFilter
)


@dataclass(slots=True)
class Evaluation:
    outcome: Outcome
    candle_ts: datetime | None = None
    candle_closed: bool = True
    last: Decimal | None = None
    change_pct: Decimal | None = None
    indicators: dict[str, str | None] = field(default_factory=dict)
    signal: str | None = None
    crossed_at: datetime | None = None
    detail: str | None = None

    def metrics(self) -> dict[str, Any]:
        """JSON for trading.screener_results.metrics (Decimal strings, ISO times)."""
        return {
            "last": _s(self.last),
            "change_pct": _s(self.change_pct),
            "indicators": self.indicators,
            "signal": self.signal,
            "crossed_at": self.crossed_at.isoformat() if self.crossed_at else None,
            "candle_closed": self.candle_closed,
        }


def _s(d: Decimal | None) -> str | None:
    return None if d is None else format(d, "f")


def bars_needed(f: Filter) -> int:
    """The fewest bars for which every value the filter reads is defined."""
    if isinstance(f, EmaCrossFilter):
        return f.slow + f.within_bars
    if isinstance(f, EmaPositionFilter):
        return f.slow
    if isinstance(f, PriceVsEmaFilter):
        return f.period
    if isinstance(f, RsiFilter):
        return f.period + 1
    if isinstance(f, MacdCrossFilter):
        return f.slow + f.signal - 1 + f.within_bars
    if isinstance(f, VolumeFilter):
        return f.avg_period + 1
    if isinstance(f, AdxFilter):
        return 2 * f.period
    return 1


def lookback_needed(filters: Sequence[Filter]) -> int:
    return max(bars_needed(f) for f in filters)


def drop_forming(rows: Sequence[Any], step: timedelta, now: datetime) -> list[Any]:
    """Only candles whose period has ended (ts + step <= now)."""
    return [r for r in rows if r.ts + step <= now]


def _carried_sign(diff: np.ndarray) -> np.ndarray:
    """sign(diff) with zeros replaced by the last non-zero sign (NaN breaks the chain)."""
    out = np.zeros(len(diff))
    prev = 0.0
    for i, d in enumerate(diff):
        if np.isnan(d):
            prev = 0.0
            out[i] = np.nan
            continue
        if d > 0:
            prev = 1.0
        elif d < 0:
            prev = -1.0
        out[i] = prev
    return out


def find_cross(diff: np.ndarray, within: int, direction: str) -> int | None:
    """Index of the most recent `direction` cross of zero in the last `within` bars, provided
    the crossed side still holds on the last bar; None otherwise."""
    n = len(diff)
    if n < 2 or np.isnan(diff[-1]):
        return None
    want = 1.0 if direction == "bullish" else -1.0
    if np.sign(diff[-1]) != want:
        return None
    carried = _carried_sign(diff)
    for i in range(n - 1, max(n - within, 1) - 1, -1):
        if np.isnan(diff[i]) or np.isnan(carried[i - 1]):
            continue
        if np.sign(diff[i]) == want and carried[i - 1] == -want:
            return i
    return None


def _cmp(a: float, op: str, b: float) -> bool:
    return a < b if op == "<" else a > b


def _rel(a: float, b: float, relation: str) -> bool:
    return a > b if relation == "above" else a < b


def _change_pct(df: pd.DataFrame, daily: bool, day_tz: tzinfo) -> Decimal | None:
    close = df["close"].to_numpy(dtype=float)
    if daily:
        prev = close[-2] if len(close) >= 2 else None
    else:
        days = pd.DatetimeIndex(df.index).tz_convert(day_tz).date
        earlier = np.flatnonzero(days < days[-1])
        prev = close[earlier[-1]] if len(earlier) else None
    if prev is None or prev == 0 or not np.isfinite(prev):
        return None
    pct = (close[-1] - prev) / prev * 100.0
    return Decimal(repr(float(pct))).quantize(Decimal("0.0001"))


def evaluate(
    rows: Sequence[Any],
    filters: Sequence[Filter],
    *,
    step: timedelta,
    mode: Literal["confirmed", "live"],
    now: datetime,
    day_tz: tzinfo,
    vwap_tz: str,
) -> Evaluation:
    """`rows`: ascending candles (ts/open/high/low/close/volume, Decimal), gap fillers excluded."""
    usable = drop_forming(rows, step, now) if mode == "confirmed" else list(rows)
    if not usable:
        return Evaluation("no_data", detail="no candles for this timeframe")
    last_row = usable[-1]
    closed = last_row.ts + step <= now
    df = ind.frame(usable)
    need = lookback_needed(filters)
    base = Evaluation(
        "no_match",
        candle_ts=last_row.ts,
        candle_closed=closed,
        last=last_row.close,
        change_pct=_change_pct(df, step >= timedelta(days=1), day_tz),
    )
    if len(df) < need:
        base.outcome = "insufficient_data"
        base.detail = f"{len(df)} bars, need {need}"
        return base

    out = base.indicators
    close = df["close"].to_numpy(dtype=float)
    cache: dict[str, pd.DataFrame] = {}

    def study(spec: str) -> pd.DataFrame:
        if spec not in cache:
            cache[spec] = ind.compute_spec(df, ind.parse_spec(spec), vwap_tz)
        return cache[spec]

    def col(spec: str, column: str = "value") -> np.ndarray:
        return study(spec)[column].to_numpy(dtype=float)

    def put(name: str, v: float) -> None:
        out[name] = ind.to_dec(float(v))

    results: list[bool] = []
    undefined: list[str] = []
    for f in filters:
        ok = False
        if isinstance(f, EmaCrossFilter | EmaPositionFilter):
            fast, slow = col(f"ema:{f.fast}"), col(f"ema:{f.slow}")
            put(f"ema:{f.fast}", fast[-1])
            put(f"ema:{f.slow}", slow[-1])
            if np.isnan(fast[-1]) or np.isnan(slow[-1]):
                undefined.append(f.type)
            elif isinstance(f, EmaCrossFilter):
                i = find_cross(fast - slow, f.within_bars, f.direction)
                ok = i is not None
                if ok and base.signal is None:
                    assert i is not None
                    base.signal = f"{f.direction}_cross"
                    base.crossed_at = usable[i].ts
            else:
                ok = _rel(fast[-1], slow[-1], f.relation)
        elif isinstance(f, PriceVsEmaFilter):
            e = col(f"ema:{f.period}")
            put(f"ema:{f.period}", e[-1])
            if np.isnan(e[-1]):
                undefined.append(f.type)
            else:
                ok = _rel(close[-1], e[-1], f.relation)
        elif isinstance(f, RsiFilter):
            r = col(f"rsi:{f.period}")
            put(f"rsi:{f.period}", r[-1])
            if np.isnan(r[-1]):
                undefined.append(f.type)
            else:
                ok = _cmp(r[-1], f.op, f.value)
        elif isinstance(f, MacdCrossFilter):
            spec = f"macd:{f.fast}:{f.slow}:{f.signal}"
            line, sig = col(spec, "macd"), col(spec, "signal")
            put(spec, line[-1])
            put(f"macd_signal:{f.fast}:{f.slow}:{f.signal}", sig[-1])
            put(f"macd_hist:{f.fast}:{f.slow}:{f.signal}", line[-1] - sig[-1])
            if np.isnan(line[-1]) or np.isnan(sig[-1]):
                undefined.append(f.type)
            else:
                i = find_cross(line - sig, f.within_bars, f.direction)
                ok = i is not None
                if ok and base.signal is None:
                    assert i is not None
                    base.signal = f"{f.direction}_cross"
                    base.crossed_at = usable[i].ts
        elif isinstance(f, VolumeFilter):
            vol = df["volume"].to_numpy(dtype=float)
            avg = float(vol[-f.avg_period - 1 : -1].mean())
            put("volume", vol[-1])
            put(f"volume_avg:{f.avg_period}", avg)
            if avg <= 0:
                undefined.append("volume (instrument reports no volume)")
            else:
                ok = _cmp(vol[-1], f.op, f.multiple_of_avg * avg)
        elif isinstance(f, AdxFilter):
            a = col(f"adx:{f.period}", "adx")
            put(f"adx:{f.period}", a[-1])
            put(f"plus_di:{f.period}", col(f"adx:{f.period}", "plus_di")[-1])
            put(f"minus_di:{f.period}", col(f"adx:{f.period}", "minus_di")[-1])
            if np.isnan(a[-1]):
                undefined.append(f.type)
            else:
                ok = _cmp(a[-1], f.op, f.value)
        else:  # VwapPositionFilter
            v = col("vwap")
            put("vwap", v[-1])
            if np.isnan(v[-1]):
                undefined.append("vwap (instrument reports no volume)")
            else:
                ok = _rel(close[-1], v[-1], f.relation)
        results.append(ok)

    if undefined:
        base.outcome = "insufficient_data"
        base.detail = ("undefined: " + ", ".join(undefined))[:200]
        return base
    if base.signal is None:
        for f in filters:
            if isinstance(f, EmaPositionFilter | PriceVsEmaFilter | VwapPositionFilter):
                base.signal = f.relation
                break
    base.outcome = "match" if all(results) else "no_match"
    return base
