"""Weekend Auto-Replay & Trade Comparison Engine (V1.6.3). READ-ONLY toward MT5: this module imports nothing that can send orders.
Reuses: detector.load_bars + indicators.compute/signal_at/simulate (the live rule, chronological, no lookahead), store/db, alerter.enqueue,
collector jobs ('backfill_range', 'mt5_history') executed by the MT5 box, mt5_history.build_positions, matcher.match.

Job state in weekly_reports: PENDING -> RUNNING -> COMPLETED | INCOMPLETE (data gaps) | FAILED. Unique key (account, symbol, week, strategy).
"""
import os, json, csv, html, datetime as dt, time
import pandas as pd
from . import config, db, indicators, matcher
from .mt5_history import build_positions
from .timeutil import now, naive

SV = config.RULES_VERSION

# ---------------- week & session ----------------
def last_completed_week(t=None):
    """Monday 00:00 UTC of the most recently COMPLETED trading week (Mon..Fri before the current weekend)."""
    t = naive(t or now()); monday = (t - dt.timedelta(days=t.weekday())).date()
    if t.weekday() >= 5 or (t.weekday() == 0 and t.hour < 1): return monday if t.weekday() >= 5 else monday - dt.timedelta(days=7)
    return monday - dt.timedelta(days=7)

def market_closed_now(t=None):
    """Broker-aware: Sat all day, Sun before session open (23:00 UTC or first bar), Fri after 22:00, holidays from market_calendar."""
    t = naive(t or now()); wd, hm = t.weekday(), t.strftime("%H:%M")
    if wd == 5: return True
    if wd == 6: return hm < "23:00"
    if wd == 4 and hm >= "22:00": return True
    hol = db.q("select 1 from market_calendar where kind='holiday' and start_utc<=%s and end_utc>%s", (t, t), one=True)
    return bool(hol)

def should_auto_run(t=None):
    return market_closed_now(t) and naive(t or now()).weekday() in (5, 6)

# ---------------- data coverage ----------------
def expected_bars(ws, we, tf):
    from .scheduler import expected_bars as eb
    hol = {naive(r["start_utc"]).date() for r in db.q("select start_utc from market_calendar where kind='holiday' and start_utc>=%s and start_utc<%s", (ws, we))}
    return eb(ws, we, tf, hol), hol

def coverage(ws, we):
    out = {}
    for tf in ("M30", "H1"):
        exp, hol = expected_bars(ws, we, tf)
        have = {naive(r["time_utc"]) for r in db.q("select time_utc from bars where symbol=%s and tf=%s and time_utc>=%s and time_utc<%s", (config.SYMBOL, tf, ws, we))}
        missing = [t for t in exp if t not in have]
        warm = db.q("select count(*) n from bars where symbol=%s and tf=%s and time_utc<%s", (config.SYMBOL, tf, ws), one=True)["n"]
        out[tf] = dict(expected=len(exp), present=len(exp) - len(missing), missing=len(missing), missing_times=[str(m) for m in missing[:50]],
                       warmup_bars=warm, warmup_ok=warm >= config.EMA_WARMUP_BARS, holidays=sorted(str(h) for h in hol))
    out["complete"] = all(out[tf]["missing"] == 0 and out[tf]["warmup_ok"] for tf in ("M30", "H1"))
    return out

def request_recovery(ws, we, cov, wait_s=600):
    """Ask the MT5 box (collector job) for missing bars; wait for the durable ack; re-check. Never fabricates bars."""
    if cov["complete"]: return cov
    jid = db.request_job("backfill_range", {"start": ws.isoformat(), "end": we.isoformat(), "tfs": ["M30", "H1"]})
    t0 = time.time()
    while time.time() - t0 < wait_s:
        j = db.job_status(jid)
        if j and j["done_at"]: break
        time.sleep(5)
    return coverage(ws, we)

def fetch_history(acct_hint, ws, we, wait_s=600):
    """Ask the MT5 box for the week's deal history (job 'mt5_history'); deals arrive via the ingest API into mt5_deals."""
    jid = db.request_job("mt5_history", {"start": ws.isoformat(), "end": we.isoformat()})
    t0 = time.time(); j = None
    while time.time() - t0 < wait_s:
        j = db.job_status(jid)
        if j and j["done_at"]: break
        time.sleep(5)
    ok = bool(j and j["done_at"] and j["state"] == "done")
    rows = db.q("select * from mt5_deals where symbol=%s and time_utc>=%s and time_utc<%s order by time_utc", (config.SYMBOL, ws - dt.timedelta(days=7), we + dt.timedelta(days=1)))
    for r in rows: r["time_utc"] = naive(r["time_utc"])
    return ok, rows

# ---------------- replay ----------------
def replay_week(ws, we):
    """Chronological replay with the LIVE rule. Returns raw detections (every VWAP cross), each labelled rejected/qualified + hypo result."""
    from .detector import load_bars
    df = load_bars(end=we + dt.timedelta(days=2))
    if df.empty: return []
    d = indicators.compute(df); out = []
    for i in range(2, len(d)):
        t = d.iloc[i]["time_utc"]
        if t < pd.Timestamp(ws) or t >= pd.Timestamp(we): continue
        s = indicators.signal_at(d, i)
        if not s:                                                  # not a cross, or warm-up/invalid
            continue
        if s["features"].get("tradeable"):
            hypo = indicators.simulate(d, i, s["dir"], s["suggested_sl"])
            cat, reason = "qualified", ""
            eligible = d.iloc[i + 1]["time_utc"] if i + 1 < len(d) else t + pd.Timedelta(minutes=30)
        else:
            hypo = None; cat, reason = "rejected", s["features"].get("reject_reason", "filtered"); eligible = None
        out.append(dict(time_utc=t.to_pydatetime(), eligible_time=(eligible.to_pydatetime() if eligible is not None else None), tf=config.RULES["tf"], dir=s["dir"],
                        price=s["price"], category=cat, reason=reason, features=s["features"], hypo=hypo))
    return out

def live_signals(ws, we):
    rows = db.q("""select s.id,s.time_utc,s.dir,s.price,s.suggested_sl,s.features,
                   (select tag from decisions dd where dd.signal_id=s.id order by tagged_at desc limit 1) decision,
                   (select mt5_ticket from trades t where t.signal_id=s.id limit 1) position_id
                   from signals s where s.source='live' and s.symbol=%s and s.time_utc>=%s and s.time_utc<%s and (s.features->>'tradeable')='true' order by s.time_utc""",
                (config.SYMBOL, ws, we))
    for r in rows: r["time_utc"] = naive(r["time_utc"])
    return rows

# ---------------- metrics ----------------
def _pf(pnls):
    g = sum(p for p in pnls if p > 0); l = -sum(p for p in pnls if p < 0); return (g / l) if l else (float("inf") if g else 0.0)
def _maxdd(pnls):
    peak = cum = 0.0; dd = 0.0
    for p in pnls: cum += p; peak = max(peak, cum); dd = min(dd, cum - peak)
    return dd
def session(t): h = t.hour; return "Asia" if h < 7 else "London" if h < 13 else "NY" if h < 21 else "Roll"

def actual_metrics(positions):
    closed = [p for p in positions if p["status"] == "closed"]; pn = [p["net_pnl"] for p in closed]; gr = [p["gross_pnl"] for p in closed]
    w = [x for x in pn if x > 0]; l = [x for x in pn if x <= 0]
    by = lambda key: {k: round(sum(p["net_pnl"] for p in closed if key(p) == k), 2) for k in sorted({key(p) for p in closed})}
    return dict(trades=len(closed), open_positions=len([p for p in positions if p["status"] == "open"]), win_rate=(len(w) / len(closed)) if closed else None, denominator=len(closed),
                gross_pnl=round(sum(gr), 2), net_pnl=round(sum(pn), 2), profit_factor=round(_pf(pn), 3) if closed else None, avg_winner=round(sum(w) / len(w), 2) if w else None,
                avg_loser=round(sum(l) / len(l), 2) if l else None, max_drawdown=round(_maxdd(pn), 2), avg_holding_min=round(sum(p["holding_min"] for p in closed) / len(closed), 1) if closed else None,
                by_weekday=by(lambda p: p["entry_time"].strftime("%a")), by_session=by(lambda p: session(p["entry_time"])),
                by_origin={k: len([p for p in closed if p["origin"] == k]) for k in ("aureon", "discretionary")})

def replay_metrics(dets):
    q = [x for x in dets if x["category"] == "qualified" and x["hypo"]]; resolved = [x for x in q if x["hypo"]["exit_how"] != "AMBIGUOUS"]
    pn = [x["hypo"]["pl_rule"] for x in resolved]; w = [p for p in pn if p > 0]
    return dict(raw_detections=len(dets), rejected=len([x for x in dets if x["category"] == "rejected"]), qualified=len(q), resolved=len(resolved), ambiguous=len(q) - len(resolved),
                hypo_win_rate=(len(w) / len(resolved)) if resolved else None, denominator=len(resolved), hypo_pnl_per_oz=round(sum(pn), 2) if resolved else None,
                hypo_profit_factor=round(_pf(pn), 3) if resolved else None, avg_mfe=round(sum(x["hypo"]["mfe"] for x in q) / len(q), 2) if q else None,
                avg_mae=round(sum(x["hypo"]["mae"] for x in q) / len(q), 2) if q else None,
                by_exit={k: len([x for x in q if x["hypo"]["exit_how"] == k]) for k in sorted({x["hypo"]["exit_how"] for x in q})},
                note="Hypothetical: $/oz per unit, bar-by-bar on M30 OHLC, stop counted first, no spread/slippage. NOT account P&L.")

# ---------------- report files ----------------
def write_reports(folder, meta, dets, positions, matches, unm_sig, unm_pos, am, rm, cov):
    os.makedirs(folder, exist_ok=True); files = {}
    def w(name, rows, fields):
        p = os.path.join(folder, name)
        with open(p, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); wr.writeheader()
            for r in rows: wr.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()})
        files[name] = p
    w("detections.csv", [{**d, "hypo_exit": (d["hypo"] or {}).get("exit_how"), "hypo_pl_per_oz": (d["hypo"] or {}).get("pl_rule"), "mfe": (d["hypo"] or {}).get("mfe"), "mae": (d["hypo"] or {}).get("mae")} for d in dets],
      ["time_utc", "eligible_time", "tf", "dir", "price", "category", "reason", "hypo_exit", "hypo_pl_per_oz", "mfe", "mae", "features"])
    w("actual_trades.csv", positions, ["position_id", "account_id", "symbol", "dir", "entry_time", "exit_time", "entry_price", "exit_price", "volume_in", "volume_out", "fills_in", "fills_out", "partial_close", "gross_pnl", "costs", "net_pnl", "holding_min", "status", "origin", "order_tickets"])
    comp = []
    for m in matches:
        s, p = m["signal"], m["position"]
        comp.append(dict(classification=m["classification"], signal_time=s["time_utc"], signal_dir=s["dir"], signal_price=s["price"], live_signal_id=s.get("live_signal_id"), decision=s.get("decision"),
                         position_id=p["position_id"], actual_entry=p["entry_price"], actual_exit=p["exit_price"], entry_delay_min=m["delay_min"], slippage=m["slippage"],
                         hypo_exit=(s.get("hypo") or {}).get("exit_how"), hypo_pl_per_oz=(s.get("hypo") or {}).get("pl_rule"), actual_net_pnl=p["net_pnl"], actual_exit_reason=p["status"], holding_min=p["holding_min"], mfe=(s.get("hypo") or {}).get("mfe"), mae=(s.get("hypo") or {}).get("mae")))
    for s in unm_sig:
        cls, why = matcher.classify_unmatched_signal(s)
        comp.append(dict(classification=cls, signal_time=s["time_utc"], signal_dir=s["dir"], signal_price=s["price"], live_signal_id=s.get("live_signal_id"), decision=s.get("decision"), reason=why,
                         hypo_exit=(s.get("hypo") or {}).get("exit_how"), hypo_pl_per_oz=(s.get("hypo") or {}).get("pl_rule")))
    for p in unm_pos:
        comp.append(dict(classification="TRADED_WITHOUT_MATCHING_DETECTION", position_id=p["position_id"], actual_entry=p["entry_price"], actual_exit=p["exit_price"], actual_net_pnl=p["net_pnl"], signal_dir=p["dir"], reason=p["origin"]))
    w("comparison.csv", comp, ["classification", "signal_time", "signal_dir", "signal_price", "live_signal_id", "decision", "position_id", "actual_entry", "actual_exit", "entry_delay_min", "slippage", "hypo_exit", "hypo_pl_per_oz", "actual_net_pnl", "actual_exit_reason", "holding_min", "mfe", "mae", "reason"])
    summary = dict(meta=meta, coverage=cov, actual=am, replay=rm, classification_counts={k: len([c for c in comp if c["classification"] == k]) for k in sorted({c["classification"] for c in comp})},
                   matches=len(matches), unmatched_signals=len(unm_sig), unmatched_positions=len(unm_pos), files=files)
    files["summary.json"] = os.path.join(folder, "summary.json"); json.dump(summary, open(files["summary.json"], "w"), indent=2, default=str)
    files["summary.html"] = os.path.join(folder, "summary.html"); open(files["summary.html"], "w").write(render_html(summary, comp))
    return files, summary

def render_html(s, comp):
    e = lambda x: html.escape(str(x)); meta, am, rm, cov = s["meta"], s["actual"], s["replay"], s["coverage"]
    rows = "".join(f"<tr><td>{e(c['classification'])}</td><td>{e(c.get('signal_time',''))}</td><td>{e(c.get('signal_dir',''))}</td><td>{e(c.get('signal_price',''))}</td><td>{e(c.get('decision',''))}</td><td>{e(c.get('position_id',''))}</td><td>{e(c.get('actual_entry',''))}</td><td>{e(c.get('entry_delay_min',''))}</td><td>{e(c.get('hypo_exit',''))}</td><td>{e(c.get('hypo_pl_per_oz',''))}</td><td>{e(c.get('actual_net_pnl',''))}</td><td>{e(c.get('reason',''))}</td></tr>" for c in comp)
    kv = lambda d: "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in d.items())
    status = "COMPLETE" if cov["complete"] else f"INCOMPLETE — missing M30 {cov['M30']['missing']}, H1 {cov['H1']['missing']}"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Aureon weekly review {e(meta['week_label'])}</title>
<style>body{{font:14px system-ui;margin:24px;color:#1a1d24}}table{{border-collapse:collapse;margin:8px 0 20px}}th,td{{border:1px solid #d8dbe2;padding:4px 8px;text-align:left;font-size:13px}}th{{background:#f3f4f6}}h2{{margin-top:28px}}.warn{{color:#b45309}}.sep{{color:#6b7280}}</style></head><body>
<h1>Aureon weekly review — {e(meta['week_label'])} ({e(meta['week_start'])} → {e(meta['week_end'])})</h1>
<p>Account {e(meta['account_id'])} · {e(meta['symbol'])} · strategy {e(meta['strategy_version'])} · generated {e(meta['generated_at'])} · data: <b class="{'' if cov['complete'] else 'warn'}">{e(status)}</b></p>
<h2>Actual account results (broker records)</h2><table>{kv({k: v for k, v in am.items() if not isinstance(v, dict)})}</table>
<table><tr><th>by weekday</th><td>{e(am['by_weekday'])}</td></tr><tr><th>by session</th><td>{e(am['by_session'])}</td></tr><tr><th>by origin</th><td>{e(am['by_origin'])}</td></tr></table>
<h2>Hypothetical replay (strategy rule, $/oz — <span class="sep">kept separate from account P&amp;L</span>)</h2><table>{kv({k: v for k, v in rm.items() if not isinstance(v, dict)})}</table><table><tr><th>by exit</th><td>{e(rm['by_exit'])}</td></tr></table>
<h2>Signal ↔ trade comparison</h2><table>{kv(s['classification_counts'])}</table>
<table><tr><th>class</th><th>signal time</th><th>dir</th><th>signal px</th><th>decision</th><th>position</th><th>actual entry</th><th>delay min</th><th>hypo exit</th><th>hypo $/oz</th><th>actual net $</th><th>reason</th></tr>{rows}</table>
<h2>Data coverage</h2><table>{kv({tf: f"{cov[tf]['present']}/{cov[tf]['expected']} bars, missing {cov[tf]['missing']}, warm-up {cov[tf]['warmup_bars']} ({'ok' if cov[tf]['warmup_ok'] else 'INSUFFICIENT'})" for tf in ('M30','H1')})}</table>
<p class="sep">Read-only report. No orders were placed or modified. Hypothetical results assume bar-by-bar M30 OHLC, stop first, no spread/slippage; AMBIGUOUS = TP and stop in the same bar, unresolved.</p></body></html>"""

# ---------------- orchestration ----------------
def run(week_start=None, force=False, account_id=None):
    ws = pd.Timestamp(week_start).to_pydatetime() if week_start else dt.datetime.combine(last_completed_week(), dt.time(0, 0))
    we = ws + dt.timedelta(days=7); label = f"{ws.isocalendar()[0]}-W{ws.isocalendar()[1]:02d}"
    acct = str(account_id or (db.q("select account_id from risk_state order by updated_at desc limit 1", one=True) or {}).get("account_id") or "unknown")
    existing = db.q("select * from weekly_reports where account_id=%s and symbol=%s and week_start=%s and strategy_version=%s", (acct, config.SYMBOL, ws.date(), SV), one=True)
    if existing and existing["state"] in ("COMPLETED",) and not force: return dict(skipped="already completed", report_id=existing["id"], files=existing["files"])
    if existing and existing["state"] == "RUNNING" and existing["started_at"] and (now() - existing["started_at"]).total_seconds() < 3600 and not force: return dict(skipped="running elsewhere", report_id=existing["id"])
    rid = db.q("""insert into weekly_reports(account_id,symbol,week_start,strategy_version,state,started_at,attempts) values(%s,%s,%s,%s,'RUNNING',now(),1)
                  on conflict(account_id,symbol,week_start,strategy_version) do update set state='RUNNING', started_at=now(), attempts=weekly_reports.attempts+1, error=null returning id""",
               (acct, config.SYMBOL, ws.date(), SV), one=True)["id"]
    try:
        cov = coverage(ws, we)
        if not cov["complete"]: cov = request_recovery(ws, we, cov)
        hist_ok, deals = fetch_history(acct, ws, we)
        positions = [p for p in build_positions(deals, config.SYMBOL) if (p["entry_time"] or p["exit_time"]) and ws <= (p["entry_time"] or p["exit_time"]) < we]
        dets = replay_week(ws, we)
        live = live_signals(ws, we)
        risk_locked_days = {r["broker_day"] for r in db.q("select broker_day from risk_state where account_id=%s and broker_day>=%s and broker_day<%s and state in ('LOCKED','UNKNOWN')", (acct, ws.date(), we.date()))}
        # qualified detections become match candidates; attach the live signal id when a live signal exists for the same bar
        qualified = []
        for n, dd in enumerate(x for x in dets if x["category"] == "qualified"):
            ls = next((l for l in live if abs((l["time_utc"] - dd["time_utc"]).total_seconds()) < 60), None)
            qualified.append(dict(id=n, time_utc=dd["eligible_time"] or dd["time_utc"], dir=dd["dir"], price=dd["price"], hypo=dd["hypo"], live_signal_id=ls["id"] if ls else None,
                                  decision=(ls or {}).get("decision"), position_id=(ls or {}).get("position_id"), risk_blocked=(dd["time_utc"].date() in {d for d in risk_locked_days})))
        matches, unm_sig, unm_pos = matcher.match(qualified, positions)
        am, rm = actual_metrics(positions), replay_metrics(dets)
        meta = dict(account_id=acct, symbol=config.SYMBOL, week_start=str(ws.date()), week_end=str((we - dt.timedelta(days=1)).date()), week_label=label, strategy_version=SV,
                    generated_at=str(now()), history_fetch_ok=hist_ok, live_signals=len(live))
        files, summary = write_reports(os.path.join(config.REVIEW_REPORTS_DIR, label), meta, dets, positions, matches, unm_sig, unm_pos, am, rm, cov)
        with db.conn() as c, c.cursor() as cur:
            cur.execute("delete from replay_detections where report_id=%s", (rid,))
            cur.executemany("insert into replay_detections(report_id,time_utc,tf,dir,price,category,reason,features,live_signal_id,hypo) values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing",
                            [(rid, d["time_utc"], d["tf"], d["dir"], d["price"], d["category"], d["reason"], json.dumps(d["features"], default=str), None, json.dumps(d["hypo"], default=str)) for d in dets])
        state = "COMPLETED" if (cov["complete"] and hist_ok) else "INCOMPLETE"
        db.q("update weekly_reports set state=%s, finished_at=now(), coverage=%s, actual_metrics=%s, replay_metrics=%s, files=%s where id=%s",
             (state, json.dumps(cov, default=str), json.dumps(am, default=str), json.dumps(rm, default=str), json.dumps(files), rid))
        from .alerter import enqueue
        enqueue(f"weekly-report:{acct}:{label}:{SV}:{state}", "report", discord_summary(meta, am, rm, summary, cov, state))
        return dict(report_id=rid, state=state, files=files, summary=summary)
    except Exception as e:
        db.q("update weekly_reports set state='FAILED', finished_at=now(), error=%s where id=%s", (f"{type(e).__name__}: {e}"[:1000], rid))
        from .alerter import enqueue; enqueue(f"weekly-report-failed:{acct}:{label}:{now():%Y%m%d%H}", "report", f"❌ weekly review {label} FAILED: {type(e).__name__}: {e}")
        raise

def discord_summary(meta, am, rm, s, cov, state):
    cc = s["classification_counts"]
    return (f"**📊 Weekly review {meta['week_label']}** ({meta['week_start']} → {meta['week_end']}) · {state}{'' if cov['complete'] else ' — data incomplete, treat as provisional'}\n"
            f"Qualified detections {rm['qualified']} (raw {rm['raw_detections']}, rejected {rm['rejected']}) · trades placed {am['trades']} · matched {s['matches']} · "
            f"missed {cc.get('DETECTED_NOT_TRADED', 0)} · without detection {cc.get('TRADED_WITHOUT_MATCHING_DETECTION', 0)} · risk-rejected {cc.get('REJECTED_SIGNAL', 0)}\n"
            f"**Actual net P&L ${am['net_pnl']:+.2f}** (gross {am['gross_pnl']:+.2f}, win rate {('%.0f%%' % (100*am['win_rate'])) if am['win_rate'] is not None else 'n/a'} of {am['denominator']})\n"
            f"Hypothetical (replay, $/oz, separate): {rm['hypo_pnl_per_oz']} over {rm['resolved']} resolved, {rm['ambiguous']} ambiguous, win rate {('%.0f%%' % (100*rm['hypo_win_rate'])) if rm['hypo_win_rate'] is not None else 'n/a'}\n"
            f"Report: {s['files'].get('summary.html')}")

if __name__ == "__main__":
    import sys
    a = sys.argv; week = a[a.index("--week") + 1] if "--week" in a else None
    r = run(week_start=week, force="--force" in a)
    print(json.dumps({k: v for k, v in r.items() if k != "summary"}, indent=2, default=str))
