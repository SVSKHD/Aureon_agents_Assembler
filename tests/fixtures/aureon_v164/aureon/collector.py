"""Runs on the MT5 box. Closed bars only. Executes jobs (backfill / verify_week) requested by Linux services."""
import sys, time, json, datetime as dt
import pandas as pd
from . import config
from .timepolicy import (eu_dst_in_effect, us_dst_in_effect, expected_weekly_open_hour_utc, mt5_shift_hours, from_mt5, to_mt5, weekly_opens,
                         shift_for, verify_policy_against_bars, infer_policy_from_bars, detect_shift_in_stored)  # noqa: F401 (re-exported)
from .store import get_store
from .timeutil import now, from_epoch

TFS = {"M30": 30, "H1": 60}

def _mt5():
    import MetaTrader5 as mt5
    if not mt5.initialize(): raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    if not mt5.symbol_select(config.SYMBOL, True): raise RuntimeError(f"symbol_select failed {config.SYMBOL}: {mt5.last_error()}")
    return mt5

def _rates_to_rows(rates, tf_min):
    """MT5 stamps bars with the broker server clock encoded as if it were UTC; BROKER_UTC_OFFSET_HOURS converts to true UTC
    (set MT5_TIMESTAMPS_ARE_UTC=true to disable the shift if your broker's server clock is already UTC)."""
    off = mt5_shift_hours(); tnow = now().replace(tzinfo=None); rows = []
    for r in rates:
        t = pd.Timestamp(from_mt5(int(r["time"])).replace(tzinfo=None))
        if t > tnow + pd.Timedelta(minutes=5): raise RuntimeError(f"bar {t} is in the future: timestamp policy is wrong (offset {off})")
        if t + pd.Timedelta(minutes=tf_min) > tnow - pd.Timedelta(seconds=5): continue      # forming bar
        o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        if not (config.PRICE_MIN < l <= min(o, c) <= max(o, c) <= h < config.PRICE_MAX): continue   # corrupt bar
        rows.append((t.to_pydatetime().replace(tzinfo=dt.timezone.utc), o, h, l, c, int(r["tick_volume"]), int(r["spread"])))   # tz-aware UTC into timestamptz
    rows.sort(key=lambda r: r[0])
    dedup = {}
    for r in rows: dedup[r[0]] = r                                # last wins; duplicates within a batch collapse
    rows = list(dedup.values())
    for a, b in zip(rows, rows[1:]):
        if (b[0] - a[0]).total_seconds() < tf_min * 60 - 1: raise RuntimeError(f"bars not aligned to {tf_min}m: {a[0]} -> {b[0]}")
    return rows

def _require(rates, what, mt5):
    if rates is None or len(rates) == 0: raise RuntimeError(f"{what} returned nothing: {mt5.last_error()}")
    return rates

def pull_recent(tf="M30", n=200):
    mt5 = _mt5()
    try:
        tfc = {"M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1}[tf]
        rows = _rates_to_rows(_require(mt5.copy_rates_from_pos(config.SYMBOL, tfc, 0, n), "copy_rates_from_pos", mt5), TFS[tf])
    finally: mt5.shutdown()
    if rows and n <= 200:
        age_min = (now().replace(tzinfo=None) - rows[-1][0]).total_seconds() / 60
        if age_min > 3 * TFS[tf] + 60 and now().weekday() < 5 and 2 <= now().hour <= 21:
            raise RuntimeError(f"newest closed {tf} bar is {age_min:.0f} min old during session: offset/clock problem or feed stale")
    get_store().upsert_bars(rows, tf); return len(rows)

def backfill_csv(path, tf):
    df = pd.read_csv(path); t = pd.to_datetime(df["time_utc"], utc=True).dt.tz_localize(None)
    rows = [(a.to_pydatetime(), float(o), float(h), float(l), float(c), int(v), int(s)) for a, o, h, l, c, v, s in
            zip(t, df["open"], df["high"], df["low"], df["close"], df["tick_volume"], df["spread"])]
    get_store().upsert_bars(rows, tf, source="csv"); print(f"backfilled {len(rows)} {tf} bars from {path}")

def verify_week(week_start):
    """Re-pull the whole week from MT5 and overwrite (marking verified is done DB-side by the job requester)."""
    mt5 = _mt5(); n = {}
    try:
        ws = pd.Timestamp(week_start).to_pydatetime(); we = ws + dt.timedelta(days=7)
        for tf, tfc in (("M30", mt5.TIMEFRAME_M30), ("H1", mt5.TIMEFRAME_H1)):
            rates = mt5.copy_rates_range(config.SYMBOL, tfc, to_mt5(ws), to_mt5(we))        # same policy as the inbound conversion
            rows = _rates_to_rows(_require(rates, f"copy_rates_range {tf}", mt5), TFS[tf]); get_store().upsert_bars(rows, tf, source="verify"); n[tf] = len(rows)
    finally: mt5.shutdown()
    return n

# ---------------- timestamp policy verification (B3) ----------------
def check_timestamp_policy():
    """Refuse to run without an explicit policy, and refuse if the policy disagrees with the broker's own weekly-open bars."""
    if not config.MT5_TIME_POLICY: raise RuntimeError("MT5_TIME_POLICY is not set — run scripts/diagnose_timestamps.py and set 'utc' or 'server_offset:+N'")
    mt5 = _mt5()
    try:
        r = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_M30, 0, 48 * 7 * 4)
        if r is None or len(r) == 0: raise RuntimeError(f"no rates for policy check: {mt5.last_error()}")
        raw = [dt.datetime.fromtimestamp(int(x["time"]), dt.timezone.utc).replace(tzinfo=None) for x in r]
    finally: mt5.shutdown()
    ok, detail = verify_policy_against_bars(raw, config.MT5_TIMESTAMPS_ARE_UTC, config.BROKER_UTC_OFFSET_HOURS, dst=config.MT5_SERVER_DST)
    if ok is False:
        raise RuntimeError(f"MT5_TIME_POLICY={config.MT5_TIME_POLICY} contradicts broker data ({detail}); policies consistent with the data: {infer_policy_from_bars(raw)}")
    return detail

def timestamp_diagnostics():
    """Report, don't infer: shows the newest closed bar in raw MT5 time and after the configured policy, next to system UTC.
    The operator confirms once (scripts/diagnose_timestamps.py) which policy is right for THIS broker; we never derive the offset
    from tick-vs-system-clock arithmetic (Windows clock skew would corrupt it)."""
    mt5 = _mt5()
    try:
        r = mt5.copy_rates_from_pos(config.SYMBOL, mt5.TIMEFRAME_M30, 0, 3)
        if r is None or len(r) == 0: raise RuntimeError("no rates for diagnostics")
        raw = pd.to_datetime(int(r[-1]["time"]), unit="s"); off = 0.0 if config.MT5_TIMESTAMPS_ARE_UTC else config.BROKER_UTC_OFFSET_HOURS
        conv = raw - pd.Timedelta(hours=off); sysnow = now().replace(tzinfo=None)
        age = (sysnow - conv).total_seconds() / 60
        return dict(raw_latest_bar=str(raw), converted_utc=str(conv), system_utc=str(sysnow), policy=("MT5_TIMESTAMPS_ARE_UTC" if config.MT5_TIMESTAMPS_ARE_UTC else f"shift {off:+.1f}h"), age_min=age,
                    plausible=(-5 <= age <= 3 * 30 + 90))
    finally: mt5.shutdown()

def check_timestamps():
    d = timestamp_diagnostics()
    if not d["plausible"]:
        raise RuntimeError(f"timestamp policy implausible: newest forming bar {d['converted_utc']} vs system {d['system_utc']} ({d['age_min']:.0f} min) under policy {d['policy']} — run scripts/diagnose_timestamps.py")
    return d

def backfill_range(start_utc, end_utc, tfs=("M30", "H1")):
    """Fetch a UTC window from MT5 (range request through the same policy), validate, upsert. Returns counts; never fabricates bars."""
    mt5 = _mt5(); n = {}
    try:
        for tf in tfs:
            tfc = {"M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1}[tf]
            rates = mt5.copy_rates_range(config.SYMBOL, tfc, to_mt5(start_utc - dt.timedelta(days=10)), to_mt5(end_utc))   # 10 days of warm-up context
            if rates is None or len(rates) == 0: n[tf] = 0; continue
            rows = _rates_to_rows(rates, TFS[tf]); get_store().upsert_bars(rows, tf, source="backfill"); n[tf] = len(rows)
    finally: mt5.shutdown()
    return n

def run_jobs():
    import socket
    worker = f"collector@{socket.gethostname()}"
    j = get_store().claim_job(worker)
    if not j: return
    try:
        p = j["payload"] if isinstance(j["payload"], dict) else json.loads(j["payload"] or "{}")
        if j["kind"] == "backfill":
            res = {}
            for tf in TFS:
                res[tf] = pull_recent(tf, int(p.get("n", 2000))); get_store().renew_job(j["id"], worker, j["lease_token"])
        elif j["kind"] == "verify_week":
            res = verify_week(p["week_start"])
        elif j["kind"] == "backfill_range":
            res = backfill_range(dt.datetime.fromisoformat(p["start"]), dt.datetime.fromisoformat(p["end"]), p.get("tfs", ["M30", "H1"]))
        elif j["kind"] == "mt5_history":
            from .mt5_history import fetch_deals
            deals = fetch_deals(dt.datetime.fromisoformat(p["start"]) - dt.timedelta(days=7), dt.datetime.fromisoformat(p["end"]) + dt.timedelta(days=1))
            get_store().ingest_deals(deals); res = {"deals": len(deals)}
        else: raise ValueError(f"unknown job {j['kind']}")
        get_store().finish_job(j["id"], True, json.dumps(res), worker=worker, token=j["lease_token"])
    except Exception as e:
        get_store().finish_job(j["id"], False, f"{type(e).__name__}: {e}", worker=worker, token=j["lease_token"])

if __name__ == "__main__":
    if "--backfill" in sys.argv:
        a = sys.argv.index("--backfill"); backfill_csv(sys.argv[a + 1], sys.argv[sys.argv.index("--tf") + 1])
    else:
        while True:
            try: check_timestamp_policy(); check_timestamps(); break
            except Exception as e: print("OFFSET CHECK FAILED:", e); from .alerter import post; post(f"🚨 collector refuses to start: {e}"); time.sleep(300)
        while True:
            try:
                st = get_store().get_state()
                run_jobs()
                if st in ("LIVE", "RESUMING"):
                    for tf in TFS: pull_recent(tf, 200)
                get_store().heartbeat("collector", st)
            except Exception as e:
                from . import selfheal; selfheal.handle("collector", e)
            time.sleep(60)
