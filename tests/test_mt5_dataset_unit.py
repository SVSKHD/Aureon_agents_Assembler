"""The EMA 8/32 cross dataset (app/services/mt5_dataset.py): pure maths, no database.

Synthetic bar series with known crosses, the bad-tick guard, the open-cross exclusion, and a
golden check on real XAUUSD M30 bars (tests/fixtures/mt5/XAUUSD_M30_sample_20260901_20261030.csv,
copied unchanged from the bot repo's tests/data, same feed and UTC times as its labels.py
golden test).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from app.services import mt5_dataset as D

GOLDEN = Path(__file__).parent / "fixtures" / "mt5" / "XAUUSD_M30_sample_20260901_20261030.csv"
T0 = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)  # a Monday


def series(closes: list[float], *, wick: float = 0.5, vol: int | None = 100) -> list[D.Bar]:
    """Bars that open at the previous close, with symmetric half-point wicks."""
    out: list[D.Bar] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(
            D.Bar(T0 + timedelta(minutes=30 * i), o, max(o, c) + wick, min(o, c) - wick, c, vol)
        )
        prev = c
    return out


def zigzag() -> list[float]:
    """Flat (no trend), up (the first trend: not a cross), down (a short cross), up again (a long
    cross that is still open at the end)."""
    flat = [100.0] * 60
    up = [100.0 + i for i in range(1, 41)]
    down = [up[-1] - i for i in range(1, 61)]
    up2 = [down[-1] + i for i in range(1, 41)]
    return flat + up + down + up2


def first_index(pred: list[bool], start: int = 0) -> int:
    return next(i for i in range(start, len(pred)) if pred[i])


# --- building blocks ----------------------------------------------------------------------


def test_ema_matches_pandas_adjust_false() -> None:
    # y0 = x0; y = a*x + (1-a)*y_prev with a = 2/(span+1) = 0.5 for span 3
    assert D.ema([1.0, 2.0, 3.0, 4.0], 3) == [1.0, 1.5, 2.25, 3.125]
    assert D.ema([], 8) == []


def test_wilder_atr_seed_and_smoothing() -> None:
    bars = series([100.0] * 20, wick=0.5)  # every true range is 1.0
    _, _, cleaned, atr = D.clean_and_atr(bars)
    assert atr[:13] == [None] * 13
    assert atr[13] == pytest.approx(1.0)
    assert all(a == pytest.approx(1.0) for a in atr[13:])  # type: ignore[arg-type]
    assert not any(cleaned)


def _one_wick(high: float, low: float) -> tuple[float, float, bool]:
    """Bar 15 of a flat series whose ATR is exactly 1.0 from bar 13 on."""
    bars = series([100.0] * 20, wick=0.5)
    bars[15] = D.Bar(bars[15].ts, 100.0, high, low, 100.0, 100)
    ch, cl, cleaned, _ = D.clean_and_atr(bars)
    return ch[15], cl[15], cleaned[15]


def test_bad_tick_guard_cuts_long_wicks_at_the_body() -> None:
    # lower wick > 2.0 x the previous ATR: ends at the body
    assert _one_wick(100.5, 100.0 - 2.1) == (100.5, 100.0, True)
    assert _one_wick(100.5, 100.0 - 1.9) == (100.5, 98.1, False)
    # upper wick > 2.5 x the previous ATR: ends at the body
    assert _one_wick(100.0 + 2.6, 99.5) == (100.0, 99.5, True)
    assert _one_wick(100.0 + 2.4, 99.5) == (102.4, 99.5, False)
    # both at once
    assert _one_wick(103.0, 97.0) == (100.0, 100.0, True)
    # Before the ATR exists there is nothing to judge a wick against: never cleaned.
    early = series([100.0] * 5)
    early[2] = D.Bar(early[2].ts, 100.0, 150.0, 50.0, 100.0, 100)
    assert not any(D.clean_and_atr(early)[2])


def test_bad_tick_guard_feeds_the_atr_and_the_excursions() -> None:
    closes = zigzag()
    clean = series(closes)
    spiked = list(clean)
    # A one-bar spike 30 points under the body, deep inside the short trend.
    k = 135
    b = spiked[k]
    spiked[k] = D.Bar(b.ts, b.open, b.high, min(b.open, b.close) - 30.0, b.close, b.tick_volume)
    p_clean, p_spiked = D.prepare(clean), D.prepare(spiked)
    assert p_spiked.cleaned[k] and p_spiked.clean_low[k] == min(b.open, b.close)
    assert p_spiked.atr[:k] == p_clean.atr[:k]
    # The 30-point spike never reaches the ATR (its bar ends at the body edge instead).
    assert p_spiked.atr[k] is not None and p_clean.atr[k] is not None
    assert p_spiked.atr[k] <= p_clean.atr[k]
    # ... so the short trade's max favourable excursion ignores the spike too.
    r_clean = D.cross_rows(clean, "X", "M30")
    r_spiked = D.cross_rows(spiked, "X", "M30")
    assert r_spiked[0]["y_max_favourable"] == r_clean[0]["y_max_favourable"]


def test_weekly_vwap_resets_on_monday_utc() -> None:
    sunday = datetime(2026, 11, 8, 23, 0, tzinfo=UTC)
    bars = [
        D.Bar(sunday, 10, 11, 9, 10, 100),  # typical 10
        D.Bar(sunday + timedelta(minutes=30), 10, 13, 11, 12, 300),  # typical 12
        D.Bar(sunday + timedelta(hours=1), 20, 21, 19, 20, 50),  # Monday 00:00: new week
        D.Bar(sunday + timedelta(hours=2), 20, 21, 19, 20, 0),
    ]
    vw = D.weekly_vwap(D.prepare(bars))
    assert vw[0] == pytest.approx(10.0)
    assert vw[1] == pytest.approx((10 * 100 + 12 * 300) / 400)
    assert vw[2] == pytest.approx(20.0) and vw[3] == pytest.approx(20.0)
    assert D.week_start(bars[2].ts) == datetime(2026, 11, 9, tzinfo=UTC)
    no_vol = [D.Bar(sunday, 10, 11, 9, 10, None)]
    assert D.weekly_vwap(D.prepare(no_vol)) == [None]


# --- crosses, features, outcomes ----------------------------------------------------------


def test_synthetic_crosses_features_and_outcomes() -> None:
    closes = zigzag()
    bars = series(closes)
    p = D.prepare(bars)
    gap = p.gap
    down_at = first_index([g < 0 for g in gap], 100)  # the short cross
    up_at = first_index([g > 0 for g in gap], down_at)  # the long cross (open)
    assert D.find_crosses(p) == [(down_at, -1), (up_at, 1)]  # the first trend is not a cross

    rows = D.cross_rows(bars, "XAUUSD", "M30")
    assert len(rows) == 1  # the last cross is still open: excluded
    r = rows[0]
    assert list(r) == list(D.COLUMNS)
    atr = p.atr[down_at]
    assert atr is not None
    g = [-x for x in gap]
    assert r["cross_ts"] == bars[down_at].ts.isoformat()
    assert r["side"] == -1 and r["price"] == closes[down_at]
    assert r["atr"] == pytest.approx(atr, abs=1e-6)
    assert r["gap_atr"] == pytest.approx(g[down_at] / atr, abs=1e-6)
    assert r["above_200"] is (closes[down_at] > p.ema200[down_at])
    assert r["with_ema200_side"] is (closes[down_at] < p.ema200[down_at])
    assert r["hour_utc"] == bars[down_at].ts.hour and r["weekday"] == bars[down_at].ts.weekday()
    assert r["steepness"] == pytest.approx((g[down_at] - g[down_at - 3]) / atr, abs=1e-6)
    assert r["bars_since_prev_cross"] is None  # no earlier cross was seen
    vw = D.weekly_vwap(p)[down_at]
    assert vw is not None and r["with_weekly_vwap"] is (closes[down_at] < vw)
    # outcomes
    assert r["y_bars_to_opposite_cross"] == up_at - down_at
    assert r["y_pts_to_opposite_cross"] == pytest.approx(
        -(closes[up_at] - closes[down_at]), abs=1e-6
    )
    lows = [p.clean_low[j] for j in range(down_at + 1, up_at + 1)]
    highs = [p.clean_high[j] for j in range(down_at + 1, up_at + 1)]
    assert r["y_max_favourable"] == pytest.approx(closes[down_at] - min(lows), abs=1e-6)
    assert r["y_max_adverse"] == pytest.approx(max(0.0, max(highs) - closes[down_at]), abs=1e-6)
    gas = [g[j] / a for j in range(down_at + 1, up_at) if (a := p.atr[j])]
    assert r["y_max_gap_atr"] == pytest.approx(max(gas), abs=1e-6)
    assert r["y_reached_wide_gap"] is (max(gas) >= 2.5)
    assert r["y_widened"] is True  # a steady 1-point/bar fall widens the gap bar after bar
    # features never carry the outcome prefix, outcomes always do
    assert all(not c.startswith("y_") for c in D.FEATURES)
    assert all(c.startswith("y_") for c in D.OUTCOMES)


def test_open_cross_only_with_include_open() -> None:
    bars = series(zigzag())
    rows = D.cross_rows(bars, "X", "M30", include_open=True)
    assert len(rows) == 2 and rows[-1]["side"] == 1
    assert rows[-1]["y_bars_to_opposite_cross"] is None
    assert rows[-1]["y_pts_to_opposite_cross"] is None
    assert rows[-1]["bars_since_prev_cross"] == (
        D.find_crosses(D.prepare(bars))[1][0] - D.find_crosses(D.prepare(bars))[0][0]
    )


def test_date_window_and_too_little_data() -> None:
    bars = series(zigzag())
    (row,) = D.cross_rows(bars, "X", "M30")
    t = datetime.fromisoformat(row["cross_ts"])
    assert D.cross_rows(bars, "X", "M30", date_from=t + timedelta(minutes=1)) == []
    assert D.cross_rows(bars, "X", "M30", date_to=t - timedelta(minutes=1)) == []
    assert len(D.cross_rows(bars, "X", "M30", date_from=t, date_to=t)) == 1
    assert D.cross_rows([], "X", "M30") == []
    assert D.cross_rows(series([100.0] * 10), "X", "M30") == []  # no valid ATR yet
    flat = D.cross_rows(series([100.0] * 300), "X", "M30")
    assert flat == []  # gap 0 everywhere: no trend, no cross


def test_csv_and_jsonl_carry_every_column_in_order() -> None:
    rows = D.cross_rows(series(zigzag()), "XAUUSD", "M30")
    text = D.to_csv(rows)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert list(parsed[0]) == list(D.COLUMNS) and len(parsed) == len(rows)
    assert parsed[0]["side"] == "-1" and parsed[0]["y_widened"] == "true"
    assert parsed[0]["bars_since_prev_cross"] == ""  # None → empty cell
    lines = D.to_jsonl(rows).splitlines()
    assert [list(json.loads(x)) for x in lines] == [list(D.COLUMNS)]
    assert D.to_csv([]) == ",".join(D.COLUMNS) + "\n"


# --- golden: real XAUUSD M30 bars ---------------------------------------------------------


def golden_bars() -> list[D.Bar]:
    with GOLDEN.open() as fh:
        return [
            D.Bar(
                datetime.fromisoformat(r["time_utc"]).replace(tzinfo=UTC),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
            )
            for r in csv.DictReader(fh)
        ]


def test_golden_xauusd_m30_short_cross_29_oct_2026() -> None:
    """The cross on the bar opening 2026-10-29 05:30 UTC (close 4038.1): short, below the EMA
    200, and it reaches the wide gap (2.5 ATR, the bot's WIDE GAP on 30 Oct 17:00).

    The fixture ends on 30 Oct 22:30 while that short trend is still running, so it is the
    open cross: excluded from the dataset, and checked here with include_open (its outcomes so
    far; reaching the wide gap cannot be undone by later bars)."""
    bars = golden_bars()
    when = datetime(2026, 10, 29, 5, 30, tzinfo=UTC)
    completed = D.cross_rows(bars, "XAUUSD", "M30")
    assert all(r["cross_ts"] != when.isoformat() for r in completed)
    rows = D.cross_rows(bars, "XAUUSD", "M30", include_open=True)
    (r,) = [r for r in rows if r["cross_ts"] == when.isoformat()]
    assert r is rows[-1]
    assert r["price"] == pytest.approx(4038.1)
    assert r["side"] == -1
    assert r["above_200"] is False and r["with_ema200_side"] is True
    assert r["y_reached_wide_gap"] is True
    assert r["y_widened"] is True  # the bot's GAP WIDENING on 30 Oct 08:30
    assert r["with_weekly_vwap"] is None  # the fixture has no tick volume
    assert len(completed) == len(rows) - 1


def test_golden_matches_the_bots_cross_list() -> None:
    """76 crosses in the fixture (the bot's labels.run() finds the same 76; checked when this
    module was written). Sides alternate, every completed row's outcome window is consistent."""
    bars = golden_bars()
    crosses = D.find_crosses(D.prepare(bars))
    assert len(crosses) == 76
    assert all(a[1] == -b[1] for a, b in pairwise(crosses))
    rows = D.cross_rows(bars, "XAUUSD", "M30")
    assert len(rows) == 75
    for r, (i, _), (j, _) in zip(rows, crosses, crosses[1:], strict=False):
        assert r["y_bars_to_opposite_cross"] == j - i
        assert r["y_max_favourable"] >= 0 and r["y_max_adverse"] >= 0
