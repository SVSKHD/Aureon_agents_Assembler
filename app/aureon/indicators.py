"""Pure functions. Same code for replay and live — that is the whole point."""
import numpy as np
import pandas as pd
from . import config

R = config.RULES

def compute(df: pd.DataFrame) -> pd.DataFrame:
    """df: columns time_utc, open, high, low, close, tick_volume — sorted ascending, closed bars only.
    Returns df with ema8/ema32/ema200/atr14/vwap/bar_of_day/vwap_side/vwap_flips8/gap_atr."""
    d = df.copy()
    for n, col in ((R["ema_fast"], "ema8"), (R["ema_slow"], "ema32"), (R["ema_trend"], "ema200")):
        d[col] = d["close"].ewm(span=n, adjust=False).mean()
    prev = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - prev).abs(), (d["low"] - prev).abs()], axis=1).max(axis=1)
    d["atr14"] = tr.ewm(alpha=1 / R["atr_len"], adjust=False).mean()
    day = pd.to_datetime(d["time_utc"]).dt.date                     # broker day == UTC date after offset applied
    tp = (d["high"] + d["low"] + d["close"]) / 3
    vol = d["tick_volume"].astype(float).clip(lower=1)
    d["vwap"] = (tp * vol).groupby(day).cumsum() / vol.groupby(day).cumsum()
    d["bar_of_day"] = d.groupby(day).cumcount()
    side = np.sign(d["close"] - d["vwap"]).astype(int)
    d["vwap_side"] = side
    flip = ((side != side.shift()) & (side != 0)).astype(int)
    flip.iloc[0] = 0
    d["vwap_flips8"] = flip.rolling(R["chop_window"], min_periods=1).sum().astype(int)
    d["gap_atr"] = (d["ema8"] - d["ema32"]).abs() / d["atr14"].replace(0, np.nan)
    d["warm"] = np.arange(len(d)) >= config.EMA_WARMUP_BARS                     # deterministic: EMA200 converged after this many bars
    bad = ~((d["low"] <= d[["open", "close"]].min(axis=1)) & (d[["open", "close"]].max(axis=1) <= d["high"]) & (d["low"] > 0) & (d["tick_volume"] >= 0))
    d["valid"] = ~bad
    return d

def signal_at(d: pd.DataFrame, i: int):
    """Evaluate R3.0 on CLOSED bar i (using bars <= i only). Returns dict or None."""
    if i < 2:
        return None
    row, prev = d.iloc[i], d.iloc[i - 1]
    if not (row.get("warm", True) and row.get("valid", True) and prev.get("valid", True)) or not pd.notna(row["atr14"]) or row["atr14"] <= 0:
        return None                                   # warm-up or invalid data: never a tradeable signal
    if row["bar_of_day"] < R["skip_first_bars_of_day"]:
        return None
    if row["vwap_side"] == 0 or row["vwap_side"] == prev["vwap_side"]:
        return None                                   # no VWAP cross on this bar
    direction = int(row["vwap_side"])
    ema_dir = np.sign(row["ema8"] - row["ema32"])
    chop = row["vwap_flips8"] >= R["chop_flips"]
    features = {
        "ema_agree": bool(ema_dir == direction), "chop": bool(chop), "vwap_flips8": int(row["vwap_flips8"]),
        "gap_atr": round(float(row["gap_atr"]), 3) if pd.notna(row["gap_atr"]) else None,
        "side200": int(np.sign(row["close"] - row["ema200"])), "bar_of_day": int(row["bar_of_day"]),
        "hour_utc": int(pd.to_datetime(row["time_utc"]).hour), "atr": round(float(row["atr14"]), 2),
    }
    if not features["ema_agree"] or chop:
        reason = ("ema_disagree" if not features["ema_agree"] else "") + ("+" if (not features["ema_agree"] and chop) else "") + ("chop" if chop else "")
        return {"kind": "vwap_cross", "dir": direction, "price": float(row["close"]), "atr": float(row["atr14"]),
                "suggested_sl": None, "suggested_tp": None, "features": {**features, "tradeable": False, "reject_reason": reason}}
    sl = round(R["sl_atr"] * float(row["atr14"]), 2)
    return {"kind": "vwap_cross", "dir": direction, "price": float(row["close"]), "atr": float(row["atr14"]),
            "suggested_sl": sl, "suggested_tp": None, "features": {**features, "tradeable": True}}

def simulate(d: pd.DataFrame, i: int, direction: int, sl: float, rules=R, tp: float = None):
    """Hypothetical outcome of entering at open of bar i+1 (bar-by-bar on highs/lows).
    Exit: SL / lock ladder, optional TP, opposite VWAP cross, flat bar, or horizon. If the stop (or lock) AND the TP are both inside
    the same bar the ordering is unknown from OHLC: exit_how='AMBIGUOUS', pl_rule=None — never resolved in favour of the TP."""
    k0 = i + 1
    if k0 >= len(d):
        return None
    ent = float(d.iloc[k0]["open"]); stop = -sl; locked_at = None
    mfe = mae = 0.0; sec = 0.0; bars20 = None; dip20 = None; last = 0.0
    for n, k in enumerate(range(k0, min(len(d), k0 + rules["horizon_bars"])), start=1):
        b = d.iloc[k]
        f = (b["high"] - ent) if direction > 0 else (ent - b["low"])
        a = (ent - b["low"]) if direction > 0 else (b["high"] - ent)
        mae = max(mae, a)
        if -a <= stop and tp is not None and f >= tp:
            return dict(pl_rule=None, exit_how="AMBIGUOUS", mfe=max(mfe, f), mae=mae, secured=sec, bars_to_20=bars20, dip_before_20=dip20, bars=n)
        if -a <= stop:
            return dict(pl_rule=stop, exit_how=("lock" if locked_at is not None else "SL"), mfe=mfe, mae=mae,
                        secured=sec, bars_to_20=bars20, dip_before_20=dip20, bars=n)
        if tp is not None and f >= tp:
            return dict(pl_rule=tp, exit_how="TP", mfe=max(mfe, f), mae=mae, secured=sec, bars_to_20=bars20, dip_before_20=dip20, bars=n)
        mfe = max(mfe, f); sec = max(sec, f)
        if bars20 is None and f >= 20:
            bars20 = n; dip20 = mae
        for reach, lock in rules["lock_ladder"]:
            if f >= reach and (locked_at is None or lock > locked_at):
                locked_at = lock; stop = lock
        last = (b["close"] - ent) if direction > 0 else (ent - b["close"])
        if n > 1 and b["vwap_side"] == -direction:
            return dict(pl_rule=last, exit_how="opp_vwap", mfe=mfe, mae=mae, secured=sec, bars_to_20=bars20, dip_before_20=dip20, bars=n)
        if b["bar_of_day"] >= rules["flat_bar_of_day"]:
            return dict(pl_rule=last, exit_how="flat", mfe=mfe, mae=mae, secured=sec, bars_to_20=bars20, dip_before_20=dip20, bars=n)
    return dict(pl_rule=last, exit_how="horizon", mfe=mfe, mae=mae, secured=sec, bars_to_20=bars20, dip_before_20=dip20, bars=n)
