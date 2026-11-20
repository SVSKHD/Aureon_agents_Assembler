"""Live market computations (app/agents/research/live_market.py, docs/38).

Pure: no database, Redis or network. Times are IST sessions on Monday 2026-11-02 (a trading
day); Saturday 2026-11-07 is closed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.agents.research import indicators as ind
from app.agents.research import live_market as lm
from app.agents.research import stock_signals as ss
from app.agents.trading.dhan.parse import IST
from app.agents.trading.types import Timeframe

D = Decimal
MON = date(2026, 11, 2)
FRI = date(2026, 10, 30)


def ist(h: int, m: int, s: int = 0, day: date = MON) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, s, tzinfo=IST).astimezone(UTC)


# --- buckets and sessions --------------------------------------------------------------------


def test_buckets_are_session_aligned() -> None:
    assert lm.bucket(ist(9, 17), Timeframe.M5) == (ist(9, 15), ist(9, 20))
    assert lm.bucket(ist(9, 29, 59), Timeframe.M15) == (ist(9, 15), ist(9, 30))
    # The last hourly bar opens at 15:15 and is cut at the session close.
    assert lm.bucket(ist(15, 20), Timeframe.H1) == (ist(15, 15), ist(15, 30))
    assert lm.bucket(ist(10, 14), Timeframe.H1) == (ist(9, 15), ist(10, 15))
    # Outside the session there is no intraday bar; the daily bar is the IST date.
    assert lm.bucket(ist(9, 10), Timeframe.M1) is None
    assert lm.bucket(ist(15, 30), Timeframe.M1) is None
    start, end = lm.bucket(ist(9, 10), Timeframe.D1)  # type: ignore[misc]
    assert start.astimezone(IST).date() == MON and end == ist(15, 30)


def test_previous_trading_day_skips_weekends_and_holidays() -> None:
    assert lm.previous_trading_day(MON) == FRI
    assert lm.previous_trading_day(MON, [FRI]) == date(2026, 10, 29)


# --- live candle aggregation -----------------------------------------------------------------


def tick(
    book: lm.CandleBook, at: datetime, price: str, vol: int | None = None, key: str = "NSE_EQ:1"
) -> list[lm.LiveBar]:
    return book.apply(key, ltt=at, ltp=D(price), cum_volume=D(vol) if vol is not None else None)


def test_first_packet_is_a_baseline_then_bars_are_complete() -> None:
    book = lm.CandleBook([Timeframe.M1, Timeframe.M5])
    out = tick(book, ist(9, 16, 10), "100", 1000)
    assert {b.tf for b in out} == {Timeframe.M1, Timeframe.M5}
    assert all(b.partial for b in out)  # trades before the first packet were not seen
    assert all(b.volume is None for b in out)  # unknown, not zero
    tick(book, ist(9, 16, 40), "101", 1100)
    out = tick(book, ist(9, 17, 5), "99", 1250)
    closed = [b for b in out if b.closed]
    assert [(b.tf, b.ts) for b in closed] == [(Timeframe.M1, ist(9, 16))]
    m1 = book.get("NSE_EQ:1", Timeframe.M1)
    assert m1 is not None and not m1.partial and m1.volume == D(150)
    assert (m1.open, m1.high, m1.low, m1.close) == (D(99), D(99), D(99), D(99))
    m5 = book.get("NSE_EQ:1", Timeframe.M5)
    assert m5 is not None and m5.partial  # still the bar the baseline landed in
    assert (m5.open, m5.high, m5.low, m5.close, m5.volume) == (D(100), D(101), D(99), D(99), D(250))


def test_missing_minutes_are_not_synthesized() -> None:
    book = lm.CandleBook([Timeframe.M1])
    tick(book, ist(9, 16), "100", 10)
    tick(book, ist(9, 17), "100", 20)
    out = tick(book, ist(9, 21), "102", 30)  # no trades 09:18-09:20
    closed = [b.ts for b in out if b.closed]
    assert closed == [ist(9, 17)]
    assert book.get("NSE_EQ:1", Timeframe.M1).ts == ist(9, 21)  # type: ignore[union-attr]
    assert book.counters.closed_bars == 2


def test_duplicates_out_of_order_and_late_packets() -> None:
    book = lm.CandleBook([Timeframe.M1])
    tick(book, ist(9, 16, 1), "100", 10)
    tick(book, ist(9, 16, 30), "101", 20)
    assert tick(book, ist(9, 16, 30), "101", 20) == []  # an identical re-send (reconnect)
    assert book.counters.duplicates == 1
    # Older than the newest packet but inside the forming bar: widens the range, keeps close.
    tick(book, ist(9, 16, 20), "98", 15)
    bar = book.get("NSE_EQ:1", Timeframe.M1)
    assert bar is not None and bar.low == D(98) and bar.close == D(101) and bar.volume == D(10)
    assert book.counters.out_of_order == 1
    tick(book, ist(9, 17, 2), "102", 25)  # closes 09:16
    assert tick(book, ist(9, 16, 59), "97", 26) == []  # for a closed bar: late, dropped
    assert book.counters.late == 1


def test_timer_closes_the_last_bar_of_the_session() -> None:
    book = lm.CandleBook([Timeframe.M15, Timeframe.H1], grace_seconds=2)
    tick(book, ist(15, 10), "100", 10)
    tick(book, ist(15, 29, 50), "101", 20)
    assert book.close_due(ist(15, 30, 1)) == []  # within the grace
    closed = book.close_due(ist(15, 30, 2))
    assert sorted((b.tf.value, b.ts) for b in closed) == [
        ("15m", ist(15, 15)),
        ("1h", ist(15, 15)),
    ]
    assert all(b.end == ist(15, 30) and b.closed for b in closed)
    assert tick(book, ist(15, 29, 59), "99", 30) == []  # late after the timer close


def test_reconnect_marks_bars_partial_and_resets_the_volume_baseline() -> None:
    book = lm.CandleBook([Timeframe.M1])
    tick(book, ist(10, 0, 0), "100", 100)
    tick(book, ist(10, 1, 0), "100", 200)  # 10:01 bar is complete so far
    book.reset_continuity()
    assert book.get("NSE_EQ:1", Timeframe.M1).partial  # type: ignore[union-attr]
    tick(book, ist(10, 3, 0), "101", 900)  # baseline again: the missed volume is not assigned
    bar = book.get("NSE_EQ:1", Timeframe.M1)
    assert bar is not None and bar.partial and bar.volume is None
    tick(book, ist(10, 4, 0), "101", 950)
    bar = book.get("NSE_EQ:1", Timeframe.M1)
    assert bar is not None and not bar.partial and bar.volume == D(50)


def test_daily_bar_uses_the_exchange_day_ohlc() -> None:
    book = lm.CandleBook([Timeframe.D1])
    book.apply(
        "NSE_EQ:1",
        ltt=ist(11, 0),
        ltp=D("105"),
        cum_volume=D(5000),
        day_open=D("100"),
        day_high=D("108"),
        day_low=D("99"),
    )
    bar = book.get("NSE_EQ:1", Timeframe.D1)
    assert bar is not None and not bar.partial
    assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
        D(100),
        D(108),
        D(99),
        D(105),
        D(5000),
    )


def test_bar_json_round_trip() -> None:
    book = lm.CandleBook([Timeframe.M5])
    (bar,) = tick(book, ist(9, 20), "100.05", 7)
    again = lm.LiveBar.from_json(bar.as_json())
    assert again is not None and again.as_json() == bar.as_json()


# --- quotes and the previous close ------------------------------------------------------------


def test_prev_close_prefers_the_feed_packet_of_the_same_session() -> None:
    closes = {FRI: D("200")}
    got = lm.resolve_prev_close(
        day=MON, feed_value=D("201.5"), feed_at=ist(8, 59), daily_closes=closes
    )
    assert got == (D("201.5"), "feed", FRI)
    # Received on another day, or after the close: not trusted for this session.
    got = lm.resolve_prev_close(
        day=MON, feed_value=D("201.5"), feed_at=ist(15, 40), daily_closes=closes
    )
    assert got == (D("200"), "daily_candle", FRI)
    got = lm.resolve_prev_close(
        day=MON, feed_value=D("201.5"), feed_at=ist(10, 0, day=FRI), daily_closes=closes
    )
    assert got == (D("200"), "daily_candle", FRI)


def test_prev_close_over_a_holiday_and_when_unknown() -> None:
    thu = date(2026, 10, 29)
    got = lm.resolve_prev_close(
        day=MON, feed_value=None, feed_at=None, daily_closes={thu: D(90)}, holidays=[FRI]
    )
    assert got == (D(90), "daily_candle", thu)
    # A Friday close is not "the previous session" when Friday was a holiday.
    got = lm.resolve_prev_close(
        day=MON, feed_value=None, feed_at=None, daily_closes={}, holidays=[FRI]
    )
    assert got == (None, None, thu)


def quote(**kw: Any) -> lm.LiveQuote:
    q = lm.LiveQuote("NSE_EQ:1")
    q.merge({"kind": "quote", **kw}, kw.pop("received", ist(10, 0, 1)))
    return q


def test_quote_view_change_and_freshness() -> None:
    q = quote(ltp="110", ltt=ist(10, 0).isoformat(), volume="1000", atp="105")
    q.merge({"kind": "prev_close", "prev_close": "100"}, ist(9, 0))
    v = lm.quote_view(q, ist(10, 0, 5), daily_closes={}, market_open=True, stale_after=60)
    assert v["prev_close"] == "100" and v["prev_close_source"] == "feed"
    assert v["change"] == "10" and v["change_pct"] == "10.0000"
    assert v["status"] == "live" and v["turnover"] == "105000"
    stale = lm.quote_view(q, ist(10, 5), daily_closes={}, market_open=True, stale_after=60)
    assert stale["status"] == "stale"
    # Outside the session silence is expected: closed, never stale.
    closed = lm.quote_view(q, ist(20, 0), daily_closes={}, market_open=False, stale_after=60)
    assert closed["status"] == "closed" and closed["change_pct"] == "10.0000"


def test_quote_ignores_older_packets_and_bad_values() -> None:
    q = quote(ltp="110", ltt=ist(10, 0).isoformat())
    assert not q.merge({"kind": "ticker", "ltp": "90", "ltt": ist(9, 59).isoformat()}, ist(10, 1))
    q.merge({"kind": "ticker", "ltp": "NaN", "ltt": ist(10, 1).isoformat()}, ist(10, 1))
    assert q.ltp == D(110)


def test_change_is_against_the_session_of_the_last_trade() -> None:
    # Saturday: the last trade was Friday, so the change is Friday vs Thursday.
    thu = date(2026, 10, 29)
    q = quote(ltp="105", ltt=ist(15, 29, day=FRI).isoformat(), received=ist(15, 29, day=FRI))
    v = lm.quote_view(
        q,
        datetime(2026, 11, 7, 6, 0, tzinfo=UTC),
        daily_closes={thu: D(100), FRI: D(104)},
        market_open=False,
        stale_after=60,
    )
    assert v["session_date"] == FRI.isoformat() and v["prev_close"] == "100"
    assert v["change_pct"] == "5.0000" and v["status"] == "closed"


# --- relative volume --------------------------------------------------------------------------


def test_time_of_day_relative_volume() -> None:
    bars = []
    for d in (FRI, date(2026, 10, 29), date(2026, 10, 28), date(2026, 10, 27)):
        for k in range(25):
            bars.append((ist(9, 15, day=d) + timedelta(minutes=15 * k), 100.0))
    prof = lm.volume_profile(bars, MON)
    assert prof is not None and prof[0] == 100 and prof[-1] == 2500
    # 09:37:30 = 1.5 slots: a normal session had traded 150.
    assert lm.expected_volume(prof, ist(9, 37, 30)) == pytest.approx(150)
    assert lm.live_rvol(D(300), prof, ist(9, 37, 30)) == pytest.approx(2.0)
    assert lm.live_rvol(D(300), prof, ist(9, 20)) is None  # too early to judge
    assert lm.volume_profile(bars[:50], MON) is None  # fewer than 3 sessions


def test_incomplete_sessions_do_not_count_in_the_profile() -> None:
    bars = [(ist(9, 15, day=d), 100.0) for d in (FRI, date(2026, 10, 29), date(2026, 10, 28))]
    assert lm.volume_profile(bars, MON) is None


# --- market movers ----------------------------------------------------------------------------


def row(
    iid: int, chg: str | None, *, status: str = "live", ltp: str = "100", **kw: Any
) -> dict[str, Any]:
    return {
        "instrument_id": iid,
        "ltp": ltp,
        "change_pct": chg,
        "status": status,
        "avg_turnover": kw.pop("avg_turnover", 1e9),
        "rvol": kw.pop("rvol", None),
        **kw,
    }


def test_movers_sorting_ties_and_exclusions() -> None:
    rows = [
        row(1, "2.5"),
        row(2, "5.0"),
        row(3, "5.0"),  # tie with 2: lower id first
        row(4, "-1.0"),
        row(5, "-3.0"),
        row(6, "9.0", status="stale"),  # excluded unless include_stale
        row(7, "8.0", ltp="5"),  # below min price
        row(8, "7.0", avg_turnover=1e3),  # illiquid
        row(9, "6.0", avg_turnover=None),  # liquidity unknown
        row(10, None),  # no previous close
        row(11, "0"),  # unchanged: neither list
    ]
    liq = lm.Liquidity(min_price=D(10), min_avg_turnover=1e6)
    top, cov = lm.rank_movers(
        rows, "gainers", limit=10, liquidity=liq, min_rvol=2, universe_size=20
    )
    assert [r["instrument_id"] for r in top] == [2, 3, 1]
    assert cov.universe_size == 20 and cov.stale == 1 and cov.illiquid == 2
    assert cov.liquidity_unknown == 1 and cov.no_prev_close == 1 and cov.evaluated == 6
    losers, _ = lm.rank_movers(rows, "losers", limit=1, liquidity=liq, min_rvol=2, universe_size=20)
    assert [r["instrument_id"] for r in losers] == [5]
    with_stale, _ = lm.rank_movers(
        rows, "gainers", limit=1, liquidity=liq, min_rvol=2, universe_size=20, include_stale=True
    )
    assert with_stale[0]["instrument_id"] == 6


def test_unusual_volume_ranks_by_rvol() -> None:
    rows = [row(1, "1", rvol=2.5), row(2, None, rvol=4.0), row(3, "1", rvol=1.5)]
    top, _ = lm.rank_movers(
        rows, "unusual_volume", limit=10, liquidity=lm.Liquidity(), min_rvol=2, universe_size=3
    )
    assert [r["instrument_id"] for r in top] == [2, 1]


# --- chart overlays and the live state --------------------------------------------------------


def series(n: int, start: datetime, step: timedelta, *, seed: int = 7) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    px = 100 + np.cumsum(rng.normal(0, 0.6, n))
    out = []
    t = start
    for i in range(n):
        c = float(px[i])
        out.append(
            {
                "ts": t,
                "open": c - 0.1,
                "high": c + 0.4,
                "low": c - 0.4,
                "close": c,
                "volume": 1000 + 10 * i,
                "is_closed": True,
            }
        )
        t += step
    return out


def test_chart_overlays_match_the_signal_engine() -> None:
    # 15m bars over several sessions: build them on session-aligned times only.
    rows: list[dict[str, Any]] = []
    d = date(2026, 10, 19)
    while len(rows) < 120:
        if d.weekday() < 5:
            rows += series(25, ist(9, 15, day=d), timedelta(minutes=15), seed=len(rows))
        d += timedelta(days=1)
    ov = lm.chart_overlays(rows, Timeframe.M15)
    df = pd.DataFrame(
        {k: [float(r[k]) for r in rows] for k in ("open", "high", "low", "close", "volume")},
        index=pd.DatetimeIndex([r["ts"] for r in rows]),
    )
    reading = ss.evaluate(df, Timeframe.M15, rows[-1]["ts"] + timedelta(minutes=15))
    assert reading.ema_fast is not None
    assert Decimal(ov["ema_fast"][-1]).quantize(D("0.0001")) == reading.ema_fast.quantize(
        D("0.0001")
    )
    assert Decimal(ov["ema_slow"][-1]).quantize(D("0.0001")) == reading.ema_slow.quantize(
        D("0.0001")
    )  # type: ignore[union-attr]
    assert Decimal(ov["vwap"][-1]).quantize(D("0.0001")) == reading.vwap.quantize(D("0.0001"))  # type: ignore[union-attr]
    # VWAP resets every session: the first bar of a day equals its typical price.
    first = rows[25]
    tp = (float(first["high"]) + float(first["low"]) + float(first["close"])) / 3
    assert float(ov["vwap"][25]) == pytest.approx(tp)
    # No lookahead: truncating the series does not change earlier values.
    short = lm.chart_overlays(rows[:80], Timeframe.M15)
    assert short["ema_fast"] == ov["ema_fast"][:80] and short["vwap"] == ov["vwap"][:80]


def test_crossover_markers_and_forming_bar() -> None:
    # Rising first (fast above slow), then down (bearish cross), then up (bullish cross). A
    # flat start would have no side yet, and leaving "no side" is not a cross.
    closes = [10.0 + 0.01 * i for i in range(40)] + [9.0] * 10 + [12.0] * 10
    rows = [
        {
            "ts": ist(9, 15) + timedelta(minutes=i),
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "volume": 1,
            "is_closed": i < len(closes) - 1,
        }
        for i, c in enumerate(closes)
    ]
    ov = lm.chart_overlays(rows, Timeframe.M1)
    kinds = [m["kind"] for m in ov["markers"]]
    assert kinds[0] == "bearish_cross" and kinds[-1] == "bullish_cross"
    ef = ind.ema(pd.Series(closes), 8).to_numpy()
    assert Decimal(ov["ema_fast"][-1]) == Decimal(ind.to_dec(float(ef[-1])))
    rows[-1]["is_closed"] = False
    cross_on_last = lm.chart_overlays(rows, Timeframe.M1)["markers"]
    assert all(m["confirmed"] for m in cross_on_last if m["ts"] != rows[-1]["ts"])


def test_session_marks() -> None:
    marks = lm.session_marks(ist(9, 15, day=FRI), ist(15, 0))
    assert [m["date"] for m in marks] == [FRI.isoformat(), MON.isoformat()]


class Bar:
    def __init__(self, r: dict[str, Any]) -> None:
        self.ts, self.open, self.high, self.low = (
            r["ts"],
            D(str(r["open"])),
            D(str(r["high"])),
            D(str(r["low"])),
        )
        self.close, self.volume = D(str(r["close"])), D(str(r["volume"]))


def test_live_state_evaluates_the_forming_bar_but_never_hides_a_gap() -> None:
    rows: list[dict[str, Any]] = []
    d = date(2026, 10, 19)
    while d < MON:
        if d.weekday() < 5:
            rows += series(25, ist(9, 15, day=d), timedelta(minutes=15), seed=len(rows))
        d += timedelta(days=1)
    stored = [Bar(r) for r in rows]
    book = lm.CandleBook([Timeframe.M15])
    for i, p in enumerate(("101", "102", "103")):
        tick(book, ist(9, 15 + i), p, 100 * (i + 1))
    forming = book.get("NSE_EQ:1", Timeframe.M15)
    s = lm.merge_series(stored, [], forming, Timeframe.M15)
    assert not s.history_gap and s.live_bars == 1 and s.forming is forming
    reading, flags = lm.live_reading(s, Timeframe.M15, ist(9, 18), ss.DEFAULT)
    assert reading.state != ss.State.UNAVAILABLE and flags["candle_closed"] is False
    assert reading.last == D("103")
    assert flags["partial_bars"] == 1  # the bar the first packet landed in
    # The confirmed reading at the same moment ignores the forming bar entirely.
    confirmed = ss.evaluate(s.frame, Timeframe.M15, ist(9, 18))
    assert confirmed.candle_ts == stored[-1].ts and confirmed.last != D("103")
    # Stored history ending on Thursday: Friday's bars are missing → unavailable, not computed.
    gap = lm.merge_series(stored[:-25], [], forming, Timeframe.M15)
    reading, flags = lm.live_reading(gap, Timeframe.M15, ist(9, 18), ss.DEFAULT)
    assert gap.history_gap and reading.state == ss.State.UNAVAILABLE
    assert "missing" in (reading.reason or "")
