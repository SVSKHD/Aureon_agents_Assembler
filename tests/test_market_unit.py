"""Market terminal, no database: indicators (agreeing with ASTRA-FRONTEND's formulas), the
screener engine (crossover edges, confirmed vs live candles, no lookahead, missing data) and
request validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from app.agents.research import indicators as ind
from app.agents.research import screener as eng
from app.agents.trading.dhan.parse import IST
from app.agents.trading.types import Timeframe
from app.schemas.market import ScreenerRequest
from app.services import screener as svc

# The frontend's own fixture (src/trading/indicators.test.ts).
CLOSE = [
    10, 11, 12, 11, 13, 14, 13, 15, 16, 15, 17, 18, 17, 19, 20, 19, 21, 22, 21, 23, 24, 23, 25,
    26, 25, 27, 28, 27, 29, 30, 29, 31, 32, 31, 33, 34, 33, 35, 36, 35,
]  # fmt: skip
STEP = timedelta(minutes=15)
T0 = datetime(2026, 11, 9, 3, 45, tzinfo=UTC)  # 09:15 IST


def df_of(close: list[float], high: list[float] | None = None, low: list[float] | None = None):
    idx = pd.DatetimeIndex([T0 + STEP * i for i in range(len(close))])
    c = pd.Series(close, index=idx, dtype=float)
    return pd.DataFrame(
        {
            "open": c,
            "high": pd.Series(high if high else [x + 1 for x in close], index=idx, dtype=float),
            "low": pd.Series(low if low else [x - 1.5 for x in close], index=idx, dtype=float),
            "close": c,
            "volume": pd.Series([100 + 10 * i for i in range(len(close))], index=idx, dtype=float),
        }
    )


# --- indicators --------------------------------------------------------------------------


def test_rsi_matches_frontend_vectors() -> None:
    r = ind.rsi(pd.Series([10.0, 11, 12, 11, 13]), 3)
    assert math.isnan(r[2])
    assert r[3] == pytest.approx(66.6667, abs=1e-3)
    assert r[4] == pytest.approx(83.3333, abs=1e-3)
    assert ind.rsi(pd.Series([1.0, 2, 3, 4]), 3)[3] == 100
    assert ind.rsi(pd.Series([5.0, 5, 5, 5]), 3)[3] == 50


def test_adx_matches_frontend_vectors() -> None:
    df = pd.DataFrame(
        {
            "high": [11.0, 12, 13, 14, 15, 16, 17],
            "low": [9.0, 10, 11, 12, 13, 14, 15],
            "close": [10.0, 11, 12, 13, 14, 15, 16],
        }
    )
    a = ind.adx(df, 3)
    assert a["plus_di"][3] == pytest.approx(50, abs=1e-10)
    assert a["minus_di"][3] == 0
    assert a["adx"][5] == pytest.approx(100, abs=1e-10)
    full = ind.adx(df_of(CLOSE), 3)["adx"]
    assert full.iloc[:5].isna().all() and not math.isnan(full.iloc[5])  # first ADX at 2n - 1


def test_existing_formulas_still_match_frontend() -> None:
    d = df_of(CLOSE)
    assert ind.atr(d, 5).iloc[39] == pytest.approx(2.6311049812, abs=1e-8)
    assert ind.macd(d["close"], 3, 6, 4)["signal"].iloc[39] == pytest.approx(0.9621796527, abs=1e-8)
    v = ind.vwap(d, "Asia/Kolkata")
    assert v.iloc[0] == pytest.approx(9.8333333333, abs=1e-8)
    assert v.iloc[39] == pytest.approx(25.8299435028, abs=1e-8)


def test_sma_and_bollinger() -> None:
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert list(ind.sma(s, 3).iloc[2:]) == [2.0, 3.0, 4.0]
    bb = ind.bollinger(s, 3, 2)
    sd = np.std([3.0, 4, 5])
    assert bb["upper"].iloc[4] == pytest.approx(4 + 2 * sd)


@pytest.mark.parametrize(
    ("text", "name", "warmup"),
    [
        ("ema:8", "ema:8", 8),
        ("EMA:200", "ema:200", 200),
        ("rsi:14", "rsi:14", 15),
        ("adx:14", "adx:14", 28),
        ("macd:12:26:9", "macd:12:26:9", 34),
        ("bb:20:2", "bb:20:2", 20),
        ("bb:20:2.5", "bb:20:2.5", 20),
        ("vwap", "vwap", 1),
    ],
)
def test_parse_spec(text: str, name: str, warmup: int) -> None:
    s = ind.parse_spec(text)
    assert (s.name, s.warmup) == (name, warmup)


@pytest.mark.parametrize(
    "bad", ["ema", "ema:0", "ema:501", "ema:x", "macd:26:12:9", "foo:1", "vwap:1", "bb:20:0"]
)
def test_parse_spec_rejects(bad: str) -> None:
    with pytest.raises(ind.SpecError):
        ind.parse_spec(bad)


def test_indicators_never_look_ahead() -> None:
    rng = np.random.default_rng(7)
    close = list(100 + np.cumsum(rng.normal(0, 1, 120)))
    d = df_of(close)
    short = df_of(close[:80])
    for spec in ("ema:8", "ema:32", "rsi:14", "adx:14", "macd:12:26:9", "vwap", "bb:20:2"):
        p = ind.parse_spec(spec)
        a = ind.compute_spec(d, p, "Asia/Kolkata").iloc[:80]
        b = ind.compute_spec(short, p, "Asia/Kolkata")
        pd.testing.assert_frame_equal(a, b, check_freq=False)


# --- crossover edges ----------------------------------------------------------------------


def arr(*v: float) -> np.ndarray:
    return np.array(v, dtype=float)


def test_cross_basic_and_window() -> None:
    assert eng.find_cross(arr(-2, -1, 1), 1, "bullish") == 2
    assert eng.find_cross(arr(-2, -1, 1, 2), 1, "bullish") is None  # crossed 2 bars ago
    assert eng.find_cross(arr(-2, -1, 1, 2), 2, "bullish") == 2
    assert eng.find_cross(arr(2, 1, -1), 1, "bearish") == 2
    assert eng.find_cross(arr(-2, -1, 1), 1, "bearish") is None


def test_cross_must_still_hold_on_the_last_bar() -> None:
    # Crossed up, then back down: no bullish cross "within 3 bars" any more.
    assert eng.find_cross(arr(-1, 1, -1), 3, "bullish") is None


def test_touch_at_zero_is_not_a_cross() -> None:
    assert eng.find_cross(arr(1, 0, 1), 3, "bullish") is None
    assert eng.find_cross(arr(-1, 0, -1), 3, "bearish") is None
    # Passing through exactly zero is one cross, on the bar that leaves zero.
    assert eng.find_cross(arr(-1, 0, 1), 3, "bullish") == 2
    # From a flat start (no earlier side) there was nothing to cross.
    assert eng.find_cross(arr(0, 0, 1), 3, "bullish") is None


def test_cross_ignores_undefined_values() -> None:
    assert eng.find_cross(arr(np.nan, 1), 2, "bullish") is None
    assert eng.find_cross(arr(np.nan, -1, 1), 2, "bullish") == 2


# --- the engine ---------------------------------------------------------------------------


@dataclass
class Row:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def rows_of(close: list[float], start: datetime = T0, volume: float = 1000) -> list[Row]:
    out = []
    for i, c in enumerate(close):
        d = Decimal(str(round(c, 4)))
        out.append(Row(start + STEP * i, d, d + 1, d - 1, d, Decimal(str(volume))))
    return out


def cross_up_close(n_down: int = 60) -> list[float]:
    """Falls, then rises until EMA 8 crosses above EMA 32 on the last bar."""
    close = [200.0 - i for i in range(n_down)]
    while True:
        close.append(close[-1] + 4)
        e8 = ind.ema(pd.Series(close), 8).to_numpy()
        e32 = ind.ema(pd.Series(close), 32).to_numpy()
        if eng.find_cross(e8 - e32, 1, "bullish") == len(close) - 1:
            return close


def run(rows: list[Row], filters: list[dict[str, Any]], *, mode: str = "confirmed", now=None):
    req = ScreenerRequest.model_validate(
        {"universe": {"exchange": ["NSE"], "kind": ["equity"]}, "timeframe": "15m",
         "candle": mode, "filters": filters}
    )  # fmt: skip
    now = now or rows[-1].ts + STEP
    return eng.evaluate(
        rows, req.filters, step=STEP, mode=req.candle, now=now, day_tz=IST, vwap_tz="Asia/Kolkata"
    )


EMA_8_32 = {"type": "ema_cross", "fast": 8, "slow": 32, "direction": "bullish", "within_bars": 1}


def test_ema_8_32_cross_matches_with_crossed_at_and_values() -> None:
    rows = rows_of(cross_up_close())
    ev = run(rows, [EMA_8_32])
    assert ev.outcome == "match" and ev.signal == "bullish_cross"
    assert ev.crossed_at == rows[-1].ts and ev.candle_ts == rows[-1].ts and ev.candle_closed
    df = ind.frame(rows)
    assert ev.indicators["ema:8"] == ind.to_dec(float(ind.ema(df["close"], 8).iloc[-1]))
    assert Decimal(ev.indicators["ema:8"]) > Decimal(ev.indicators["ema:32"])
    # Bearish on the same data: no match.
    assert run(rows, [{**EMA_8_32, "direction": "bearish"}]).outcome == "no_match"


def test_within_bars_window() -> None:
    close = cross_up_close()
    rows = rows_of([*close, close[-1] + 4, close[-1] + 8])  # 2 more bars above
    assert run(rows, [EMA_8_32]).outcome == "no_match"
    assert run(rows, [{**EMA_8_32, "within_bars": 3}]).outcome == "match"


def test_confirmed_drops_the_forming_candle_live_keeps_it() -> None:
    close = cross_up_close()
    closed = rows_of(close[:-1])
    forming = rows_of(close)  # last bar is still forming at `now`
    now = forming[-1].ts + STEP / 2
    confirmed = run(forming, [EMA_8_32], mode="confirmed", now=now)
    assert confirmed.outcome == "no_match" and confirmed.candle_ts == closed[-1].ts
    live = run(forming, [EMA_8_32], mode="live", now=now)
    assert live.outcome == "match" and live.candle_closed is False


def test_missing_and_insufficient_data() -> None:
    no_rows = eng.evaluate(
        [], [], step=STEP, mode="confirmed", now=T0, day_tz=IST, vwap_tz="Asia/Kolkata"
    )
    assert no_rows.outcome == "no_data"
    ev = run(rows_of([100.0] * 32), [EMA_8_32])  # needs slow + within = 33
    assert ev.outcome == "insufficient_data" and "need 33" in (ev.detail or "")
    # Only a forming bar: nothing confirmed.
    only_forming = rows_of([100.0])
    assert run(only_forming, [EMA_8_32], now=T0 + STEP / 2).outcome == "no_data"


def test_volume_filter_and_no_volume_instruments() -> None:
    close = [100.0 + (i % 3) for i in range(30)]
    rows = rows_of(close)
    rows[-1].volume = Decimal(5000)
    f = {"type": "volume", "op": ">", "multiple_of_avg": 2, "avg_period": 20}
    ev = run(rows, [f])
    assert ev.outcome == "match"
    assert ev.indicators["volume_avg:20"] == "1000" and ev.indicators["volume"] == "5000"
    idx = rows_of(close, volume=0)  # indices report no volume
    assert run(idx, [f]).outcome == "insufficient_data"
    assert run(idx, [{"type": "vwap_position", "relation": "above"}]).outcome == (
        "insufficient_data"
    )


def test_rsi_macd_adx_price_and_vwap_filters() -> None:
    close = cross_up_close()
    rows = rows_of(close)
    df = ind.frame(rows)
    r = float(ind.rsi(df["close"], 14).iloc[-1])
    assert run(rows, [{"type": "rsi", "period": 14, "op": ">", "value": r - 1}]).outcome == "match"
    assert run(rows, [{"type": "rsi", "period": 14, "op": "<", "value": r - 1}]).outcome == (
        "no_match"
    )
    a = float(ind.adx(df, 14)["adx"].iloc[-1])
    assert run(rows, [{"type": "adx", "period": 14, "op": ">", "value": a - 1}]).outcome == "match"
    ev = run(rows, [{"type": "price_vs_ema", "period": 32, "relation": "above"}])
    assert ev.outcome == "match" and ev.signal == "above"
    ev = run(rows, [{"type": "ema_position", "fast": 8, "slow": 32, "relation": "above"}])
    assert ev.outcome == "match"
    ev = run(rows, [{"type": "vwap_position", "relation": "above"}])
    assert ev.outcome in ("match", "no_match") and ev.indicators["vwap"] is not None
    m = ind.macd(df["close"])
    diff = (m["macd"] - m["signal"]).to_numpy()
    want = eng.find_cross(diff, 20, "bullish") is not None
    ev = run(
        rows,
        [{"type": "macd_cross", "fast": 12, "slow": 26, "signal": 9, "direction": "bullish",
          "within_bars": 20}],
    )  # fmt: skip
    assert (ev.outcome == "match") == want
    assert "macd_hist:12:26:9" in ev.indicators


def test_filters_are_anded() -> None:
    rows = rows_of(cross_up_close())
    ok = run(rows, [EMA_8_32, {"type": "rsi", "period": 14, "op": ">", "value": 0}])
    no = run(rows, [EMA_8_32, {"type": "rsi", "period": 14, "op": "<", "value": 0}])
    assert (ok.outcome, no.outcome) == ("match", "no_match")


def test_change_pct_is_against_previous_day_close() -> None:
    day1 = rows_of([100.0] * 25)
    day2 = rows_of([110.0] * 10, start=T0 + timedelta(days=1))
    ev = run(day1 + day2, [{"type": "price_vs_ema", "period": 5, "relation": "above"}])
    assert ev.change_pct == Decimal("10.0000")
    one_day = run(day2, [{"type": "price_vs_ema", "period": 5, "relation": "above"}])
    assert one_day.change_pct is None  # no previous day loaded: unknown, not 0


# --- request validation -------------------------------------------------------------------


BASE: dict[str, Any] = {
    "universe": {"exchange": ["NSE"], "kind": ["equity"], "watchlist_id": None},
    "timeframe": "15m",
    "candle": "confirmed",
    "filters": [EMA_8_32],
    "sort": {"field": "change_pct", "dir": "desc"},
    "limit": 50,
    "offset": 0,
}


def test_frontend_default_request_is_valid() -> None:
    req = ScreenerRequest.model_validate(BASE)
    assert req.filters[0].type == "ema_cross"


@pytest.mark.parametrize(
    "patch",
    [
        {"filters": [{**EMA_8_32, "fast": 32, "slow": 8}]},
        {"filters": [{**EMA_8_32, "fast": 0}]},
        {"filters": [{**EMA_8_32, "within_bars": 101}]},
        {"filters": [{"type": "rsi", "period": 14, "op": "<", "value": 101}]},
        {"filters": [{"type": "volume", "op": ">", "multiple_of_avg": 0, "avg_period": 20}]},
        {"filters": [{"type": "nope"}]},
        {"filters": []},
        {"filters": [EMA_8_32] * 13},
        {"timeframe": "2h"},
        {"candle": "maybe"},
        {"limit": 501},
        {"limit": 0},
        {"offset": -1},
        {"universe": {"exchange": ["NYSE"], "kind": ["equity"]}},
        {"universe": {"exchange": [], "kind": ["equity"]}},
        {"universe": {"exchange": ["NSE"], "kind": [], "watchlist_id": None}},
        {"universe": {"exchange": [], "kind": [], "watchlist_id": "device-list-1"}},
        {"timeframe": "1d", "filters": [{"type": "vwap_position", "relation": "above"}]},
        {"sort": {"field": "volume", "dir": "desc"}},
        {"extra": 1},
    ],
)
def test_invalid_requests(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ScreenerRequest.model_validate({**BASE, **patch})


def test_request_hash_ignores_paging_and_tracks_the_bar() -> None:
    now = datetime(2026, 11, 10, 5, 1, tzinfo=UTC)
    a = ScreenerRequest.model_validate(BASE)
    b = ScreenerRequest.model_validate({**BASE, "limit": 10, "offset": 20, "sort": {"dir": "asc"}})
    assert svc.request_hash(a, now) == svc.request_hash(b, now)
    assert svc.request_hash(a, now) != svc.request_hash(a, now + timedelta(minutes=15))
    live = ScreenerRequest.model_validate({**BASE, "candle": "live"})
    assert svc.request_hash(live, now) == svc.request_hash(live, now + timedelta(minutes=15))


def test_unsupported_timeframes_are_refused() -> None:
    for tf in ("30m", "4h"):
        with pytest.raises(svc.ScreenerError) as e:
            svc.check_supported(ScreenerRequest.model_validate({**BASE, "timeframe": tf}))
        assert e.value.status == 422 and e.value.code == "timeframe_unsupported"
    svc.check_supported(ScreenerRequest.model_validate({**BASE, "timeframe": Timeframe.D1}))
