"""Deterministic technical indicators on closed candles (pandas/numpy).

Every function takes a candle DataFrame indexed by UTC open time with float columns
open/high/low/close/volume, and returns values aligned to that index. Value `i` uses only
rows `0..i`, so nothing ever looks ahead. Prices are converted to float only for this
analysis math; money (P/L, fees) is always computed in Decimal (see backtest.py), and
stored indicator values are Decimal strings rounded to 8 places.

Conventions (matching common charting packages):
- EMA(n): seeded with the simple mean of the first n closes, then
  ema = prev + (close - prev) * 2 / (n + 1).
- MACD(f, s, sig): EMA(f) - EMA(s); signal = EMA(sig) of MACD (same seeding); hist = MACD - signal.
- ATR(n): Wilder's smoothing of the true range, seeded with the mean of the first n TRs.
- VWAP: typical price (H+L+C)/3 weighted by volume, reset at each session day in `anchor_tz`.
- Structure: pivot highs/lows over `k` bars each side (strict on the left, ties allowed on
  the right). A pivot at bar j is only *known* at
  bar j+k, so it is reported from then on (no lookahead).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

Q = Decimal("0.00000001")


def frame(rows: Sequence[Any]) -> pd.DataFrame:
    """Candle rows (objects with ts/open/high/low/close/volume as Decimal) → float frame."""
    df = pd.DataFrame(
        {
            "open": [float(r.open) for r in rows],
            "high": [float(r.high) for r in rows],
            "low": [float(r.low) for r in rows],
            "close": [float(r.close) for r in rows],
            "volume": [float(r.volume) for r in rows],
        },
        index=pd.DatetimeIndex([r.ts for r in rows], name="ts"),
    )
    return df


def _seeded_ema(values: np.ndarray, n: int) -> np.ndarray:
    out = np.full(values.shape, np.nan)
    if n < 1:
        raise ValueError("period must be >= 1")
    valid = np.flatnonzero(~np.isnan(values))
    if len(valid) < n:
        return out
    start = valid[0]
    seed_end = start + n - 1
    window = values[start : seed_end + 1]
    if np.isnan(window).any():
        return out
    alpha = 2.0 / (n + 1)
    prev = float(window.mean())
    out[seed_end] = prev
    for i in range(seed_end + 1, len(values)):
        v = values[i]
        if np.isnan(v):
            out[i] = prev
            continue
        prev = prev + (v - prev) * alpha
        out[i] = prev
    return out


def ema(close: pd.Series, n: int) -> pd.Series:
    return pd.Series(_seeded_ema(close.to_numpy(dtype=float), n), index=close.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    if fast >= slow:
        raise ValueError("MACD fast period must be shorter than slow")
    line = ema(close, fast) - ema(close, slow)
    sig = pd.Series(_seeded_ema(line.to_numpy(dtype=float), signal), index=close.index)
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1, skipna=True)
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    tr = true_range(df).to_numpy(dtype=float)
    out = np.full(tr.shape, np.nan)
    if len(tr) < n:
        return pd.Series(out, index=df.index)
    prev = float(tr[:n].mean())
    out[n - 1] = prev
    for i in range(n, len(tr)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return pd.Series(out, index=df.index)


def vwap(df: pd.DataFrame, anchor_tz: str = "UTC") -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    idx = pd.DatetimeIndex(df.index)
    day = idx.tz_convert(ZoneInfo(anchor_tz)).date
    pv = (tp * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum()
    out = pv / vol.replace(0.0, np.nan)
    return pd.Series(out.to_numpy(dtype=float), index=df.index)


def structure(df: pd.DataFrame, k: int = 3) -> pd.DataFrame:
    """Last confirmed swing high/low and a trend label at every bar.

    trend: "up" (higher high and higher low), "down" (lower high and lower low), else "range".
    """
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    n = len(df)
    swing_high = np.full(n, np.nan)
    swing_low = np.full(n, np.nan)
    trend: list[str | None] = [None] * n
    highs: list[float] = []
    lows: list[float] = []
    for i in range(n):
        j = i - k  # the candidate pivot that becomes known at bar i
        if j >= k:
            # Pivot: strictly above (below) the k bars before it and not exceeded by the k
            # bars after it, so a flat top counts once (on its first bar).
            if high[j] > high[j - k : j].max() and high[j] >= high[j + 1 : j + k + 1].max():
                highs.append(float(high[j]))
            if low[j] < low[j - k : j].min() and low[j] <= low[j + 1 : j + k + 1].min():
                lows.append(float(low[j]))
        if highs:
            swing_high[i] = highs[-1]
        if lows:
            swing_low[i] = lows[-1]
        if len(highs) >= 2 and len(lows) >= 2:
            hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
            lh, ll = highs[-1] < highs[-2], lows[-1] < lows[-2]
            trend[i] = "up" if hh and hl else "down" if lh and ll else "range"
    return pd.DataFrame(
        {
            "swing_high": swing_high,
            "swing_low": swing_low,
            "trend": pd.Series(trend, index=df.index, dtype=object),
        },
        index=df.index,
    )


# --- the configured set -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IndicatorSet:
    ema_periods: tuple[int, ...] = (9, 21)
    macd: tuple[int, int, int] = (12, 26, 9)
    atr_period: int = 14
    vwap_tz: str = "UTC"
    structure_k: int = 3


def to_dec(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float | np.floating):
        if not np.isfinite(v):
            return None
        return format(Decimal(repr(float(v))).quantize(Q).normalize(), "f")
    return str(v)


def compute(df: pd.DataFrame, spec: IndicatorSet) -> dict[str, pd.DataFrame]:
    """name → frame of value columns, aligned to df.index."""
    out: dict[str, pd.DataFrame] = {}
    for p in sorted(set(spec.ema_periods)):
        out[f"ema:{p}"] = pd.DataFrame({"value": ema(df["close"], p)})
    f, s, g = spec.macd
    out[f"macd:{f}:{s}:{g}"] = macd(df["close"], f, s, g)
    out[f"atr:{spec.atr_period}"] = pd.DataFrame({"value": atr(df, spec.atr_period)})
    out[f"vwap:{spec.vwap_tz}"] = pd.DataFrame({"value": vwap(df, spec.vwap_tz)})
    out[f"structure:{spec.structure_k}"] = structure(df, spec.structure_k)
    return out


def rows_for_storage(results: dict[str, pd.DataFrame]) -> list[tuple[str, Any, dict[str, Any]]]:
    """(name, ts, values) for every bar where at least one value is defined."""
    rows = []
    for name, fr in results.items():
        for ts, rec in zip(fr.index, fr.to_dict("records"), strict=True):
            values: dict[str, Any] = {str(k): to_dec(v) for k, v in rec.items()}
            if any(v is not None for v in values.values()):
                rows.append((name, ts.to_pydatetime(), values))
    return rows
