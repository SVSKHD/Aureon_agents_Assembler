"""Research agents, pure parts: indicator values on known fixtures, no-lookahead, strategies,
backtest execution and metrics, tick aggregation (no DB)."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from app.agents.research import backtest as bt
from app.agents.research import indicators as ind
from app.agents.research import strategies as st
from app.services.research import TickAggregator, grid_params

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


@dataclass(frozen=True)
class C:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(1)


def candles(closes: list[float], step: timedelta = timedelta(minutes=5)) -> list[C]:
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append(
            C(
                T0 + step * i,
                Decimal(str(o)),
                Decimal(str(max(o, c) + 0.5)),
                Decimal(str(min(o, c) - 0.5)),
                Decimal(str(c)),
            )
        )
        prev = c
    return out


def walk(n: int, seed: int = 7) -> list[C]:
    rnd = random.Random(seed)
    x, xs = 100.0, []
    for _ in range(n):
        x = round(x + rnd.uniform(-1, 1), 2)
        xs.append(x)
    return candles(xs)


# --- indicators on hand-computed fixtures --------------------------------------------------


def test_ema_seeded_with_sma() -> None:
    s = pd.Series([10.0, 11, 12, 11, 10, 12])
    got = ind.ema(s, 3).tolist()
    # seed = mean(10,11,12) = 11; alpha = 0.5
    assert math.isnan(got[0]) and math.isnan(got[1])
    assert got[2:] == [11.0, 11.0, 10.5, 11.25]


def test_macd_matches_its_definition() -> None:
    s = pd.Series([float(x) for x in [1, 2, 3, 4, 5, 6, 7, 8]])
    m = ind.macd(s, 2, 3, 2)
    # EMA2 seed at i=1: 1.5 then +(x-prev)*2/3; EMA3 seed at i=2: 2 then +(x-prev)/2
    e2 = [math.nan, 1.5]
    for x in [3, 4, 5, 6, 7, 8]:
        e2.append(e2[-1] + (x - e2[-1]) * 2 / 3)
    e3 = [math.nan, math.nan, 2.0]
    for x in [4, 5, 6, 7, 8]:
        e3.append(e3[-1] + (x - e3[-1]) / 2)
    line = [a - b for a, b in zip(e2, e3, strict=True)]
    assert np.allclose(m["macd"].to_numpy()[2:], line[2:])
    sig_seed = (line[2] + line[3]) / 2  # EMA2 of MACD, seeded on its first 2 values
    assert m["signal"].iloc[3] == pytest.approx(sig_seed)
    assert m["hist"].iloc[3] == pytest.approx(line[3] - sig_seed)


def test_atr_wilder() -> None:
    df = pd.DataFrame(
        {
            "high": [10.0, 11, 12, 11],
            "low": [9.0, 10, 10, 9],
            "close": [9.5, 10.5, 11.5, 9.5],
            "open": [9.0, 9.5, 10.5, 11.5],
            "volume": [1.0] * 4,
        }
    )
    # TR: 1, max(1, 1.5, .5)=1.5, max(2, 1.5, .5)=2, max(2, .5, 2.5)=2.5
    a = ind.atr(df, 3)
    assert a.iloc[2] == pytest.approx((1 + 1.5 + 2) / 3)
    assert a.iloc[3] == pytest.approx((1.5 * 2 + 2.5) / 3)


def test_vwap_resets_each_day() -> None:
    idx = pd.DatetimeIndex(
        [
            datetime(2026, 11, 2, 10, tzinfo=UTC),
            datetime(2026, 11, 2, 11, tzinfo=UTC),
            datetime(2026, 11, 3, 10, tzinfo=UTC),
        ]
    )
    df = pd.DataFrame(
        {
            "high": [12.0, 15, 30],
            "low": [9.0, 12, 27],
            "close": [9.0, 12, 30],
            "volume": [1.0, 3, 2],
        },
        index=idx,
    )
    v = ind.vwap(df, "UTC")
    # tp: 10, 13, 29
    assert v.iloc[0] == pytest.approx(10)
    assert v.iloc[1] == pytest.approx((10 * 1 + 13 * 3) / 4)
    assert v.iloc[2] == pytest.approx(29)  # new day


def test_structure_reports_pivots_only_once_confirmed() -> None:
    highs = [1, 2, 5, 2, 1, 2, 6, 2, 1, 2, 3]
    lows = [0, 1, 3, 1, 0.5, 1, 4, 1, 0.8, 1, 2]
    df = pd.DataFrame({"high": [float(h) for h in highs], "low": [float(x) for x in lows]})
    s = ind.structure(df, k=2)
    assert math.isnan(s["swing_high"].iloc[3])  # pivot at 2 is unknown until bar 4
    assert s["swing_high"].iloc[4] == 5
    assert s["swing_high"].iloc[8] == 6
    assert s["swing_low"].iloc[6] == 0.5 and s["swing_low"].iloc[10] == 0.8
    assert s["trend"].iloc[10] == "up"  # higher high (6>5) and higher low (0.8>0.5)


def test_storage_values_are_decimal_strings() -> None:
    rows = ind.rows_for_storage(ind.compute(ind.frame(walk(60)), ind.IndicatorSet()))
    names = {n for n, _, _ in rows}
    assert {"ema:9", "ema:21", "macd:12:26:9", "atr:14", "vwap:UTC", "structure:3"} <= names
    for _, _, values in rows:
        for v in values.values():
            assert v is None or isinstance(v, str)
            if v is not None and v not in ("up", "down", "range"):
                Decimal(v)


# --- no lookahead --------------------------------------------------------------------------


def test_indicators_and_decisions_never_look_ahead() -> None:
    full = walk(250)
    spec = ind.IndicatorSet()
    all_ind = ind.compute(ind.frame(full), spec)
    all_dec = {k: f(ind.frame(full), {"fast": 5, "slow": 13}) for k, f in st.REGISTRY.items()}
    for cut in (40, 77, 151, 249):
        part = full[:cut]
        part_ind = ind.compute(ind.frame(part), spec)
        for name, fr in part_ind.items():
            a = fr.iloc[cut - 1].to_dict()
            b = all_ind[name].iloc[cut - 1].to_dict()
            for k in a:
                if isinstance(a[k], float):
                    assert (math.isnan(a[k]) and math.isnan(b[k])) or a[k] == pytest.approx(b[k])
                else:
                    assert a[k] == b[k]
        for k, f in st.REGISTRY.items():
            d = f(ind.frame(part), {"fast": 5, "slow": 13})
            assert (d.side.to_numpy() == all_dec[k].side.to_numpy()[:cut]).all(), k


def test_backtest_fills_next_bar_and_ignores_the_future() -> None:
    full = walk(300, seed=3)
    costs = bt.Costs()
    params = {"fast": 5, "slow": 13}
    r_full = bt.run(full, st.ema_cross, params, quantity=Decimal(1), costs=costs)
    decisions = st.ema_cross(ind.frame(full), params).side.to_numpy()
    by_ts = {c.ts: i for i, c in enumerate(full)}
    for t in r_full.trades:
        i = by_ts[t.entry_time]
        assert decisions[i - 1] == t.side  # decided on the previous bar's close
        assert t.entry_price == full[i].open  # filled at this bar's open
    # Changing everything after bar 200 cannot change trades that closed before it.
    cut = 200
    future = full[:cut] + [
        C(c.ts, c.open * 2, c.high * 2, c.low * 2, c.close * 2) for c in full[cut:]
    ]
    r_mut = bt.run(future, st.ema_cross, params, quantity=Decimal(1), costs=costs)
    closed_before = [t for t in r_full.trades if t.exit_time and t.exit_time <= full[cut - 1].ts]
    assert closed_before
    assert [(t.entry_time, t.pnl) for t in closed_before] == [
        (t.entry_time, t.pnl) for t in r_mut.trades[: len(closed_before)]
    ]


# --- backtest arithmetic -------------------------------------------------------------------


def _decider(seq: list[str]) -> st.StrategyFn:
    def fn(df: pd.DataFrame, _: dict[str, object]) -> st.Decisions:
        return st.Decisions(pd.Series(seq, index=df.index), pd.DataFrame(index=df.index))

    return fn


def test_backtest_pnl_costs_and_metrics() -> None:
    cs = candles([100, 100, 110, 110, 105, 100, 100])
    # BUY on bar 0 → fill open[1]; SELL on bar 2 → flip at open[3]; BUY on bar 4 → open[5]
    seq = ["BUY", "NO_TRADE", "SELL", "NO_TRADE", "BUY", "NO_TRADE", "NO_TRADE"]
    r = bt.run(cs, _decider(seq), {}, quantity=Decimal(2), costs=bt.Costs())
    o = [c.open for c in cs]
    assert [(t.side, t.entry_price, t.exit_price) for t in r.trades] == [
        ("BUY", o[1], o[3]),
        ("SELL", o[3], o[5]),
        ("BUY", o[5], cs[-1].close),
    ]
    pnl = [(o[3] - o[1]) * 2, (o[3] - o[5]) * 2, (cs[-1].close - o[5]) * 2]
    assert [t.pnl for t in r.trades] == pnl
    assert r.trades[-1].open_at_end
    m = r.metrics
    assert m["trades"] == 3 and m["fill"] == "next bar open"
    wins = [p for p in pnl if p > 0]
    assert m["wins"] == len(wins)
    assert Decimal(m["net_pnl"]) == sum(pnl)

    costly = bt.run(
        cs,
        _decider(seq),
        {},
        quantity=Decimal(2),
        costs=bt.Costs(Decimal(10), Decimal(5), Decimal(3)),
    )
    first = costly.trades[0]
    adverse = Decimal(10) / 2 + Decimal(5)  # bps
    assert first.entry_price == o[1] * (1 + adverse / 10000)
    assert first.fees > 0 and Decimal(costly.metrics["net_pnl"]) < Decimal(m["net_pnl"])
    for t in costly.trades:
        assert isinstance(t.pnl, Decimal) and isinstance(t.fees, Decimal)


def test_metrics_profit_factor_drawdown() -> None:
    trades = [
        bt.Trade("BUY", Decimal(1), T0, Decimal(1), pnl=Decimal(p)) for p in ("10", "-4", "-6", "8")
    ]
    m = bt.metrics(trades, bt.Costs())
    assert m["win_rate"] == "0.5" and m["expectancy"] == "2"
    assert m["profit_factor"] == "1.8"  # 18 / 10
    assert m["max_drawdown"] == "10"  # peak 10 → 0


def test_ema_cross_detects_the_cross() -> None:
    closes = [10.0] * 30 + [11, 12, 13, 14, 15] + [14, 12, 10, 8, 6, 4]
    d = st.ema_cross(ind.frame(candles(closes)), {"fast": 3, "slow": 8}).side.tolist()
    assert d.count("BUY") == 1 and d.count("SELL") == 1
    assert d.index("BUY") == 30 and d.index("SELL") > 35


def test_params_validated() -> None:
    with pytest.raises(ValueError, match="shorter"):
        st.ema_cross(ind.frame(walk(10)), {"fast": 21, "slow": 9})
    with pytest.raises(ValueError, match="positive integer"):
        st.ema_pullback(ind.frame(walk(10)), {"fast": "9"})


def test_grid_expansion_and_cap() -> None:
    combos = grid_params({"slow": 21}, {"fast": [5, 9], "slow": [21, 34]}, cap=10)
    assert len(combos) == 4 and {"fast": 9, "slow": 34} in combos
    with pytest.raises(ValueError, match="max 3"):
        grid_params({}, {"fast": [1, 2], "slow": [3, 4]}, cap=3)


# --- ticks → 1m candles --------------------------------------------------------------------


def test_tick_aggregator_emits_complete_minutes() -> None:
    agg = TickAggregator()

    def p(ts: str, ltp: str, vol: int) -> dict[str, object]:
        return {
            "segment": "NSE_EQ",
            "security_id": "1333",
            "ltp": ltp,
            "ltt": f"2026-11-02T{ts}+00:00",
            "volume": vol,
        }

    assert agg.add(p("04:00:01", "100", 1000)) == []
    assert agg.add(p("04:00:30", "102", 1010)) == []
    assert agg.add(p("04:00:59", "99", 1015)) == []
    out = agg.add(p("04:01:02", "101", 1030))
    assert len(out) == 1
    key, bar = out[0]
    assert key == "NSE_EQ:1333" and bar.ts == datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
    assert (bar.open, bar.high, bar.low, bar.close) == tuple(
        Decimal(x) for x in ("100", "102", "99", "99")
    )
    assert bar.volume == Decimal(15)  # 1000 → 1015 within the minute
    assert agg.add(p("04:00:50", "500", 1031)) == []  # late tick for an emitted minute
    assert agg.add({"segment": "NSE_EQ", "security_id": "1", "ltp": None}) == []
