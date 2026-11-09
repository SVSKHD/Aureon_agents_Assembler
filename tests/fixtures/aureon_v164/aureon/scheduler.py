"""Linux. Operator intent lives in control.desired (RUN|PAUSE); heartbeats are status only.
State: LIVE / PAUSED / RESUMING. Never touches MT5 — requests a backfill job and waits for the Windows collector."""
import time, datetime as dt, subprocess, shlex
import pandas as pd
from . import config, db, alerter
from .timeutil import now, naive

SERVICES = ("collector", "detector", "alerter", "guard")
STEP = {"M30": 30, "H1": 60}

def in_session(t):
    wd, hm = t.weekday(), t.strftime("%H:%M")
    if wd == 5: return False
    if wd == 6: return hm >= "23:00"
    if wd == 4 and hm >= "22:00": return False
    return not db.q("select 1 from market_calendar where kind='holiday' and start_utc<=%s and end_utc>%s", (t, t), one=True)

def _hm(s): h, m = map(int, s.split(":")); return h, m

def expected_bars(start, end, tf, holidays=()):
    """Every bar open that SHOULD exist between start and end: Mon–Fri, SESSION_FIRST_BAR_UTC..SESSION_LAST_BAR_UTC, minus holidays."""
    step = STEP[tf]; fh, fm = _hm(config.SESSION_FIRST_BAR_UTC); lh, lm = _hm(config.SESSION_LAST_BAR_UTC)
    last_h, last_m = (lh, lm) if tf == "M30" else (lh, 0)
    out = []; day = start.date()
    while day <= end.date():
        if day.weekday() < 5 and day not in holidays:
            t = dt.datetime.combine(day, dt.time(fh, fm)); stop = dt.datetime.combine(day, dt.time(last_h, last_m))
            while t <= stop:
                if start <= t <= end: out.append(t)
                t += dt.timedelta(minutes=step)
        day += dt.timedelta(days=1)
    return out

def expected_gap(prev, cur, step):
    """Kept for tests: True if no expected bar lies strictly between prev and cur."""
    return not [t for t in expected_bars(prev, cur, "M30" if step == 30 else "H1") if prev < t < cur]

def gaps(tf="M30", days=3):
    end = naive(now()) - dt.timedelta(minutes=STEP[tf] * 2); start = end - dt.timedelta(days=days)
    hol = {naive(r["start_utc"]).date() for r in db.q("select start_utc from market_calendar where kind='holiday' and start_utc>=%s", (start - dt.timedelta(days=1),))}
    have = {naive(r["time_utc"]) for r in db.q("select time_utc from bars where symbol=%s and tf=%s and time_utc between %s and %s", (config.SYMBOL, tf, start, end))}
    return [f"{t:%d %b %H:%M}" for t in expected_bars(start, end, tf, hol) if t not in have]

def wait_job(job_id, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = db.job_status(job_id)
        if j and j["done_at"]: return j
        time.sleep(5)
    return None

PENDING_JOB = {"id": None}

def collector_fresh(t, max_age=180):
    r = db.q("select last_beat from service_heartbeats where service='collector'", one=True)
    return bool(r) and (t - r["last_beat"]).total_seconds() <= max_age

def resume(t):
    """Durable ack: the backfill job row is the evidence. One outstanding job at a time; no elapsed-time assumptions."""
    if not collector_fresh(t): return "collector (MT5 box) not heartbeating — cannot backfill"
    if PENDING_JOB["id"] is None:
        PENDING_JOB["id"] = db.request_job("backfill", {"n": 2000}); return "backfill requested, waiting for collector ack"
    j = db.job_status(PENDING_JOB["id"])
    if not j or j["state"] in ("pending", "running"): return f"backfill job {PENDING_JOB['id']} {j['state'] if j else '?'} — waiting"
    PENDING_JOB["id"] = None
    if j["state"] != "done": return f"backfill job {j['id']} {j['state']}: {j['result']}"
    g = gaps("M30", 3)
    if g: return f"gaps remain after backfill: {g[:5]}"
    from . import detector
    detector.replay(naive(t) - dt.timedelta(days=3), naive(t))
    return None

def watchdog(t):
    for s in SERVICES:
        r = db.q("select last_beat from service_heartbeats where service=%s", (s,), one=True)
        age = None if not r else (t - r["last_beat"]).total_seconds()
        if age is None or age > config.HEARTBEAT_SEC * config.HEARTBEAT_MISS_LIMIT:
            where = "(MT5 box)" if s in ("collector", "guard") else ""
            alerter.post(f"⚠️ {s} {where}: no heartbeat for {int(age) if age else '?'}s")
            if config.RESTART_CMD and s not in ("collector", "guard"):
                subprocess.run(shlex.split(config.RESTART_CMD.format(service=s)), check=False)

def maybe_weekend_review(t):
    """Non-blocking: on a closed Saturday/Sunday, run last week's review once in a background thread (idempotent on the report key)."""
    import threading
    from . import weekend_review as wr
    if not wr.should_auto_run(t) or getattr(maybe_weekend_review, "started", False): return
    maybe_weekend_review.started = True
    threading.Thread(target=lambda: wr.run(), daemon=True, name="weekend-review").start()

if __name__ == "__main__":
    db.ensure_rules_version(); state = db.get_state(); fails = 0
    try: maybe_weekend_review(now())
    except Exception as e: alerter.post(f"weekend review could not start: {e}", key=f"wr-start:{now():%Y%m%d}")
    while True:
        t = now(); desired = db.get_control("desired", "RUN")
        if desired == "PAUSE":
            if state != "PAUSED": alerter.post("⏸ Aureon PAUSED by operator")
            state = "PAUSED"
        elif in_session(t):
            if state in ("PAUSED", "RESUMING", "FAILED"):
                state = "RESUMING"; db.heartbeat("scheduler", state, "backfill+replay")
                try: err = resume(t)
                except Exception as e: err = f"{type(e).__name__}: {e}"
                if err is None: state = "LIVE"; fails = 0; alerter.post("▶️ Aureon LIVE", key=f"live:{t:%Y%m%d%H}")
                elif "waiting" in err: pass                                            # normal: job in flight
                else:
                    fails += 1; alerter.post(f"resume attempt {fails}: {err}", key=f"resume:{t:%Y%m%d%H%M}")
                    if fails >= 10: state = "FAILED"; alerter.post("🚨 resume FAILED 10x — operator action needed; alerts suppressed, guard still runs", key=f"resumefail:{t:%Y%m%d}")
            elif state == "LIVE":
                if not collector_fresh(t): state = "DEGRADED"; alerter.post("⚠️ DEGRADED: collector (MT5 box) stopped heartbeating — no new signals until it returns", key=f"degraded:{t:%Y%m%d%H}")
                watchdog(t)
            elif state == "DEGRADED":
                if collector_fresh(t): state = "RESUMING"                               # re-backfill what was missed, then LIVE
        else:
            if state != "PAUSED": state = "PAUSED"; alerter.post("⏸ Aureon PAUSED (weekend / out of session)")
            try: maybe_weekend_review(t)
            except Exception as e: alerter.post(f"weekend review could not start: {e}", key=f"wr-start:{t:%Y%m%d}")
        db.heartbeat("scheduler", state, "operator" if desired == "PAUSE" else ("backfill+replay" if state == "RESUMING" else ""))
        # detector/alerter only act in LIVE: DEGRADED and FAILED therefore stop new signals automatically
        time.sleep(60)
