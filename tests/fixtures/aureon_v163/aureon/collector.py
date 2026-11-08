"""Runs on the MT5 box. Closed bars only. Executes jobs (backfill / verify_week) requested by Linux services."""
import sys, time, json, datetime as dt
import pandas as pd
from . import config
from .store import store
from .timeutil import now, from_epoch

TFS = {"M30": 30, "H1": 60}

def eu_dst_in_effect(d):
    """EU summer time: last Sunday of March 01:00 UTC -> last Sunday of October 01:00 UTC."""
    y = d.year
    last_sun = lambda m: dt.date(y, m, 31) - dt.timedelta(days=(dt.date(y, m, 31).weekday() + 1) % 7)
    return last_sun(3) <= d < last_sun(10)

def mt5_shift_hours(at=None):
    """Hours to SUBTRACT from an MT5-encoded time to get UTC, for the date `at` (server-encoded, naive).
    'utc' -> 0; 'server_offset:+N' -> N; 'server_dst:eu:+B' -> B+1 while EU DST is in effect at that date, else B."""
    if config.MT5_TIMESTAMPS_ARE_UTC: return 0.0
    base = config.BROKER_UTC_OFFSET_HOURS
    if config.MT5_SERVER_DST == "eu" and at is not None: return base + (1.0 if eu_dst_in_effect(at.date()) else 0.0)
    if config.MT5_SERVER_DST == "us" and at is not None: return base + (1.0 if us_dst_in_effect(at.date()) else 0.0)
    return base

def from_mt5(epoch_or_dt):
    """MT5-encoded time -> tz-aware UTC (offset chosen from the bar's own date under a DST policy)."""
    raw = dt.datetime.fromtimestamp(epoch_or_dt, dt.timezone.utc).replace(tzinfo=None) if isinstance(epoch_or_dt, (int, float)) else epoch_or_dt.replace(tzinfo=None)
    return (raw - dt.timedelta(hours=mt5_shift_hours(raw))).replace(tzinfo=dt.timezone.utc)

def to_mt5(dt_utc):
    """UTC (aware or naive-UTC) -> the naive datetime MT5's python API expects for ranges."""
    if dt_utc.tzinfo is not None: dt_utc = dt_utc.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return dt_utc + dt.timedelta(hours=mt5_shift_hours(dt_utc))

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
    store.upsert_bars(rows, tf); return len(rows)

def backfill_csv(path, tf):
    df = pd.read_csv(path); t = pd.to_datetime(df["time_utc"], utc=True).dt.tz_localize(None)
    rows = [(a.to_pydatetime(), float(o), float(h), float(l), float(c), int(v), int(s)) for a, o, h, l, c, v, s in
            zip(t, df["open"], df["high"], df["low"], df["close"], df["tick_volume"], df["spread"])]
    store.upsert_bars(rows, tf, source="csv"); print(f"backfilled {len(rows)} {tf} bars from {path}")

def verify_week(week_start):
    """Re-pull the whole week from MT5 and overwrite (marking verified is done DB-side by the job requester)."""
    mt5 = _mt5(); n = {}
    try:
        ws = pd.Timestamp(week_start).to_pydatetime(); we = ws + dt.timedelta(days=7)
        for tf, tfc in (("M30", mt5.TIMEFRAME_M30), ("H1", mt5.TIMEFRAME_H1)):
            rates = mt5.copy_rates_range(config.SYMBOL, tfc, to_mt5(ws), to_mt5(we))        # same policy as the inbound conversion
            rows = _rates_to_rows(_require(rates, f"copy_rates_range {tf}", mt5), TFS[tf]); store.upsert_bars(rows, tf, source="verify"); n[tf] = len(rows)
    finally: mt5.shutdown()
    return n

# ---------------- timestamp policy verification (B3) ----------------
def us_dst_in_effect(d):
    """US DST: second Sunday of March 02:00 local -> first Sunday of November. Gold (COMEX) hours follow New York."""
    y = d.year
    march = dt.date(y, 3, 8) + dt.timedelta(days=(6 - dt.date(y, 3, 8).weekday()) % 7)        # 2nd Sunday of March
    nov = dt.date(y, 11, 1) + dt.timedelta(days=(6 - dt.date(y, 11, 1).weekday()) % 7)          # 1st Sunday of November
    return march <= d < nov

def expected_weekly_open_hour_utc(sunday, rule=None):
    """WEEKLY_OPEN_RULE: 'us_dst' (COMEX-linked: Sunday 18:00 New York = 22 UTC in DST, 23 otherwise) or 'fixed:HH'
    (brokers whose session does not follow New York). Set per broker; the diagnostic prints the raw opens so you can tell."""
    rule = rule or config.WEEKLY_OPEN_RULE
    if rule.startswith("fixed:"): return int(rule.split(":", 1)[1])
    return 22 if us_dst_in_effect(sunday) else 23

def weekly_opens(times):
    """Bar opens that follow a gap > 20 h (the first bar after each weekend); the sample's first bar counts if it is a Sunday."""
    opens = [b for a, b in zip(times, times[1:]) if (b - a) > dt.timedelta(hours=20)]
    if times and times[0].weekday() == 6 and (not opens or opens[0] != times[0]): opens.insert(0, times[0])
    return opens

def shift_for(raw, is_utc, offset_hours, dst=""):
    if is_utc: return 0.0
    if dst == "eu": return offset_hours + (1.0 if eu_dst_in_effect(raw.date()) else 0.0)
    if dst == "us": return offset_hours + (1.0 if us_dst_in_effect(raw.date()) else 0.0)
    return offset_hours

def verify_policy_against_bars(raw_times, is_utc, offset_hours, rule=None, min_fraction=None, holidays=(), dst=""):
    """raw_times: naive datetimes exactly as MT5 encodes them. Converts with the policy and checks weekly opens against the
    WEEKLY_OPEN_RULE. Weeks whose Sunday/Monday is a listed holiday are skipped; otherwise at least WEEKLY_OPEN_MIN_FRACTION of
    the weeks must agree (one odd week — an unlisted holiday or a late open — does not fail a correct policy, but a wrong offset
    fails every week). Returns (ok, detail); ok is None when there is nothing to check."""
    min_fraction = config.WEEKLY_OPEN_MIN_FRACTION if min_fraction is None else min_fraction
    conv = [t - dt.timedelta(hours=shift_for(t, is_utc, offset_hours, dst)) for t in raw_times]
    opens = [o for o in weekly_opens(conv) if o.date() not in holidays and (o + dt.timedelta(days=1)).date() not in holidays]
    if not opens: return None, "no weekly open in sample"
    bad = [o for o in opens if not (o.weekday() == 6 and o.hour == expected_weekly_open_hour_utc(o.date(), rule) and o.minute == 0)]
    ok = (len(opens) - len(bad)) / len(opens) >= min_fraction
    return ok, f"{len(opens)} weekly opens checked, {len(bad)} inconsistent: {[str(b) for b in bad[:3]]}"

def infer_policy_from_bars(raw_times):
    """Diagnostics: every policy string that makes ALL weekly opens land on the true gold open. Fixed offsets and the two
    DST-following server rules are tried; a fixed offset only wins when the sample never crosses a clock change."""
    cands = []
    for off in [x / 2 for x in range(-24, 25)]:
        ok, _ = verify_policy_against_bars(raw_times, False, off, min_fraction=1.0)
        if ok: cands.append("utc" if off == 0 else f"server_offset:{off:+g}")
    for dst in ("eu", "us"):
        for base in (-1.0, 0.0, 1.0, 2.0, 3.0):
            ok, _ = verify_policy_against_bars(raw_times, False, base, min_fraction=1.0, dst=dst)
            if ok: cands.append(f"server_dst:{dst}:{base:+g}")
    return cands

def detect_shift_in_stored(times_utc):
    c = infer_policy_from_bars(list(times_utc))
    if not c: return None
    fixed = [x for x in c if x == "utc" or x.startswith("server_offset:")]
    if len(fixed) == 1: return 0.0 if fixed[0] == "utc" else float(fixed[0].split(":")[1])
    return c

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
            rows = _rates_to_rows(rates, TFS[tf]); store.upsert_bars(rows, tf, source="backfill"); n[tf] = len(rows)
    finally: mt5.shutdown()
    return n

def run_jobs():
    import socket
    worker = f"collector@{socket.gethostname()}"
    j = store.claim_job(worker)
    if not j: return
    try:
        p = j["payload"] if isinstance(j["payload"], dict) else json.loads(j["payload"] or "{}")
        if j["kind"] == "backfill":
            res = {}
            for tf in TFS:
                res[tf] = pull_recent(tf, int(p.get("n", 2000))); store.renew_job(j["id"], worker, j["lease_token"])
        elif j["kind"] == "verify_week":
            res = verify_week(p["week_start"])
        elif j["kind"] == "backfill_range":
            res = backfill_range(dt.datetime.fromisoformat(p["start"]), dt.datetime.fromisoformat(p["end"]), p.get("tfs", ["M30", "H1"]))
        elif j["kind"] == "mt5_history":
            from .mt5_history import fetch_deals
            deals = fetch_deals(dt.datetime.fromisoformat(p["start"]) - dt.timedelta(days=7), dt.datetime.fromisoformat(p["end"]) + dt.timedelta(days=1))
            store.ingest_deals(deals); res = {"deals": len(deals)}
        else: raise ValueError(f"unknown job {j['kind']}")
        store.finish_job(j["id"], True, json.dumps(res), worker=worker, token=j["lease_token"])
    except Exception as e:
        store.finish_job(j["id"], False, f"{type(e).__name__}: {e}", worker=worker, token=j["lease_token"])

if __name__ == "__main__":
    if "--backfill" in sys.argv:
        a = sys.argv.index("--backfill"); backfill_csv(sys.argv[a + 1], sys.argv[sys.argv.index("--tf") + 1])
    else:
        while True:
            try: check_timestamp_policy(); check_timestamps(); break
            except Exception as e: print("OFFSET CHECK FAILED:", e); from .alerter import post; post(f"🚨 collector refuses to start: {e}"); time.sleep(300)
        while True:
            try:
                st = store.get_state()
                run_jobs()
                if st in ("LIVE", "RESUMING"):
                    for tf in TFS: pull_recent(tf, 200)
                store.heartbeat("collector", st)
            except Exception as e:
                from . import selfheal; selfheal.handle("collector", e)
            time.sleep(60)
