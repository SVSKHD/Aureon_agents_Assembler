"""R3.0 on every closed M30 bar: V1.6.3 aureon/detector.py on the Assembler.

Live and replay share indicators.compute()/signal_at() (copied verbatim): the same rule, on
closed bars only, chronological, no lookahead. Signals land in aureon.signals (source 'live'
or 'replay'), where the guard reads them (GET /api/v1/ingest/recent_signal) and the alerter
queues them. Nothing here talks to MT5 or places an order.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.aureon import config, db, indicators

TF: str = str(config.RULES["tf"])
RV = config.RULES_VERSION
LOOKBACK = 2500  # bars loaded for live detection; EMA200 is converged well inside this


def _naive(x: Any) -> Any:
    t = pd.Timestamp(x)
    return t.tz_convert("UTC").tz_localize(None) if t.tzinfo else t


async def load_bars(
    s: AsyncSession,
    tf: str = TF,
    start: Any = None,
    end: Any = None,
    limit: int | None = None,
) -> pd.DataFrame:
    sql = (
        "select time_utc,open,high,low,close,tick_volume from aureon.bars "
        "where symbol=:sym and tf=:tf"
    )
    p: dict[str, Any] = {"sym": config.SYMBOL, "tf": tf}
    if start is not None:
        sql += " and time_utc>=:start"
        p["start"] = db.utc(pd.Timestamp(start).to_pydatetime())
    if end is not None:
        sql += " and time_utc<=:end"
        p["end"] = db.utc(pd.Timestamp(end).to_pydatetime())
    sql += " order by time_utc" + (f" desc limit {int(limit)}" if limit else "")
    rows = await db.q(s, sql, p)
    df = pd.DataFrame(rows, columns=["time_utc", "open", "high", "low", "close", "tick_volume"])
    if limit:
        df = df.iloc[::-1].reset_index(drop=True)
    if not df.empty:
        df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True).dt.tz_localize(None)
        for c in ("open", "high", "low", "close"):
            df[c] = df[c].astype(float)
        df["tick_volume"] = df["tick_volume"].astype("int64")
    return df


def _ind_row(d: pd.DataFrame, i: int) -> dict[str, Any]:
    r = d.iloc[i]
    return {
        "sym": config.SYMBOL, "tf": TF, "t": db.utc(pd.Timestamp(r["time_utc"]).to_pydatetime()),
        "rv": RV, "e8": float(r["ema8"]), "e32": float(r["ema32"]), "e200": float(r["ema200"]),
        "atr": float(r["atr14"]), "vwap": float(r["vwap"]), "bod": int(r["bar_of_day"]),
        "side": int(r["vwap_side"]), "flips": int(r["vwap_flips8"]),
        "gap": None if pd.isna(r["gap_atr"]) else float(r["gap_atr"]),
    }  # fmt: skip


async def persist_indicators(s: AsyncSession, d: pd.DataFrame, idx: list[int]) -> None:
    if not idx:
        return
    await s.execute(
        text(
            """insert into aureon.indicators(symbol,tf,time_utc,rules_version,ema8,ema32,ema200,
               atr14,vwap,bar_of_day,vwap_side,vwap_flips8,gap_atr)
               values(:sym,:tf,:t,:rv,:e8,:e32,:e200,:atr,:vwap,:bod,:side,:flips,:gap)
               on conflict do nothing"""
        ),
        [_ind_row(d, i) for i in idx],
    )


async def persist_signal(
    s: AsyncSession, d: pd.DataFrame, idx: int, sig: dict[str, Any], source: str
) -> int | None:
    r = d.iloc[idx]
    row = await db.q(
        s,
        """insert into aureon.signals(symbol,tf,time_utc,rules_version,source,kind,dir,price,atr,
           suggested_sl,suggested_tp,features)
           values(:sym,:tf,:t,:rv,:src,:kind,:dir,:price,:atr,:sl,:tp,cast(:f as jsonb))
           on conflict(symbol,tf,time_utc,kind,rules_version,source) do nothing returning id""",
        {"sym": config.SYMBOL, "tf": TF, "t": db.utc(pd.Timestamp(r["time_utc"]).to_pydatetime()),
         "rv": RV, "src": source, "kind": sig["kind"], "dir": sig["dir"], "price": sig["price"],
         "atr": sig["atr"], "sl": sig["suggested_sl"], "tp": sig["suggested_tp"],
         "f": json.dumps(sig["features"])},
        one=True,
    )  # fmt: skip
    return int(row["id"]) if row else None


async def replay(s: AsyncSession, start: Any, end: Any) -> dict[str, int]:
    """Deterministic re-run over history (full history so the EMA state is exact)."""
    await db.ensure_rules_version(s)
    df = await load_bars(s, end=end)
    if df.empty:
        return {"signals": 0, "tradeable": 0}
    d = indicators.compute(df)
    sel = d.index[d["time_utc"] >= _naive(start)]
    if len(sel) == 0:
        return {"signals": 0, "tradeable": 0}
    i0 = int(sel[0])
    await persist_indicators(s, d, list(range(i0, len(d))))
    n_sig = n_trade = 0
    for i in range(i0, len(d)):
        sig = indicators.signal_at(d, i)
        if sig:
            await persist_signal(s, d, i, sig, "replay")
            n_sig += 1
            n_trade += int(sig["features"]["tradeable"])
    return {"signals": n_sig, "tradeable": n_trade}


async def live_once(s: AsyncSession) -> list[int]:
    """Evaluate EVERY closed bar not yet in indicators (a polling gap cannot skip a signal).
    Returns the ids of new live signals."""
    df = await load_bars(s, limit=LOOKBACK)
    if df.empty:
        return []
    last = (
        await db.q(
            s,
            """select max(time_utc) t from aureon.indicators
               where symbol=:sym and tf=:tf and rules_version=:rv""",
            {"sym": config.SYMBOL, "tf": TF, "rv": RV},
            one=True,
        )
    )["t"]
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    if last is None:  # first run: a proper full-history replay once, not a partial catch-up
        first = (
            await db.q(
                s,
                "select min(time_utc) t from aureon.bars where symbol=:sym and tf=:tf",
                {"sym": config.SYMBOL, "tf": TF},
                one=True,
            )
        )["t"]
        await replay(s, first, now)
        return []
    last_n = _naive(last)
    if df.iloc[0]["time_utc"] >= last_n:  # gap bigger than the live window: replay exactly
        await replay(s, last_n, now)
        return []
    d = indicators.compute(df)
    todo = [i for i in range(len(d)) if d.iloc[i]["time_utc"] > last_n]
    await persist_indicators(s, d, todo)
    new: list[int] = []
    for i in todo:
        sig = indicators.signal_at(d, i)
        if sig:
            sid = await persist_signal(s, d, i, sig, "live")
            if sid:
                new.append(sid)
    return new


async def verify_last_days(s: AsyncSession, days: int = 7) -> list[dict[str, Any]]:
    """Nightly: replay the last N days into source='replay' and list live signals that the
    replay does not reproduce (mismatches)."""
    end = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    start = end - dt.timedelta(days=days)
    await replay(s, start, end)
    rows: list[dict[str, Any]] = await db.q(
        s,
        """select l.id, l.time_utc from aureon.signals l left join aureon.signals r
           on r.symbol=l.symbol and r.tf=l.tf and r.time_utc=l.time_utc and r.kind=l.kind
           and r.rules_version=l.rules_version and r.source='replay'
           where l.source='live' and l.time_utc>=:s and r.id is null""",
        {"s": db.utc(start)},
    )
    return rows
