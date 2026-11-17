"""Stock Signals rankings (docs/37 §3): market movers and a 0-100 trend-strength score.

Pure and deterministic, on CLOSED candles only (the caller passes the frame the signal engine
used). The score ranks; it is NOT a probability of profit and is never presented as one.

Features (each scaled to -1 … +1, positive = bullish; documented in docs/37):

    ema        EMA fast vs slow separation, % of the slow EMA, ÷ 2 %
    vwap       close vs session VWAP, %, ÷ 1.5 %
    volume     relative volume (today so far vs the same bars of earlier sessions), as
               confirmation: (rvol - 1) ÷ 2, clipped to 0 … 1, signed by the EMA direction
    rsi        (RSI 14 - 50) ÷ 25
    adx        trend strength (ADX 14 - 15) ÷ 25, clipped 0 … 1, signed by +DI vs -DI
    momentum   change over the last 8 bars, %, ÷ 3 %
    relative   today's change minus Nifty 50's today, %, ÷ 2 %

score = 50 + 50 * sum(w_i * f_i) / sum(w_i), over the features that could be computed (a
missing VWAP, volume history or Nifty series drops that feature, listed in `missing`). Weights come
from STOCK_SIGNALS_SCORE_WEIGHTS. Gainers and losers use `change_pct` (the real price change
from the previous session's last close), never the score.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from app.agents.research import indicators as ind

DEFAULT_WEIGHTS: dict[str, float] = {
    "ema": 0.25,
    "vwap": 0.20,
    "volume": 0.10,
    "rsi": 0.10,
    "adx": 0.15,
    "momentum": 0.10,
    "relative": 0.10,
}
MOMENTUM_BARS = 8
TZ = "Asia/Kolkata"


@dataclass(frozen=True, slots=True)
class Features:
    change_pct: float | None = None  # vs the previous session's last close
    rvol: float | None = None
    turnover: float | None = None  # mean close * volume per bar over the frame (INR)
    rsi: float | None = None
    adx: float | None = None
    momentum_pct: float | None = None
    relative_pct: float | None = None  # change_pct - Nifty 50 change_pct
    components: dict[str, float] = field(default_factory=dict)
    missing: tuple[str, ...] = ()
    score: float | None = None

    def as_json(self) -> dict[str, Any]:
        def s(x: float | None, q: str = "0.0001") -> str | None:
            if x is None or not np.isfinite(x):
                return None
            return format(Decimal(repr(float(x))).quantize(Decimal(q)), "f")

        return {
            "change_pct": s(self.change_pct),
            "rvol": s(self.rvol),
            "turnover": s(self.turnover, "1"),
            "rsi": s(self.rsi),
            "adx": s(self.adx),
            "momentum_pct": s(self.momentum_pct),
            "relative_pct": s(self.relative_pct),
            "components": {k: s(v) for k, v in self.components.items()},
            "missing": list(self.missing),
        }


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return float(min(hi, max(lo, x)))


def _days(df: pd.DataFrame) -> np.ndarray:
    return np.asarray(pd.DatetimeIndex(df.index).tz_convert(TZ).date)


def change_pct(df: pd.DataFrame) -> float | None:
    """Last close vs the previous IST session's last close, in %."""
    if df.empty:
        return None
    days = _days(df)
    earlier = np.flatnonzero(days < days[-1])
    if not len(earlier):
        return None
    prev = float(df["close"].iloc[earlier[-1]])
    last = float(df["close"].iloc[-1])
    if prev <= 0:
        return None
    return (last - prev) / prev * 100.0


def relative_volume(df: pd.DataFrame) -> float | None:
    """Today's cumulative volume vs the average cumulative volume of earlier sessions at the
    same bar of the session (time-of-day adjusted). None without earlier sessions or volume."""
    if df.empty:
        return None
    days = _days(df)
    vol = df["volume"].to_numpy(dtype=float)
    today = days[-1]
    k = int(np.sum(days == today))  # bars so far today
    today_vol = float(vol[days == today].sum())
    past = []
    for d in dict.fromkeys(days[days < today]):
        v = vol[days == d]
        if len(v) >= k:
            past.append(float(v[:k].sum()))
    base = float(np.mean(past)) if past else 0.0
    if base <= 0:
        return None
    return today_vol / base


def features(
    df: pd.DataFrame,
    *,
    ema_fast: float | None,
    ema_slow: float | None,
    vwap: float | None,
    nifty: pd.DataFrame | None = None,
    weights: Mapping[str, float] = DEFAULT_WEIGHTS,
) -> Features:
    """The ranking features and the score for the newest bar of `df` (closed candles)."""
    if df.empty:
        return Features(missing=tuple(weights))
    close = df["close"]
    c = float(close.iloc[-1])
    chg = change_pct(df)
    rvol = relative_volume(df)
    turnover = float((df["close"] * df["volume"]).mean())
    rsi = float(ind.rsi(close, 14).iloc[-1]) if len(df) > 15 else float("nan")
    adx_df = ind.adx(df, 14) if len(df) > 28 else None
    adx = float(adx_df["adx"].iloc[-1]) if adx_df is not None else float("nan")
    mom = (
        (c - float(close.iloc[-1 - MOMENTUM_BARS])) / float(close.iloc[-1 - MOMENTUM_BARS]) * 100
        if len(df) > MOMENTUM_BARS and float(close.iloc[-1 - MOMENTUM_BARS]) > 0
        else None
    )
    nifty_chg = change_pct(nifty) if nifty is not None else None
    rel = chg - nifty_chg if chg is not None and nifty_chg is not None else None

    comp: dict[str, float] = {}
    direction = 0.0
    if ema_fast is not None and ema_slow is not None and ema_slow > 0:
        spread = (ema_fast - ema_slow) / ema_slow * 100
        comp["ema"] = _clip(spread / 2.0)
        direction = float(np.sign(spread))
    if vwap is not None and vwap > 0:
        comp["vwap"] = _clip((c - vwap) / vwap * 100 / 1.5)
    if rvol is not None and direction:
        comp["volume"] = _clip((rvol - 1) / 2, 0.0, 1.0) * direction
    if np.isfinite(rsi):
        comp["rsi"] = _clip((rsi - 50) / 25)
    if adx_df is not None and np.isfinite(adx):
        di = float(adx_df["plus_di"].iloc[-1] - adx_df["minus_di"].iloc[-1])
        comp["adx"] = _clip((adx - 15) / 25, 0.0, 1.0) * float(np.sign(di))
    if mom is not None:
        comp["momentum"] = _clip(mom / 3.0)
    if rel is not None:
        comp["relative"] = _clip(rel / 2.0)

    used = {k: w for k, w in weights.items() if k in comp and w > 0}
    total = sum(used.values())
    score = 50 + 50 * sum(w * comp[k] for k, w in used.items()) / total if total else None
    return Features(
        change_pct=chg,
        rvol=rvol,
        turnover=turnover,
        rsi=rsi if np.isfinite(rsi) else None,
        adx=adx if np.isfinite(adx) else None,
        momentum_pct=mom,
        relative_pct=rel,
        components=comp,
        missing=tuple(k for k in weights if k not in comp),
        score=round(score, 2) if score is not None else None,
    )


# --- validation: does the score order forward returns? -----------------------------------------


@dataclass(frozen=True, slots=True)
class Bucket:
    lo: float
    hi: float
    n: int
    mean_forward_pct: float
    hit_rate: float  # share of observations with a positive forward return


def score_series(
    df: pd.DataFrame,
    fast: int,
    slow: int,
    weights: Mapping[str, float],
    *,
    start: int,
    intraday: bool = True,
) -> list[tuple[int, float]]:
    """(bar index, score) for every bar from `start`, each computed from bars 0..i only.
    Daily bars have no session VWAP of their own: `intraday=False` leaves that feature out."""
    out = []
    for i in range(start, len(df)):
        window = df.iloc[: i + 1]
        close = window["close"]
        ef = float(ind.ema(close, fast).iloc[-1])
        es = float(ind.ema(close, slow).iloc[-1])
        vw = float(ind.vwap(window, TZ).iloc[-1]) if intraday else float("nan")
        f = features(
            window,
            ema_fast=ef if np.isfinite(ef) else None,
            ema_slow=es if np.isfinite(es) else None,
            vwap=vw if np.isfinite(vw) else None,
            weights=weights,
        )
        if f.score is not None:
            out.append((i, f.score))
    return out


def buckets(
    observations: list[tuple[float, float]], edges: tuple[float, ...] = (0, 30, 45, 55, 70, 100)
) -> list[Bucket]:
    """Group (score, forward return %) pairs by score band."""
    out = []
    for lo, hi in itertools.pairwise(edges):
        got = [r for s, r in observations if lo <= s < hi or (hi == edges[-1] and s == hi)]
        if got:
            arr = np.asarray(got, dtype=float)
            out.append(Bucket(lo, hi, len(got), float(arr.mean()), float((arr > 0).mean())))
        else:
            out.append(Bucket(lo, hi, 0, float("nan"), float("nan")))
    return out


def forward_return(df: pd.DataFrame, i: int, horizon: int) -> float | None:
    """% change from bar i's close to bar i+horizon's close (the future, for validation only)."""
    if i + horizon >= len(df):
        return None
    a = float(df["close"].iloc[i])
    b = float(df["close"].iloc[i + horizon])
    return (b - a) / a * 100 if a > 0 else None


def as_of(df: pd.DataFrame) -> datetime | None:
    return None if df.empty else pd.Timestamp(df.index[-1]).to_pydatetime()
