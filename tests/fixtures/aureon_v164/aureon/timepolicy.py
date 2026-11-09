"""Pure timestamp-policy helpers (no MT5, no store, no DB) — importable from scripts and tests anywhere.
Policies: 'utc' | 'server_offset:+N' | 'server_dst:eu:+B' | 'server_dst:us:+B' (see config.py)."""
import datetime as dt
from . import config

def us_dst_in_effect(d):
    """US DST: second Sunday of March 02:00 local -> first Sunday of November. Gold (COMEX) hours follow New York."""
    y = d.year
    march = dt.date(y, 3, 8) + dt.timedelta(days=(6 - dt.date(y, 3, 8).weekday()) % 7)        # 2nd Sunday of March
    nov = dt.date(y, 11, 1) + dt.timedelta(days=(6 - dt.date(y, 11, 1).weekday()) % 7)          # 1st Sunday of November
    return march <= d < nov

def eu_dst_in_effect(d):
    """EU summer time: last Sunday of March 01:00 UTC -> last Sunday of October 01:00 UTC."""
    y = d.year
    last_sun = lambda m: dt.date(y, m, 31) - dt.timedelta(days=(dt.date(y, m, 31).weekday() + 1) % 7)
    return last_sun(3) <= d < last_sun(10)

def expected_weekly_open_hour_utc(sunday, rule=None):
    """WEEKLY_OPEN_RULE: 'us_dst' (COMEX-linked: Sunday 18:00 New York = 22 UTC in DST, 23 otherwise) or 'fixed:HH'
    (brokers whose session does not follow New York). Set per broker; the diagnostic prints the raw opens so you can tell."""
    rule = rule or config.WEEKLY_OPEN_RULE
    if rule.startswith("fixed:"): return int(rule.split(":", 1)[1])
    return 22 if us_dst_in_effect(sunday) else 23

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

