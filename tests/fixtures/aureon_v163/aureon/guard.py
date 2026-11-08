"""Owns every open trade. Attaches to YOUR position, never opens one. Never AI-patched. STORE_MODE=api on the MT5 box.
Trade states: open -> closing (close sent, outcome unknown) -> closed (complete broker evidence) | unreconciled.
Risk: aureon.risk.RiskEngine (durable per broker day). LOCKED/UNKNOWN deny new entries (alerter) and trigger the flattening
policy; protective management of existing positions never stops. Manual trades placed outside Aureon can only be detected and
reported, not prevented — an application guard cannot enforce funded-account rules by itself."""
import time, datetime as dt, logging, uuid
from . import config, orders
from .store import store
from .alerter import post
from .timeutil import now, naive, from_epoch
from .risk import RiskEngine, broker_day

R = config.RULES; log = logging.getLogger("guard")
UNPROTECTED = {}                    # ticket -> first seen without verified stop (naive utc)
MISSING = {}; MISSING_LIMIT = 3
OFF = config.BROKER_UTC_OFFSET_HOURS if not config.MT5_TIMESTAMPS_ARE_UTC else 0.0
ENGINE = None

def _engine():
    global ENGINE
    if ENGINE is None: ENGINE = RiskEngine(orders.account_info()["login"])
    return ENGINE

def _signal_sl(sig):
    """Stop distance ONLY from validated data: the signal's suggested_sl (1.5 x ATR at signal time) or the last bar's ATR. None otherwise."""
    if sig and sig.get("suggested_sl"): return float(sig["suggested_sl"])
    bar = store.last_closed_m30(); atr = bar.get("atr14") if bar else None
    return R["sl_atr"] * float(atr) if atr and atr > 0 else None

# ---------------- protection ----------------
def _ensure_sl(ticket, want):
    """Sets and VERIFIES a broker-side stop. Returns verified SL or None (ticket recorded as unprotected)."""
    try:
        got = orders.modify_sl(ticket, want); UNPROTECTED.pop(ticket, None); return got
    except orders.OrderError as e:
        UNPROTECTED.setdefault(ticket, naive(now())); post(f"🚨 ticket {ticket} has NO verified stop: {e} — set it manually NOW", key=f"naked:{ticket}:{naive(now()):%H%M}"); return None

def _naked_failsafe(t, ticket):
    """A position without a verifiable stop and no validated distance is never 'protected' by an arbitrary number.
    Policy NAKED_POSITION_POLICY: alert_only (heartbeat flags UNPROTECTED, /readyz fails) or flatten_after (grace, then close).
    Applies to EVERY trade state: open trades go through _exit; CLOSING/UNRECONCILED positions get a direct, request-id-tagged close
    whose outcome is then reconciled like any other."""
    first = UNPROTECTED.setdefault(ticket, naive(now()))
    post(f"🚨 ticket {ticket} ({t['status'] if t else 'unattached'}) has NO verifiable stop and no validated distance — policy {config.NAKED_POSITION_POLICY}", key=f"naked:{ticket}:{first:%H%M}")
    if config.NAKED_POSITION_POLICY != "flatten_after" or (naive(now()) - first).total_seconds() < config.NAKED_POSITION_GRACE_MIN * 60: return
    post(f"🛑 ticket {ticket} still unprotected after {config.NAKED_POSITION_GRACE_MIN} min — fail-safe flatten", key=f"naked-flatten:{ticket}")
    if t is None or t["status"] == "open": 
        if t is not None: _exit(t, "naked_failsafe")
        return
    if t.get("close_sent_at") and (naive(now()) - naive(t["close_sent_at"])).total_seconds() < 120: return     # a close is already in flight
    rid = t.get("close_request_id") or uuid.uuid4().hex[:12]
    store.update_trade(t["id"], close_sent_at=naive(now()), close_request_id=rid); store.event(t["id"], "close_sent", note=f"naked_failsafe rid={rid} ({t['status']})")
    try: d = orders.close(t["mt5_ticket"], rid)
    except orders.OrderError as e: post(f"⚠️ fail-safe close on {ticket} not confirmed: {e} — reconciling", key=f"naked-closeunk:{ticket}"); return
    if d.get("complete") and d.get("pnl") is not None: _close_in_db(t, d, "naked_failsafe", t["status"])

def attach_new_positions(live):
    for p in live.values():
        if store.trade_by_ticket(p["ticket"]): continue
        sig = store.recent_signal(p["dir"]); dist = _signal_sl(sig)
        sl_set = p["sl"] or None
        if not sl_set and dist is not None: sl_set = _ensure_sl(p["ticket"], p["price"] - dist if p["dir"] > 0 else p["price"] + dist)
        if not sl_set and dist is None: UNPROTECTED.setdefault(p["ticket"], naive(now()))    # policy enforced by manage()/protect_uncertain() every loop
        tid = store.insert_trade(dict(signal_id=sig["id"] if sig else None, mt5_ticket=p["ticket"], dir=p["dir"], lots=p["lots"],
                                      entry_time=from_epoch(p["time"], OFF), entry_price=p["price"], sl=sl_set, tp=p["tp"] or None))
        store.event(tid, "attach", new_sl=sl_set, note=f"signal {sig['id'] if sig else 'EXTERNAL (no matching signal)'}; sl {'ok' if sl_set else 'MISSING'}")
        if not sig: post(f"ℹ️ external position detected: ticket {p['ticket']} {('BUY' if p['dir']>0 else 'SELL')} {p['lots']} @ {p['price']} — no Aureon signal matches; managing protection only", key=f"external:{p['ticket']}")

# ---------------- reconciliation ----------------
def _close_in_db(t, d, reason, expected):
    ok = store.update_trade(t["id"], expected_status=expected, status="closed",
                            exit_time=from_epoch(d["time"], OFF) if isinstance(d.get("time"), (int, float)) else naive(now()),
                            exit_price=d["price"], pnl_usd=d["pnl"] + (d.get("commission") or 0) + (d.get("swap") or 0), exit_reason=reason)
    if ok: store.event(t["id"], "exit", note=reason)
    return ok

def _mark_unreconciled(t, why):
    if t["status"] != "unreconciled" and store.update_trade(t["id"], expected_status=t["status"], status="unreconciled", exit_reason="unreconciled"):
        store.event(t["id"], "exit", note=f"UNRECONCILED: {why}")
        post(f"⚠️ ticket {t['mt5_ticket']} UNRECONCILED: {why}. Not counted as closed. Reply `reconcile {t['mt5_ticket']}` after checking MT5 history.", key=f"unrec:{t['mt5_ticket']}")

def _reconcile_missing(t):
    n = MISSING.get(t["mt5_ticket"], 0) + 1; MISSING[t["mt5_ticket"]] = n
    try: d = orders.deal_result(t["mt5_ticket"])
    except orders.MT5Unavailable: return
    if d and d["complete"]: _close_in_db(t, d, "mt5_closed", t["status"]); MISSING.pop(t["mt5_ticket"], None)
    elif n >= MISSING_LIMIT: _mark_unreconciled(t, f"absent for {n} loops, closing deals {'partial' if d else 'not found'}"); MISSING.pop(t["mt5_ticket"], None)

def retry_uncertain(live, only_ticket=None):
    """Resolve 'closing' and 'unreconciled' trades from broker evidence; re-adopt ones the broker still shows open."""
    for t in store.open_trades():
        if t["status"] not in ("closing", "unreconciled") or (only_ticket and t["mt5_ticket"] != only_ticket): continue
        if t["mt5_ticket"] in live:
            if t["status"] == "closing" and t.get("close_sent_at") and (naive(now()) - naive(t["close_sent_at"])).total_seconds() < 120: continue   # give the broker a moment
            if store.update_trade(t["id"], expected_status=t["status"], status="open", exit_reason=None):
                store.event(t["id"], "attach", note=f"broker still shows position open ({t['status']}) -> back to open")
            continue
        try: d = orders.deal_result(t["mt5_ticket"])
        except orders.MT5Unavailable: return
        if d and d["complete"] and _close_in_db(t, d, "mt5_closed_reconciled", t["status"]):
            post(f"✅ ticket {t['mt5_ticket']} reconciled: exit {d['price']} pnl {d['pnl']:+.2f}", key=f"reconciled:{t['mt5_ticket']}")
        elif t["status"] == "closing" and t.get("close_sent_at") and (naive(now()) - naive(t["close_sent_at"])).total_seconds() > 600:
            _mark_unreconciled(t, "close sent >10 min ago, no complete deal and position not visible")

def handle_reconcile_request(live):
    req = store.get_control("reconcile_request")
    if req and str(req).isdigit(): retry_uncertain(live, only_ticket=int(req)); store.set_control("reconcile_request", "", "guard")

# ---------------- management ----------------
def manage(live):
    bar = store.last_closed_m30(); tnow = now()
    for t in store.open_trades():
        if t["status"] != "open": continue                           # closing/unreconciled handled by retry_uncertain (and still protected below)
        p = live.get(t["mt5_ticket"])
        if p is None: _reconcile_missing(t); continue
        MISSING.pop(t["mt5_ticket"], None)
        ent, d = t["entry_price"], t["dir"]; entry_time = naive(t["entry_time"])
        if not p["sl"]:
            dist = t["sl"] and abs(ent - t["sl"]) or _signal_sl(None)
            got = _ensure_sl(t["mt5_ticket"], ent - dist * d) if dist else None
            if got: store.update_trade(t["id"], sl=got); store.event(t["id"], "sl_set", new_sl=got, note="restored")
            else: _naked_failsafe(t, t["mt5_ticket"])
            continue
        UNPROTECTED.pop(t["mt5_ticket"], None)
        if tnow.weekday() == 4 and tnow.strftime("%H:%M") >= R["friday_flat_utc"]: _exit(t, "flat_friday"); continue
        if bar and naive(bar["time_utc"]) >= entry_time:                 # only bars opened at/after entry
            if bar["vwap_side"] == -d and naive(bar["time_utc"]) > entry_time: _exit(t, "opp_vwap_cross"); continue
            if bar["bar_of_day"] >= R["flat_bar_of_day"]: _exit(t, "flat_roll"); continue
            reached = (bar["high"] - ent) if d > 0 else (ent - bar["low"]); cur_lock = (p["sl"] - ent) * d
            target = max((lock for reach, lock in R["lock_ladder"] if reached >= reach and cur_lock < lock), default=None)
            if target is not None:
                new_sl = ent + target if d > 0 else ent - target; px = orders.current_price()
                if (d > 0 and px["bid"] <= new_sl) or (d < 0 and px["ask"] >= new_sl): _exit(t, "lock_level_breached"); continue
                try:
                    got = orders.modify_sl(t["mt5_ticket"], new_sl); store.update_trade(t["id"], sl=got)
                    store.event(t["id"], "lock", old_sl=p["sl"], new_sl=got, note=f"reached +{reached:.1f}")
                except orders.OrderError as e: post(f"lock failed on {t['mt5_ticket']}: {e}", key=f"lockfail:{t['mt5_ticket']}:{naive(now()):%H%M}")
    protect_uncertain(live)

def protect_uncertain(live):
    """CLOSING / UNRECONCILED trades whose position the BROKER still shows: re-query the live position (never the DB row) and make
    sure a verified protective stop exists. A missing DB SL is never taken as 'the broker has one'. No close is sent here —
    closing is owned by retry_uncertain / _exit, so no duplicate attempts."""
    for t in store.open_trades():
        if t["status"] not in ("closing", "unreconciled") or t["mt5_ticket"] not in live: continue
        try: p = orders.position_state(t["mt5_ticket"])                  # fresh broker read, includes current SL and volume
        except orders.MT5Unavailable: return                              # disconnected: no assumptions, next loop
        if p is None: continue                                            # gone between snapshot and now -> retry_uncertain handles evidence
        if p["sl"]: UNPROTECTED.pop(t["mt5_ticket"], None); continue         # a broker-held stop is by construction on the valid side of the market (B8: no direction test)
        dist = (abs(t["entry_price"] - t["sl"]) if t.get("sl") else None) or _signal_sl(None)
        if dist is None: _naked_failsafe(t, t["mt5_ticket"]); continue
        want = p["price_open"] - dist if p["dir"] > 0 else p["price_open"] + dist
        got = _ensure_sl(t["mt5_ticket"], want)                           # modify_sl verifies the broker response + re-reads the position
        if got: store.event(t["id"], "sl_set", new_sl=got, note=f"protective stop restored on {t['status']} position (volume {p['volume']})")

def _exit(t, reason):
    """Single close attempt per loop, guarded by the 'closing' state so a second loop can't double-send."""
    if t["status"] == "closing": return
    rid = t.get("close_request_id") or uuid.uuid4().hex[:12]
    if not store.update_trade(t["id"], expected_status="open", status="closing", close_sent_at=naive(now()), close_request_id=rid): return
    store.event(t["id"], "close_sent", note=f"{reason} rid={rid}")
    try: d = orders.close(t["mt5_ticket"], rid)
    except orders.ExecutionUnknown as e:
        post(f"⚠️ close outcome UNKNOWN on {t['mt5_ticket']} ({reason}): {e} — reconciling from broker history", key=f"closeunk:{t['mt5_ticket']}"); return   # stays 'closing'
    except orders.OrderError as e:
        store.update_trade(t["id"], expected_status="closing", status="open"); post(f"🚨 close REJECTED on {t['mt5_ticket']} ({reason}): {e} — close manually", key=f"closefail:{t['mt5_ticket']}:{naive(now()):%H%M}"); return
    if d.get("complete") and d.get("pnl") is not None: _close_in_db(t, d, reason, "closing")
    else: _mark_unreconciled({**t, "status": "closing"}, f"close for {reason}: broker deals incomplete")

def flatten_all(live, reason):
    attach_new_positions(live)
    for t in store.open_trades():
        if t["status"] == "open" and t["mt5_ticket"] in live: _exit(t, reason)

def loop_once():
    eng = _engine()
    live = {p["ticket"]: p for p in orders.open_positions()}           # MT5Unavailable -> whole loop skipped
    attach_new_positions(live)
    try:
        h, m = map(int, config.SESSION_FIRST_BAR_UTC.split(":")); day0 = dt.datetime.combine(broker_day(), dt.time(h, m))
        snap = orders.account_snapshot(day0); state = eng.update(snap)
    except orders.MT5Unavailable as e:
        eng.mark_unknown(f"broker data unavailable: {e}"); state = "UNKNOWN"; snap = None
    if snap and snap["other_symbol_positions"]: post(f"ℹ️ positions on other symbols detected: {snap['other_symbol_positions']} — counted in account-level risk, not managed", key=f"othersym:{broker_day()}")
    if state == "LOCKED" and config.FLATTEN_ON_LOCK and live: flatten_all(live, "daily_loss_limit")
    handle_reconcile_request(live); manage(live); retry_uncertain(live)
    if snap: eng.close_day_snapshot(snap)
    unrec = [t["mt5_ticket"] for t in store.open_trades() if t["status"] == "unreconciled"]
    closing = [t["mt5_ticket"] for t in store.open_trades() if t["status"] == "closing"]
    note = " ".join(x for x in [f"RISK={state}", ("UNPROTECTED:" + ",".join(map(str, sorted(UNPROTECTED)))) if UNPROTECTED else "",
                                ("UNRECONCILED:" + ",".join(map(str, unrec))) if unrec else "", ("CLOSING:" + ",".join(map(str, closing))) if closing else ""] if x)
    store.heartbeat("guard", "LIVE", note)

if __name__ == "__main__":
    consecutive = 0
    while True:
        try: loop_once(); consecutive = 0
        except orders.MT5Unavailable as e:
            post(f"⚠️ guard: MT5 unavailable ({e}) — loop skipped, no state changed", key=f"mt5down:{naive(now()):%H%M}"); time.sleep(30)
        except Exception as e:
            consecutive += 1
            try: naked = [p["ticket"] for p in orders.open_positions() if not p["sl"]]
            except Exception: naked = ["unknown — MT5 unreachable"]
            post(f"⚠️ GUARD ERROR `{type(e).__name__}: {e}` — positions WITHOUT stop: {naked or 'none'}", key=f"guarderr:{naive(now()):%H%M}")
            if consecutive >= 5: raise SystemExit(1)
            time.sleep(30)
        time.sleep(15)
