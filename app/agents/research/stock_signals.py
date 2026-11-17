"""Stock Signals engine: EMA 8/32 crossover + session VWAP (docs/37-stock-signals.md).

Pure and deterministic: candles in, one Reading out. No database, no broker, no clock (the
caller passes `now`). It reuses the research indicators (`indicators.ema`, SMA-seeded, and
`indicators.vwap`, anchored to the IST day) and the screener's `find_cross`, so a signal here
agrees to the digit with the chart, the stored indicators and the screener.

Rules (all on CLOSED candles only; a candle still forming at `now` is dropped first, and
nothing after `now` is ever read):

- A bullish cross is the bar where EMA fast - EMA slow turns from negative to positive (a
  touch of zero carries the previous side, as in the screener). Bearish is the reverse.
- A cross is CONFIRMED when a close is on the same side of the session VWAP, on the cross
  bar or one of the next `confirm_bars - 1` bars; the first such bar's close time is the
  confirmation time, kept apart from the cross time. Until then it is PENDING.
- With no cross in that window: EMA fast > slow and close > VWAP is BULLISH_TREND, the
  mirror is BEARISH_TREND, anything else (including no VWAP) is MIXED.
- Too few candles for the slow EMA, or a newest candle older than `stale_bars` bars of
  trading time: UNAVAILABLE, with the reason. Nothing is filled in or guessed.

VWAP is the session VWAP (resets at the IST day boundary, i.e. each NSE/BSE session). Daily
candles have no intraday VWAP of their own: the caller passes each day's closing session VWAP
computed from that day's stored intraday candles, or nothing, in which case a daily cross
can only be PENDING and a daily trend is MIXED.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.agents.research import indicators as ind
from app.agents.research.screener import find_cross
from app.agents.trading.types import Timeframe
from app.services.market_session import candle_close, missed_bars

VWAP_TZ = "Asia/Kolkata"
SUPPORTED = (Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.D1)
Direction = Literal["bullish", "bearish"]


class State(StrEnum):
    BULLISH_CONFIRMED = "BULLISH_CONFIRMED"
    BULLISH_PENDING = "BULLISH_PENDING"
    BEARISH_CONFIRMED = "BEARISH_CONFIRMED"
    BEARISH_PENDING = "BEARISH_PENDING"
    BULLISH_TREND = "BULLISH_TREND"
    BEARISH_TREND = "BEARISH_TREND"
    MIXED = "MIXED"
    UNAVAILABLE = "UNAVAILABLE"


CROSS_STATES = {
    State.BULLISH_CONFIRMED,
    State.BULLISH_PENDING,
    State.BEARISH_CONFIRMED,
    State.BEARISH_PENDING,
}


@dataclass(frozen=True, slots=True)
class Params:
    fast: int = 8
    slow: int = 32
    # A cross waits for VWAP confirmation on its own bar and the next (confirm_bars - 1).
    confirm_bars: int = 4
    # The newest closed candle may lag the newest possible one by this many bars.
    stale_bars: float = 2.0
    # 0 = 2 x slow: the slow EMA is seeded by an SMA of its first `slow` closes.
    min_bars: int = 0

    @property
    def needed(self) -> int:
        return self.min_bars or 2 * self.slow


DEFAULT = Params()


@dataclass(frozen=True, slots=True)
class Reading:
    state: State
    bars: int
    candle_ts: datetime | None = None  # open time of the newest closed candle used
    candle_close_at: datetime | None = None
    last: Decimal | None = None
    ema_fast: Decimal | None = None
    ema_slow: Decimal | None = None
    vwap: Decimal | None = None
    direction: Direction | None = None  # of the cross, for the four cross states
    crossed_ts: datetime | None = None  # open time of the cross bar
    crossed_at: datetime | None = None  # close time of the cross bar
    cross_close: Decimal | None = None
    confirmed_at: datetime | None = None  # close time of the first VWAP-confirming bar
    confirm_close: Decimal | None = None
    confirm_vwap: Decimal | None = None
    vwap_applicable: bool = True
    reason: str | None = None

    @property
    def is_cross(self) -> bool:
        return self.state in CROSS_STATES

    def metrics(self) -> dict[str, Any]:
        """Supporting numbers as Decimal strings (JSON-safe): spread and VWAP distance in %."""
        out: dict[str, Any] = {"bars": self.bars}
        if self.ema_fast is not None and self.ema_slow is not None and self.ema_slow != 0:
            out["ema_spread_pct"] = _s(_q((self.ema_fast - self.ema_slow) / self.ema_slow * 100))
        if self.last is not None and self.vwap is not None and self.vwap != 0:
            out["vwap_distance_pct"] = _s(_q((self.last - self.vwap) / self.vwap * 100))
        return out


def _q(x: Decimal) -> Decimal:
    return x.quantize(ind.Q)


def _s(d: Decimal | None) -> str | None:
    return None if d is None else format(d, "f")


def _d(x: float) -> Decimal | None:
    if x is None or not np.isfinite(x):
        return None
    return _q(Decimal(repr(float(x))))


def _py(ts: Any) -> datetime:
    return pd.Timestamp(ts).to_pydatetime()


def closed_only(df: pd.DataFrame, tf: Timeframe, now: datetime) -> pd.DataFrame:
    """Drop every candle that has not closed by `now` (no lookahead, no forming bar)."""
    if df.empty:
        return df
    keep = [candle_close(_py(t), tf) <= now for t in df.index]
    return df[np.asarray(keep, dtype=bool)]


def daily_vwap(intraday: pd.DataFrame) -> pd.Series:
    """Each IST day's closing session VWAP from that day's intraday candles, indexed by the
    IST date. Used to give daily candles a VWAP; days without volume have none."""
    if intraday.empty:
        return pd.Series(dtype=float)
    v = ind.vwap(intraday, VWAP_TZ)
    days = pd.DatetimeIndex(intraday.index).tz_convert(VWAP_TZ).date
    return v.groupby(days).last()


def evaluate(
    df: pd.DataFrame,
    tf: Timeframe,
    now: datetime,
    params: Params = DEFAULT,
    *,
    day_vwap: pd.Series | None = None,
    holidays: Iterable[date] = (),
) -> Reading:
    """Classify the newest closed candle of `df` (float OHLCV frame, UTC index ascending)."""
    if tf not in SUPPORTED:
        return Reading(State.UNAVAILABLE, 0, reason=f"timeframe {tf.value} is not supported")
    df = closed_only(df.sort_index(), tf, now)
    n = len(df)
    if n == 0:
        return Reading(State.UNAVAILABLE, 0, reason="no closed candles stored")
    last_ts = _py(df.index[-1])
    close_at = candle_close(last_ts, tf)
    base = Reading(State.UNAVAILABLE, n, candle_ts=last_ts, candle_close_at=close_at)
    if n < params.needed:
        return replace(
            base, reason=f"insufficient data: {n} closed candles, {params.needed} needed"
        )
    missed = missed_bars(close_at, now, tf, holidays)
    if missed > params.stale_bars:
        return replace(
            base,
            last=_d(df["close"].iloc[-1]),
            reason=f"stale: the newest closed candle is {missed:.0f} bars behind",
        )

    close = df["close"]
    fast = ind.ema(close, params.fast).to_numpy(dtype=float)
    slow = ind.ema(close, params.slow).to_numpy(dtype=float)
    if tf is Timeframe.D1:
        applicable = day_vwap is not None and len(day_vwap) > 0
        days = pd.DatetimeIndex(df.index).tz_convert(VWAP_TZ).date
        vw = (
            np.array([float(day_vwap.get(d, np.nan)) for d in days])  # type: ignore[union-attr]
            if applicable
            else np.full(n, np.nan)
        )
    else:
        applicable = True
        vw = ind.vwap(df, VWAP_TZ).to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    if not (np.isfinite(fast[-1]) and np.isfinite(slow[-1])):
        return replace(base, reason="insufficient data for the slow EMA")

    valued = replace(
        base,
        last=_d(c[-1]),
        ema_fast=_d(fast[-1]),
        ema_slow=_d(slow[-1]),
        vwap=_d(vw[-1]),
        vwap_applicable=applicable,
    )
    diff = fast - slow
    directions: tuple[Direction, Direction] = ("bullish", "bearish")
    for direction in directions:
        i = find_cross(diff, params.confirm_bars, direction)
        if i is None:
            continue
        bullish = direction == "bullish"
        side = 1.0 if bullish else -1.0
        crossed_ts = _py(df.index[i])
        crossed = replace(
            valued,
            direction=direction,
            crossed_ts=crossed_ts,
            crossed_at=candle_close(crossed_ts, tf),
            cross_close=_d(c[i]),
        )
        for j in range(i, n):
            if np.isfinite(vw[j]) and (c[j] - vw[j]) * side > 0:
                return replace(
                    crossed,
                    state=State.BULLISH_CONFIRMED if bullish else State.BEARISH_CONFIRMED,
                    confirmed_at=candle_close(_py(df.index[j]), tf),
                    confirm_close=_d(c[j]),
                    confirm_vwap=_d(vw[j]),
                )
        why = None if np.isfinite(vw[-1]) else "no session VWAP: the cross cannot be confirmed"
        return replace(
            crossed,
            state=State.BULLISH_PENDING if bullish else State.BEARISH_PENDING,
            reason=why,
        )

    if not np.isfinite(vw[-1]):
        return replace(valued, state=State.MIXED, reason="no session VWAP")
    if fast[-1] > slow[-1] and c[-1] > vw[-1]:
        return replace(valued, state=State.BULLISH_TREND)
    if fast[-1] < slow[-1] and c[-1] < vw[-1]:
        return replace(valued, state=State.BEARISH_TREND)
    return replace(valued, state=State.MIXED)
