"""Stock Signals engine (docs/37): crossovers, VWAP confirmation and session reset, closed
candles only, stale and insufficient data. Pure: no database, no broker."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.agents.research import indicators as ind
from app.agents.research import stock_signals as ss
from app.agents.research.stock_signals import Params, State
from app.agents.trading.types import Timeframe
from app.services import market_session as ms

M15 = Timeframe.M15
STEP = timedelta(minutes=15)
# Mon 2026-11-09 .. Fri 2026-11-13: an ordinary NSE week.
DAYS = [date(2026, 11, 9) + timedelta(days=i) for i in range(5)]


def ist(d: date, hh: int, mm: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ms.IST).astimezone(UTC)


def session(d: date) -> list[datetime]:
    """The 25 fifteen-minute bar open times of one session (09:15 … 15:15 IST)."""
    lo, _ = ms.session_bounds(d)
    return [lo + i * STEP for i in range(25)]


def frame(
    times: list[datetime], closes: list[float], volume: list[float] | None = None
) -> pd.DataFrame:
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": np.asarray(volume if volume is not None else [1.0] * len(c), dtype=float),
        },
        index=pd.DatetimeIndex(times, name="ts"),
    )


def after(df: pd.DataFrame, minutes: int = 1) -> datetime:
    """A moment just after the last candle closed."""
    return ms.candle_close(df.index[-1].to_pydatetime(), M15) + timedelta(minutes=minutes)


def falling_then(d_tail: list[float]) -> pd.DataFrame:
    """Three sessions falling 200 → 125 (a clear downtrend), then `d_tail` on day 4."""
    times = session(DAYS[0]) + session(DAYS[1]) + session(DAYS[2])
    closes = list(np.linspace(200, 125, len(times)))
    return frame(times + session(DAYS[3])[: len(d_tail)], closes + d_tail)


def first_flip(df: pd.DataFrame, fast: int = 8, slow: int = 32) -> int:
    d = (ind.ema(df["close"], fast) - ind.ema(df["close"], slow)).to_numpy()
    for i in range(1, len(d)):
        if np.isfinite(d[i - 1]) and d[i - 1] < 0 < d[i]:
            return i
    raise AssertionError("no bullish flip")


# --- crossovers --------------------------------------------------------------------------------


def test_bullish_cross_confirmed_by_vwap_on_the_cross_bar() -> None:
    df = falling_then([130, 140, 150, 160, 170, 180])
    k = first_flip(df)
    cut = df.iloc[: k + 1]
    r = ss.evaluate(cut, M15, after(cut))
    assert r.state is State.BULLISH_CONFIRMED and r.direction == "bullish"
    assert r.crossed_ts == cut.index[-1].to_pydatetime()
    assert r.crossed_at == r.confirmed_at == r.candle_close_at  # confirmed on the cross bar
    assert r.confirm_close is not None and r.confirm_vwap is not None
    assert r.confirm_close > r.confirm_vwap
    assert r.ema_fast is not None and r.ema_slow is not None and r.ema_fast > r.ema_slow


def test_bullish_cross_pending_then_confirmed_later_keeps_both_times() -> None:
    # Day 4 opens with a huge-volume print at 190, so the session VWAP sits far above the
    # following closes: the EMA cross happens under VWAP (pending) …
    tail = [190.0, 150, 152, 230, 240]
    vol = [1.0] * 75 + [5000.0] + [1.0] * 4
    times = session(DAYS[0]) + session(DAYS[1]) + session(DAYS[2]) + session(DAYS[3])[: len(tail)]
    df = frame(times, list(np.linspace(200, 125, 75)) + tail, vol)
    pend = None
    for end in range(76, len(df) + 1):
        r = ss.evaluate(df.iloc[:end], M15, after(df.iloc[:end]))
        if r.state is State.BULLISH_PENDING:
            pend = (end, r)
            break
    assert pend is not None, "expected a pending bullish cross"
    end, p = pend
    assert p.confirmed_at is None and p.crossed_at is not None
    assert p.vwap is not None and p.last is not None and p.last < p.vwap
    # … and is confirmed when a later close clears VWAP, within the confirmation window.
    for end2 in range(end + 1, min(end + Params().confirm_bars, len(df)) + 1):
        r = ss.evaluate(df.iloc[:end2], M15, after(df.iloc[:end2]))
        if r.state is State.BULLISH_CONFIRMED:
            assert r.crossed_at == p.crossed_at  # the cross time is kept
            assert r.confirmed_at is not None and r.confirmed_at > r.crossed_at
            break
    else:
        raise AssertionError("pending cross was never confirmed")


def test_bearish_cross_is_the_mirror() -> None:
    times = session(DAYS[0]) + session(DAYS[1]) + session(DAYS[2])
    up = list(np.linspace(100, 175, len(times)))
    tail = [170, 160, 150, 140, 130, 120]
    df = frame(times + session(DAYS[3])[: len(tail)], up + tail)
    found = [ss.evaluate(df.iloc[:e], M15, after(df.iloc[:e])) for e in range(76, len(df) + 1)]
    states = [r.state for r in found]
    assert State.BEARISH_CONFIRMED in states
    r = found[states.index(State.BEARISH_CONFIRMED)]
    assert r.direction == "bearish" and r.confirm_close is not None and r.confirm_vwap is not None
    assert r.confirm_close < r.confirm_vwap


def test_trend_and_mixed_without_a_recent_cross() -> None:
    times = session(DAYS[0]) + session(DAYS[1]) + session(DAYS[2])
    up = frame(times, list(np.linspace(100, 175, len(times))))
    assert ss.evaluate(up, M15, after(up)).state is State.BULLISH_TREND
    down = frame(times, list(np.linspace(175, 100, len(times))))
    assert ss.evaluate(down, M15, after(down)).state is State.BEARISH_TREND
    # EMAs still bullish, but the day's last close dropped under its session VWAP: MIXED.
    mixed = frame(times, [*np.linspace(100, 175, len(times) - 1), 150.0])
    r = ss.evaluate(mixed, M15, after(mixed))
    assert r.state is State.MIXED and r.ema_fast is not None and r.ema_slow is not None
    assert r.ema_fast > r.ema_slow and r.last is not None and r.vwap is not None and r.last < r.vwap


def test_old_cross_outside_the_window_is_a_trend_not_an_event() -> None:
    df = falling_then([130, 140, 150, 160, 170, 180, 190, 200, 210, 220, 230, 240])
    k = first_flip(df)
    cut = df.iloc[: k + Params().confirm_bars + 1]
    assert ss.evaluate(cut, M15, after(cut)).state is State.BULLISH_TREND


# --- VWAP --------------------------------------------------------------------------------------


def test_vwap_resets_at_each_session() -> None:
    times = session(DAYS[0]) + session(DAYS[1])
    df = frame(times, [100.0] * 25 + [200.0] * 25)
    v = ind.vwap(df, ss.VWAP_TZ).to_numpy()
    assert v[24] == pytest.approx(100.0) and v[25] == pytest.approx(200.0)  # day 2 starts fresh


def test_daily_candles_use_the_day_vwap_or_stay_pending() -> None:
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(120)]
    trading = [d for d in days if d.weekday() < 5][:80]
    closes = [*np.linspace(200, 125, 74), 130, 140, 150, 160, 170, 180]
    df = frame([ms.session_bounds(d)[0] for d in trading], closes)
    found = None
    for e in range(66, len(df) + 1):
        now = ms.session_bounds(trading[e - 1])[1] + timedelta(minutes=1)
        r = ss.evaluate(df.iloc[:e], Timeframe.D1, now)
        if r.is_cross:
            found = (e, now, r)
            break
    assert found is not None
    e, now, r = found
    assert r.state is State.BULLISH_PENDING and r.vwap is None and not r.vwap_applicable
    day = trading[e - 1]
    vw = pd.Series({day: 100.0})  # that day's session VWAP from intraday candles
    r2 = ss.evaluate(df.iloc[:e], Timeframe.D1, now, day_vwap=vw)
    assert r2.state is State.BULLISH_CONFIRMED and r2.vwap == ss._d(100.0)


def test_daily_vwap_from_intraday_candles() -> None:
    df = frame(session(DAYS[0]) + session(DAYS[1]), [100.0] * 25 + [200.0] * 25)
    out = ss.daily_vwap(df)
    assert out[DAYS[0]] == pytest.approx(100.0) and out[DAYS[1]] == pytest.approx(200.0)


# --- closed candles, no lookahead --------------------------------------------------------------


def test_forming_and_future_candles_are_never_read() -> None:
    df = falling_then([130, 140, 150, 160, 170, 180])
    k = first_flip(df)
    upto = df.iloc[: k + 1]
    now = after(upto)
    base = ss.evaluate(upto, M15, now)
    # Add the next bar (still forming at `now`) and later bars with wild prices.
    future = df.iloc[: k + 1].copy()
    extra = frame(
        [upto.index[-1].to_pydatetime() + STEP * i for i in (1, 2, 3)], [1.0, 9999.0, 1.0]
    )
    assert ss.evaluate(pd.concat([future, extra]), M15, now) == base
    # Before the cross bar closes, the cross does not exist yet.
    early = ss.evaluate(
        upto, M15, ms.candle_close(upto.index[-1].to_pydatetime(), M15) - timedelta(seconds=1)
    )
    assert early.candle_ts == upto.index[-2].to_pydatetime() and not early.is_cross


# --- stale and insufficient --------------------------------------------------------------------


def test_insufficient_data_is_unavailable() -> None:
    df = frame(session(DAYS[0]) + session(DAYS[1])[:10], list(np.linspace(100, 120, 35)))
    r = ss.evaluate(df, M15, after(df))
    assert r.state is State.UNAVAILABLE and "insufficient" in (r.reason or "")
    assert r.ema_fast is None and r.vwap is None  # nothing invented
    assert ss.evaluate(frame([], []), M15, after(df)).state is State.UNAVAILABLE


def test_stale_is_counted_in_trading_time() -> None:
    times = session(DAYS[0]) + session(DAYS[1]) + session(DAYS[2]) + session(DAYS[3])
    df = frame(times, list(np.linspace(100, 175, len(times))))
    # Friday 11:00: Thursday's last bar is 7 bars behind → stale.
    r = ss.evaluate(df, M15, ist(DAYS[4], 11, 0))
    assert r.state is State.UNAVAILABLE and "stale" in (r.reason or "")
    assert r.last is not None and r.ema_fast is None  # the price is reported, no signal
    # Friday 09:20: no Friday bar has closed yet → fresh.
    assert ss.evaluate(df, M15, ist(DAYS[4], 9, 20)).state is State.BULLISH_TREND
    # A holiday Friday: Monday 09:20 is still fresh after the long weekend.
    mon = date(2026, 11, 16)
    assert ss.evaluate(df, M15, ist(mon, 9, 20), holidays=[DAYS[4]]).state is State.BULLISH_TREND


def test_unsupported_timeframe() -> None:
    r = ss.evaluate(frame([], []), Timeframe.M1, datetime.now(UTC))
    assert r.state is State.UNAVAILABLE and "not supported" in (r.reason or "")


# --- market sessions ---------------------------------------------------------------------------


def test_session_boundaries() -> None:
    d = DAYS[0]
    assert ms.latest_boundary(ist(d, 9, 20), M15) is None  # first bar not closed yet
    assert ms.latest_boundary(ist(d, 9, 31), M15) == ist(d, 9, 30)
    assert ms.latest_boundary(ist(d, 15, 40), M15) == ist(d, 15, 30)
    assert ms.latest_boundary(ist(date(2026, 11, 14), 11, 0), M15) is None  # Saturday
    assert ms.latest_boundary(ist(d, 11, 0), M15, holidays=[d]) is None
    # The last hourly bar (15:15) closes at the session close, not 16:15.
    assert ms.candle_close(ist(d, 15, 15), Timeframe.H1) == ist(d, 15, 30)
    assert ms.candle_close(ist(d, 9, 15), Timeframe.D1) == ist(d, 15, 30)
    assert ms.trading_seconds_between(ist(d, 15, 0), ist(DAYS[1], 9, 45)) == 60 * 60
    assert ms.missed_bars(ist(DAYS[4], 15, 30), ist(date(2026, 11, 16), 9, 20), M15) == 0.0
