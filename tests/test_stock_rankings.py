"""Stock Signals rankings (docs/37 §3): real % change for movers, time-of-day relative
volume, a bounded and transparent trend score, and a no-lookahead validation."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.agents.research import stock_rankings as rank
from app.services import market_session as ms

STEP = timedelta(minutes=15)
DAYS = [date(2026, 11, 9) + timedelta(days=i) for i in range(4)]


def session(d: date, n: int = 25) -> list:
    lo, _ = ms.session_bounds(d)
    return [lo + i * STEP for i in range(n)]


def frame(times: list, closes: list[float], volume: list[float] | None = None) -> pd.DataFrame:
    c = np.asarray(closes, dtype=float)
    v = np.asarray(volume if volume is not None else [1000.0] * len(c), dtype=float)
    return pd.DataFrame(
        {"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": v},
        index=pd.DatetimeIndex(times, name="ts"),
    )


def test_change_pct_is_from_the_previous_sessions_last_close() -> None:
    df = frame(session(DAYS[0]) + session(DAYS[1], 3), [100.0] * 24 + [200.0] + [210, 205, 220])
    assert rank.change_pct(df) == pytest.approx(10.0)  # 220 vs 200, not vs 100
    assert rank.change_pct(frame(session(DAYS[0]), [100.0] * 25)) is None  # no prior session


def test_relative_volume_compares_the_same_bars_of_earlier_sessions() -> None:
    # Two earlier sessions trade 100 per bar; today's first 4 bars trade 300 each.
    times = session(DAYS[0]) + session(DAYS[1]) + session(DAYS[2], 4)
    vol = [100.0] * 50 + [300.0] * 4
    df = frame(times, [100.0] * len(times), vol)
    assert rank.relative_volume(df) == pytest.approx(3.0)
    assert rank.relative_volume(frame(session(DAYS[0], 4), [1.0] * 4)) is None


def trending(up: bool) -> pd.DataFrame:
    times = session(DAYS[0]) + session(DAYS[1]) + session(DAYS[2]) + session(DAYS[3], 10)
    n = len(times)
    closes = np.linspace(100, 160, n) if up else np.linspace(160, 100, n)
    return frame(times, list(closes), [1000.0] * (n - 10) + [3000.0] * 10)


def scored(df: pd.DataFrame, **kw: object) -> rank.Features:
    from app.agents.research import indicators as ind

    ef = float(ind.ema(df["close"], 8).iloc[-1])
    es = float(ind.ema(df["close"], 32).iloc[-1])
    vw = float(ind.vwap(df, rank.TZ).iloc[-1])
    return rank.features(df, ema_fast=ef, ema_slow=es, vwap=vw, **kw)  # type: ignore[arg-type]


def test_score_is_bounded_and_points_the_right_way() -> None:
    up, down = scored(trending(True)), scored(trending(False))
    assert up.score is not None and down.score is not None
    assert 50 < up.score <= 100 and 0 <= down.score < 50
    assert up.components["volume"] > 0 > down.components["volume"]  # volume confirms, signed
    assert up.missing == ("relative",)  # no Nifty series passed
    j = up.as_json()
    assert j["change_pct"] is not None and j["missing"] == ["relative"]


def test_relative_strength_against_the_benchmark() -> None:
    stock = trending(True)
    flat = frame(list(stock.index), [100.0] * len(stock))
    f = scored(stock, nifty=flat)
    assert f.relative_pct == pytest.approx(f.change_pct) and "relative" not in f.missing


def test_weights_are_configurable_and_renormalised() -> None:
    df = trending(True)
    only_ema = scored(df, weights={"ema": 1.0})
    assert only_ema.score == pytest.approx(round(50 + 50 * only_ema.components["ema"], 2))
    # A feature that cannot be computed does not drag the score toward 50.
    with_missing = scored(df, weights={"ema": 1.0, "relative": 5.0})
    assert with_missing.score == only_ema.score


def test_score_series_never_reads_the_future() -> None:
    df = trending(True)
    base = dict(rank.score_series(df.iloc[:80], 8, 32, rank.DEFAULT_WEIGHTS, start=70))
    changed = df.copy()
    changed.iloc[80:, changed.columns.get_loc("close")] = 1.0  # rewrite the future
    again = dict(rank.score_series(changed, 8, 32, rank.DEFAULT_WEIGHTS, start=70))
    assert all(again[i] == base[i] for i in base)


def test_buckets_and_forward_returns() -> None:
    df = frame(session(DAYS[0], 5), [100.0, 101, 102, 103, 110])
    assert rank.forward_return(df, 0, 4) == pytest.approx(10.0)
    assert rank.forward_return(df, 2, 4) is None
    got = rank.buckets([(20.0, -1.0), (25.0, 1.0), (80.0, 2.0), (100.0, 4.0)])
    low, top = got[0], got[-1]
    assert (low.n, low.mean_forward_pct, low.hit_rate) == (2, 0.0, 0.5)
    assert (top.n, top.mean_forward_pct, top.hit_rate) == (2, 3.0, 1.0)
    assert got[2].n == 0
