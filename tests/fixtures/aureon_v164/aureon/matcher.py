"""Deterministic one-to-one matching of qualified signals to actual positions. Preferred key: Aureon request/signal id.
Fallback: same symbol + direction, execution within [eligible_time, eligible_time + MAX_ENTRY_DELAY_MIN], price within MAX_PRICE_DEV;
greedy by smallest entry delay; each position matched at most once; ties broken by earliest signal then lowest position id."""
import datetime as dt
from . import config

def classify_delay(delay_min, bars_min=30):
    if delay_min < 0: return "EARLY_ENTRY"
    if delay_min > bars_min: return "LATE_ENTRY"
    return "DETECTED_AND_TRADED"

def match(signals, positions, max_delay_min=None, max_price_dev=None):
    """signals: [{id, time_utc(eligible), dir, price, live_signal_id, decision}] ; positions: from mt5_history.build_positions.
    Returns (matches, unmatched_signals, unmatched_positions). matches: [{signal, position, classification, delay_min, slippage}]."""
    max_delay_min = max_delay_min or config.REVIEW_MAX_ENTRY_DELAY_MIN; max_price_dev = max_price_dev or config.REVIEW_MAX_PRICE_DEV
    pos_by_rid = {}
    for p in positions:
        for rid in p.get("request_ids", []): pos_by_rid[rid] = p
    used = set(); matches = []; left_sig = []
    # pass 1: explicit ids (trades table links signal_id -> mt5 position via ticket; caller passes signal['position_id'] when known)
    for s in sorted(signals, key=lambda x: (x["time_utc"], x["id"])):
        p = next((q for q in positions if s.get("position_id") and q["position_id"] == s["position_id"] and q["position_id"] not in used), None)
        if p: used.add(p["position_id"]); matches.append(_m(s, p, "id")); continue
        left_sig.append(s)
    # pass 2: time/price window, greedy by delay
    cands = []
    for s in left_sig:
        for p in positions:
            if p["position_id"] in used or p["status"] == "open" or p["symbol"] != config.SYMBOL: continue
            if p["dir"] != s["dir"]:
                delay = (p["entry_time"] - s["time_utc"]).total_seconds() / 60
                if -5 <= delay <= max_delay_min: cands.append((abs(delay) + 1e6, s, p, "DIRECTION_MISMATCH"))      # considered last
                continue
            delay = (p["entry_time"] - s["time_utc"]).total_seconds() / 60
            if -5 <= delay <= max_delay_min and abs(p["entry_price"] - s["price"]) <= max_price_dev: cands.append((abs(delay), s, p, None))
    taken_s = set()
    for _, s, p, forced in sorted(cands, key=lambda c: (c[0], c[1]["time_utc"], c[2]["position_id"])):
        if s["id"] in taken_s or p["position_id"] in used: continue
        used.add(p["position_id"]); taken_s.add(s["id"]); matches.append(_m(s, p, "window", forced))
    unmatched_sig = [s for s in left_sig if s["id"] not in taken_s]
    unmatched_pos = [p for p in positions if p["position_id"] not in used]
    return matches, unmatched_sig, unmatched_pos

def _m(s, p, how, forced=None):
    delay = (p["entry_time"] - s["time_utc"]).total_seconds() / 60 if p["entry_time"] else None
    cls = forced or ("PARTIAL_EXECUTION" if p.get("partial_close") or p.get("fills_in", 1) > 1 else classify_delay(delay))
    return dict(signal=s, position=p, how=how, classification=cls, delay_min=delay, slippage=((p["entry_price"] - s["price"]) * s["dir"]) if p["entry_price"] else None)

def classify_unmatched_signal(s):
    d = (s.get("decision") or "").lower()
    if d in ("skip", "late", "news", "doubt", "auto_skip"): return "DETECTED_NOT_TRADED", f"operator:{d}"
    if s.get("risk_blocked"): return "REJECTED_SIGNAL", "risk state blocked entries"
    if s.get("decision") == "take": return "UNMATCHED_OR_UNCERTAIN", "taken per decision but no broker position matched"
    return "DETECTED_NOT_TRADED", "no decision recorded"
