"""Deterministic strategies, looked up by trading.strategy_versions.code_ref.

A strategy maps closed candles to one decision per bar: BUY, SELL or NO_TRADE, plus the
inputs it used. Decision `i` depends only on rows `0..i` (tests check this by truncating the
series). Versioning: the parameters live in strategy_versions.params; a change in params is
a new version, so every stored signal says exactly which rules produced it.

params keys:
- algorithm parameters (e.g. "fast", "slow"),
- what to watch: "instrument_ids" (list of trading.instruments ids) and "timeframes".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.agents.research.indicators import ema, to_dec
from app.agents.trading.types import SignalSide

BUY, SELL, NONE = SignalSide.BUY.value, SignalSide.SELL.value, SignalSide.NO_TRADE.value


@dataclass(frozen=True, slots=True)
class Decisions:
    side: pd.Series  # str per bar
    inputs: pd.DataFrame  # float columns per bar

    def inputs_at(self, i: int) -> dict[str, str | None]:
        return {str(k): to_dec(v) for k, v in self.inputs.iloc[i].to_dict().items()}


StrategyFn = Callable[[pd.DataFrame, dict[str, Any]], Decisions]


def _int(params: dict[str, Any], key: str, default: int) -> int:
    v = params.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ValueError(f"param {key} must be a positive integer")
    return v


def ema_cross(df: pd.DataFrame, params: dict[str, Any]) -> Decisions:
    """BUY when EMA(fast) crosses above EMA(slow) on a closed bar; SELL on the cross below."""
    fast_n, slow_n = _int(params, "fast", 9), _int(params, "slow", 21)
    if fast_n >= slow_n:
        raise ValueError("fast must be shorter than slow")
    fast, slow = ema(df["close"], fast_n), ema(df["close"], slow_n)
    diff = fast - slow
    prev = diff.shift(1)
    side = np.where((diff > 0) & (prev <= 0), BUY, np.where((diff < 0) & (prev >= 0), SELL, NONE))
    return Decisions(
        pd.Series(side, index=df.index),
        pd.DataFrame({"close": df["close"], "ema_fast": fast, "ema_slow": slow}),
    )


def ema_pullback(df: pd.DataFrame, params: dict[str, Any]) -> Decisions:
    """Trend = EMA(fast) above (below) EMA(slow) and close above (below) EMA(slow).
    BUY in an up-trend when the bar's low touches EMA(fast) and it closes back above it;
    SELL is the mirror image."""
    fast_n, slow_n = _int(params, "fast", 9), _int(params, "slow", 21)
    if fast_n >= slow_n:
        raise ValueError("fast must be shorter than slow")
    c, lo, hi = df["close"], df["low"], df["high"]
    fast, slow = ema(c, fast_n), ema(c, slow_n)
    up = (fast > slow) & (c > slow)
    down = (fast < slow) & (c < slow)
    buy = up & (lo <= fast) & (c > fast)
    sell = down & (hi >= fast) & (c < fast)
    side = np.where(buy, BUY, np.where(sell, SELL, NONE))
    return Decisions(
        pd.Series(side, index=df.index),
        pd.DataFrame({"close": c, "low": lo, "high": hi, "ema_fast": fast, "ema_slow": slow}),
    )


REGISTRY: dict[str, StrategyFn] = {"ema_cross": ema_cross, "ema_pullback": ema_pullback}


def get(code_ref: str) -> StrategyFn | None:
    return REGISTRY.get(code_ref)


def watch_list(params: dict[str, Any]) -> list[tuple[int, str]]:
    ids = params.get("instrument_ids") or []
    tfs = params.get("timeframes") or []
    return [(int(i), str(t)) for i in ids for t in tfs]


def ema_periods(params: dict[str, Any]) -> set[int]:
    out = set()
    for k in ("fast", "slow"):
        v = params.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            out.add(v)
    return out
