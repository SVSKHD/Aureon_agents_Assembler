"""Runs R3.0 on every closed M30 bar. Live and replay share compute()/signal_at()."""
import json, sys, time, datetime as dt
import pandas as pd
from . import config
from . import db, indicators
from .timeutil import now as _utcnow

TF = config.RULES["tf"]; RV = config.RULES_VERSION
LOOKBACK = 2500          # bars loaded for live detection; EMA200 is converged well inside this

def load_bars(tf=TF, start=None, end=None, limit=None):
    sql = "select time_utc,open,high,low,close,tick_volume from bars where symbol=%s and tf=%s"
    p = [config.SYMBOL, tf]
    if start: sql += " and time_utc>=%s"; p.append(start)
    if end:   sql += " and time_utc<=%s"; p.append(end)
    sql += " order by time_utc" + (" desc limit %s" % int(limit) if limit else "")
    rows = db.q(sql, p)
    df = pd.DataFrame(rows)
    if limit: df = df.iloc[::-1].reset_index(drop=True)
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True).dt.tz_localize(None)
    return df

def persist_indicators(d, idx):
    r = d.iloc[idx]
    db.q("""insert into indicators(symbol,tf,time_utc,rules_version,ema8,ema32,ema200,atr14,vwap,bar_of_day,vwap_side,vwap_flips8,gap_atr)
            values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing""",
         (config.SYMBOL, TF, r["time_utc"], RV, r["ema8"], r["ema32"], r["ema200"], r["atr14"], r["vwap"],
          int(r["bar_of_day"]), int(r["vwap_side"]), int(r["vwap_flips8"]), None if pd.isna(r["gap_atr"]) else float(r["gap_atr"])))

def persist_signal(d, idx, s, source):
    r = d.iloc[idx]
    row = db.q("""insert into signals(symbol,tf,time_utc,rules_version,source,kind,dir,price,atr,suggested_sl,suggested_tp,features)
                  values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  on conflict(symbol,tf,time_utc,kind,rules_version,source) do nothing returning id""",
               (config.SYMBOL, TF, r["time_utc"], RV, source, s["kind"], s["dir"], s["price"], s["atr"],
                s["suggested_sl"], s["suggested_tp"], json.dumps(s["features"])), one=True)
    return row["id"] if row else None

def replay(start, end):
    """Deterministic re-run over history. Must reproduce the research counts."""
    db.ensure_rules_version()
    df = load_bars(start=None, end=end)                 # full history so EMA state is exact
    if df.empty: print("replay: no bars"); return
    d = indicators.compute(df)
    sel = d.index[d["time_utc"] >= pd.Timestamp(start).tz_localize(None) if pd.Timestamp(start).tzinfo else pd.Timestamp(start)]
    if len(sel) == 0: print(f"replay {start}..{end}: start is after the newest stored bar — nothing to do"); return
    i0 = int(sel[0])
    n_sig = n_trade = 0
    for i in range(i0, len(d)):
        persist_indicators(d, i)
        s = indicators.signal_at(d, i)
        if s:
            persist_signal(d, i, s, "replay"); n_sig += 1; n_trade += int(s["features"]["tradeable"])
    print(f"replay {start}..{end}: {n_sig} vwap crosses, {n_trade} tradeable")

def live_once():
    """Evaluate EVERY closed bar not yet in indicators (so a polling gap cannot skip a signal)."""
    df = load_bars(limit=LOOKBACK)
    if df.empty: return
    last = db.q("select max(time_utc) t from indicators where symbol=%s and tf=%s and rules_version=%s", (config.SYMBOL, TF, RV), one=True)["t"]
    if last is None:                                   # first run: do a proper full-history replay once, not a partial catch-up
        first = db.q("select min(time_utc) t from bars where symbol=%s and tf=%s", (config.SYMBOL, TF), one=True)["t"]
        replay(first, _utcnow().replace(tzinfo=None)); return
    last = pd.Timestamp(last).tz_localize(None)
    if df.iloc[0]["time_utc"] >= last:                 # gap bigger than the live window: replay the range exactly
        replay(last, _utcnow().replace(tzinfo=None)); return
    d = indicators.compute(df)
    todo = [i for i in range(len(d)) if d.iloc[i]["time_utc"] > last]
    for i in todo:
        persist_indicators(d, i)
        s = indicators.signal_at(d, i)
        if s:
            sid = persist_signal(d, i, s, "live")
            if sid: print("signal", sid, s["kind"], s["dir"], s["features"].get("tradeable"))

def verify_last_days(days=7):
    """Nightly: replay last N days into source='replay' and compare with live rows. Returns mismatches."""
    end = _utcnow().replace(tzinfo=None); start = end - dt.timedelta(days=days)
    replay(start, end)
    rows = db.q("""select l.id, l.time_utc from signals l left join signals r
                   on r.symbol=l.symbol and r.tf=l.tf and r.time_utc=l.time_utc and r.kind=l.kind and r.rules_version=l.rules_version and r.source='replay'
                   where l.source='live' and l.time_utc>=%s and r.id is null""", (start,))
    return rows

if __name__ == "__main__":
    if "--replay" in sys.argv:
        a = sys.argv.index("--replay"); replay(sys.argv[a + 1], sys.argv[a + 2])
    else:
        while True:
            try:
                if db.get_state() == "LIVE": live_once()
                db.heartbeat("detector", db.get_state())
            except Exception as e:
                from . import selfheal; selfheal.handle("detector", e)
            FAILS = globals().get("FAILS", 0) + 1; globals()["FAILS"] = FAILS
            if FAILS >= 10: raise SystemExit(1)                  # let the supervisor restart a wedged process
            time.sleep(30)
