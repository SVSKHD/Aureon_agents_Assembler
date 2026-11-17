"""Check the trend score against history (docs/37 §3): does a higher score come before
higher returns? Read-only: stored candles in, a table out; nothing is written or traded.

For each instrument in the Stock Signals universe it recomputes the score at every bar from
that bar's own past (no lookahead), takes the % change `horizon` bars later, and groups the
pairs by score band. A useful score shows forward returns rising with the band; a flat or
inverted table means the weights need work. Either way it measures the past and promises
nothing about the future.

On the server:

    docker compose exec api python -m app.services.score_validation --timeframe 15m \
        --horizon 8 --instruments 50
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from typing import Any

from app.agents.research import indicators as ind
from app.agents.research import stock_rankings as rank
from app.agents.trading.types import Timeframe
from app.core.config import Settings
from app.db.session import build_engine, build_sessionmaker
from app.services import stock_signals
from app.services.screener import load_bars


def observations(
    df: Any,
    fast: int,
    slow: int,
    weights: dict[str, float],
    horizon: int,
    warmup: int,
    intraday: bool,
) -> list[tuple[float, float]]:
    out = []
    for i, score in rank.score_series(df, fast, slow, weights, start=warmup, intraday=intraday):
        fwd = rank.forward_return(df, i, horizon)
        if fwd is not None:
            out.append((score, fwd))
    return out


async def validate(
    settings: Settings, tf: Timeframe, horizon: int, instruments: int, bars: int
) -> dict[str, Any]:
    engine = build_engine(settings)
    maker = build_sessionmaker(engine)
    try:
        async with maker() as db:
            ids = (await stock_signals.universe(db, settings))[:instruments]
            got = await load_bars(db, ids, tf, bars, datetime.now(UTC))
    finally:
        await engine.dispose()
    pairs: list[tuple[float, float]] = []
    warmup = 2 * settings.STOCK_SIGNALS_EMA_SLOW
    for rows in got.values():
        if len(rows) > warmup + horizon:
            pairs += await asyncio.to_thread(
                observations,
                ind.frame(rows),
                settings.STOCK_SIGNALS_EMA_FAST,
                settings.STOCK_SIGNALS_EMA_SLOW,
                settings.STOCK_SIGNALS_SCORE_WEIGHTS,
                horizon,
                warmup,
                tf is not Timeframe.D1,
            )
    return {
        "timeframe": tf.value,
        "horizon_bars": horizon,
        "instruments": len(got),
        "observations": len(pairs),
        "buckets": rank.buckets(pairs),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--timeframe", default="15m", choices=["5m", "15m", "1h", "1d"])
    p.add_argument("--horizon", type=int, default=8, help="bars ahead (default 8)")
    p.add_argument("--instruments", type=int, default=50)
    p.add_argument("--bars", type=int, default=600, help="stored bars per instrument")
    a = p.parse_args()
    out = asyncio.run(
        validate(Settings(), Timeframe(a.timeframe), a.horizon, a.instruments, a.bars)
    )
    print(
        f"timeframe {out['timeframe']}, {out['horizon_bars']} bars ahead, "
        f"{out['instruments']} instruments, {out['observations']} observations"
    )
    print(f"{'score band':>12} {'n':>8} {'mean fwd %':>11} {'hit rate':>9}")
    for b in out["buckets"]:
        mean = "-" if b.n == 0 else f"{b.mean_forward_pct:+.3f}"
        hit = "-" if b.n == 0 else f"{b.hit_rate:.1%}"
        print(f"{b.lo:>5.0f}-{b.hi:<6.0f} {b.n:>8} {mean:>11} {hit:>9}")
    print("Past behaviour only; the score ranks trend strength and is not a probability of profit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
