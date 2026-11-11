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
- SMA(n): mean of the last n values; NaN until n values exist.
- RSI(n): Wilder. Gains/losses start at index 1 (each needs the previous close); the average
  gain/loss is seeded with the mean of the first n of them (so the first RSI is at index n),
  then avg = (prev * (n - 1) + x) / n. A zero average loss gives 100 (50 when both are zero).
- ADX(n): Wilder +DM/-DM and TR from index 1, smoothed the same mean-seeded way; +DI/-DI and DX
  from index n; ADX = mean of the first n DX at index 2n - 1, then Wilder.
- Bollinger(n, k): SMA(n) +/- k x population standard deviation (ddof 0).
These four follow ASTRA-FRONTEND src/trading/indicators.ts formula for formula, so a study
the chart draws and the value the screener filters on agree.
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


def _wilder(x: np.ndarray, n: int, start: int) -> np.ndarray:
    """Wilder smoothing seeded with the mean of x[start : start + n]."""
    out = np.full(x.shape, np.nan)
    seed_end = start + n - 1
    if n < 1 or seed_end >= len(x):
        return out
    prev = float(x[start : seed_end + 1].mean())
    out[seed_end] = prev
    for i in range(seed_end + 1, len(x)):
        prev = (prev * (n - 1) + x[i]) / n
        out[i] = prev
    return out


def sma(values: pd.Series, n: int) -> pd.Series:
    if n < 1:
        raise ValueError("period must be >= 1")
    return values.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    if n < 1:
        raise ValueError("period must be >= 1")
    c = close.to_numpy(dtype=float)
    diff = np.diff(c, prepend=c[0]) if len(c) else c
    gains = np.where(diff > 0, diff, 0.0)
    losses = np.where(diff < 0, -diff, 0.0)
    g = _wilder(gains, n, 1)
    lo = _wilder(losses, n, 1)
    out = np.full(c.shape, np.nan)
    for i in range(len(c)):
        if np.isnan(g[i]) or np.isnan(lo[i]):
            continue
        if lo[i] == 0:
            out[i] = 50.0 if g[i] == 0 else 100.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + g[i] / lo[i])
    return pd.Series(out, index=close.index)


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """ADX with +DI / -DI (Wilder)."""
    if n < 1:
        raise ValueError("period must be >= 1")
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    size = len(df)
    pdm = np.full(size, np.nan)
    mdm = np.full(size, np.nan)
    for i in range(1, size):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        pdm[i] = up if up > down and up > 0 else 0.0
        mdm[i] = down if down > up and down > 0 else 0.0
    tr = true_range(df).to_numpy(dtype=float)
    s_tr, s_p, s_m = _wilder(tr, n, 1), _wilder(pdm, n, 1), _wilder(mdm, n, 1)
    plus = np.full(size, np.nan)
    minus = np.full(size, np.nan)
    dx = np.full(size, np.nan)
    for i in range(n, size):
        if np.isnan(s_tr[i]) or s_tr[i] == 0:
            continue
        plus[i] = 100 * s_p[i] / s_tr[i]
        minus[i] = 100 * s_m[i] / s_tr[i]
        total = plus[i] + minus[i]
        dx[i] = 0.0 if total == 0 else 100 * abs(plus[i] - minus[i]) / total
    out = np.full(size, np.nan)
    seed_end = 2 * n - 1
    if seed_end < size and not np.isnan(dx[n : seed_end + 1]).any():
        prev = float(dx[n : seed_end + 1].mean())
        out[seed_end] = prev
        for i in range(seed_end + 1, size):
            if not np.isnan(dx[i]):
                prev = (prev * (n - 1) + dx[i]) / n
            out[i] = prev
    return pd.DataFrame({"adx": out, "plus_di": plus, "minus_di": minus}, index=df.index)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return pd.DataFrame({"middle": mid, "upper": mid + k * sd, "lower": mid - k * sd})


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


# --- on-demand studies (GET /api/trading/indicators/compute and the screener) -------------

MAX_PERIOD = 500
SPEC_KINDS = ("ema", "sma", "rsi", "adx", "atr", "macd", "bb", "vwap")


class SpecError(ValueError):
    """An indicator spec the server cannot compute (bad name or parameters)."""


@dataclass(frozen=True, slots=True)
class Spec:
    kind: str
    params: tuple[float, ...]

    @property
    def name(self) -> str:
        if not self.params:
            return self.kind
        return ":".join([self.kind, *(_fmt_param(p) for p in self.params)])

    @property
    def warmup(self) -> int:
        """Bars needed before the first defined value."""
        p = [int(x) for x in self.params]
        if self.kind in ("ema", "sma", "atr", "bb"):
            return p[0]
        if self.kind == "rsi":
            return p[0] + 1
        if self.kind == "adx":
            return 2 * p[0]
        if self.kind == "macd":
            return p[1] + p[2] - 1
        return 1


def _fmt_param(p: float) -> str:
    return str(int(p)) if float(p).is_integer() else format(p, "g")


def _period(v: str) -> int:
    try:
        n = int(v)
    except ValueError as exc:
        raise SpecError(f"period {v!r} is not a whole number") from exc
    if not 1 <= n <= MAX_PERIOD:
        raise SpecError(f"period {n} must be 1..{MAX_PERIOD}")
    return n


def parse_spec(text: str) -> Spec:
    """ "ema:8", "rsi:14", "adx:14", "atr:14", "sma:50", "macd:12:26:9", "bb:20:2", "vwap"."""
    parts = text.strip().lower().split(":")
    kind, args = parts[0], parts[1:]
    if kind not in SPEC_KINDS:
        raise SpecError(f"unknown indicator {kind!r} (one of {', '.join(SPEC_KINDS)})")
    if kind == "vwap":
        if args:
            raise SpecError("vwap takes no parameters")
        return Spec("vwap", ())
    if kind == "macd":
        if len(args) != 3:
            raise SpecError("macd needs fast:slow:signal, e.g. macd:12:26:9")
        f, s, g = (_period(a) for a in args)
        if f >= s:
            raise SpecError("macd fast period must be shorter than slow")
        return Spec("macd", (f, s, g))
    if kind == "bb":
        if len(args) != 2:
            raise SpecError("bb needs period:multiplier, e.g. bb:20:2")
        try:
            k = float(args[1])
        except ValueError as exc:
            raise SpecError("bb multiplier must be a number") from exc
        if not 0 < k <= 10:
            raise SpecError("bb multiplier must be in (0, 10]")
        return Spec("bb", (_period(args[0]), k))
    if len(args) != 1:
        raise SpecError(f"{kind} needs one period, e.g. {kind}:14")
    return Spec(kind, (_period(args[0]),))


def compute_spec(df: pd.DataFrame, spec: Spec, vwap_tz: str) -> pd.DataFrame:
    """One spec → value columns aligned to df.index (value i uses rows 0..i only)."""
    p = spec.params
    if spec.kind == "ema":
        return pd.DataFrame({"value": ema(df["close"], int(p[0]))})
    if spec.kind == "sma":
        return pd.DataFrame({"value": sma(df["close"], int(p[0]))})
    if spec.kind == "rsi":
        return pd.DataFrame({"value": rsi(df["close"], int(p[0]))})
    if spec.kind == "atr":
        return pd.DataFrame({"value": atr(df, int(p[0]))})
    if spec.kind == "adx":
        return adx(df, int(p[0]))
    if spec.kind == "macd":
        return macd(df["close"], int(p[0]), int(p[1]), int(p[2]))
    if spec.kind == "bb":
        return bollinger(df["close"], int(p[0]), float(p[1]))
    return pd.DataFrame({"value": vwap(df, vwap_tz)})
