"""NSE/BSE cash-market sessions, for deciding which candles should exist (docs/37).

The equity session is 09:15-15:30 IST on weekdays that are not in DHAN_MARKET_HOLIDAYS
(there is no built-in exchange calendar: holidays come from that setting). Dhan's intraday
candles start at 09:15, so a 15m session has bars at 09:15, 09:30, ... 15:15.

"Stale" is measured in trading time, not wall time: a 15m candle from Friday 15:15 is not
stale on Monday at 09:20, because no 15m candle could have closed in between.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime, timedelta

from app.agents.trading.dhan.parse import IST
from app.agents.trading.types import Timeframe
from app.services.dhan_feed_health import CLOSE_AT, OPEN_AT
from app.services.research import tf_delta

SESSION_MINUTES = 375  # 09:15 → 15:30


def is_trading_day(day: date, holidays: Iterable[date] = ()) -> bool:
    return day.weekday() < 5 and day not in set(holidays)


def session_bounds(day: date) -> tuple[datetime, datetime]:
    """The day's open and close as UTC datetimes."""
    return (
        datetime.combine(day, OPEN_AT, IST).astimezone(UTC),
        datetime.combine(day, CLOSE_AT, IST).astimezone(UTC),
    )


def _days(a: datetime, b: datetime) -> Iterator[date]:
    d = a.astimezone(IST).date()
    end = b.astimezone(IST).date()
    while d <= end:
        yield d
        d += timedelta(days=1)


def trading_seconds_between(a: datetime, b: datetime, holidays: Iterable[date] = ()) -> float:
    """Seconds of open market between `a` and `b` (0 when b <= a)."""
    if b <= a:
        return 0.0
    hol = set(holidays)
    total = 0.0
    for d in _days(a, b):
        if not is_trading_day(d, hol):
            continue
        lo, hi = session_bounds(d)
        total += max(0.0, (min(b, hi) - max(a, lo)).total_seconds())
    return total


def trading_days_between(a: datetime, b: datetime, holidays: Iterable[date] = ()) -> int:
    """Trading days strictly after a's IST date, up to and including b's IST date."""
    hol = set(holidays)
    start = a.astimezone(IST).date()
    return sum(1 for d in _days(a, b) if d > start and is_trading_day(d, hol))


def candle_close(ts: datetime, tf: Timeframe) -> datetime:
    """When a candle opened at `ts` is complete. Intraday bars never run past the session
    close (the 15:15 1h bar closes at 15:30); a daily bar closes at that day's 15:30."""
    if tf is Timeframe.D1:
        return session_bounds(ts.astimezone(IST).date())[1]
    end = ts + tf_delta(tf)
    close = session_bounds(ts.astimezone(IST).date())[1]
    return min(end, close) if ts < close else end


def missed_bars(
    last_close: datetime, now: datetime, tf: Timeframe, holidays: Iterable[date] = ()
) -> float:
    """How many whole bars of `tf` should have closed after `last_close` by `now`, in trading time.
    0 means the newest stored candle is the newest one that could exist."""
    if tf is Timeframe.D1:
        # Today's bar only counts once today's session has closed.
        hol = set(holidays)
        n = trading_days_between(last_close, now, hol)
        today = now.astimezone(IST).date()
        if (
            is_trading_day(today, hol)
            and now < session_bounds(today)[1]
            and today > last_close.astimezone(IST).date()
        ):
            n -= 1
        return float(max(n, 0))
    # Only whole bars: a bar that is still forming has not been missed.
    return float(
        int(trading_seconds_between(last_close, now, holidays) // tf_delta(tf).total_seconds())
    )


def latest_boundary(now: datetime, tf: Timeframe, holidays: Iterable[date] = ()) -> datetime | None:
    """The close time of the newest `tf` bar that has closed by `now` in today's session, or
    None outside a session (before the first bar closes, weekends, holidays). Intraday only."""
    if tf is Timeframe.D1:
        return None
    day = now.astimezone(IST).date()
    if not is_trading_day(day, holidays):
        return None
    lo, hi = session_bounds(day)
    step = tf_delta(tf)
    if now < lo + step and now < hi:
        return None
    if now >= hi:
        return hi
    k = int((now - lo) / step)
    return lo + k * step


def is_open(now: datetime, holidays: Iterable[date] = ()) -> bool:
    day = now.astimezone(IST).date()
    lo, hi = session_bounds(day)
    return is_trading_day(day, holidays) and lo <= now < hi


__all__ = [
    "IST",
    "SESSION_MINUTES",
    "candle_close",
    "is_open",
    "is_trading_day",
    "latest_boundary",
    "missed_bars",
    "session_bounds",
    "trading_days_between",
    "trading_seconds_between",
]
