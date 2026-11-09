"""Sunday job: verify week's bars, hypothetical outcome per signal, decisions vs bot, Gemini summary."""
import sys, json, datetime as dt
import pandas as pd
from . import config, db, detector, indicators, alerter

RV = config.RULES_VERSION

def fill_outcomes(week_start):
    ws = pd.Timestamp(week_start); we = ws + pd.Timedelta(days=7)
    df = detector.load_bars(end=we + pd.Timedelta(days=2))
    d = indicators.compute(df); idx = {t: i for i, t in enumerate(d["time_utc"])}
    sigs = db.q("""select * from signals where source='live' and rules_version=%s and (features->>'tradeable')='true'
                   and time_utc>=%s and time_utc<%s""", (RV, ws, we))
    for s in sigs:
        i = idx.get(pd.Timestamp(s["time_utc"]).tz_localize(None))
        if i is None: continue
        o = indicators.simulate(d, i, s["dir"], s["suggested_sl"])
        if not o: continue
        db.q("""insert into hypo_outcomes(signal_id,rules_version,mfe,mae,bars_to_20,dip_before_20,secured,pl_rule,exit_how)
                values(%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(signal_id,rules_version) do update set
                mfe=excluded.mfe,mae=excluded.mae,bars_to_20=excluded.bars_to_20,dip_before_20=excluded.dip_before_20,
                secured=excluded.secured,pl_rule=excluded.pl_rule,exit_how=excluded.exit_how,computed_at=now()""",
             (s["id"], RV, o["mfe"], o["mae"], o["bars_to_20"], o["dip_before_20"], o["secured"], o["pl_rule"], o["exit_how"]))
    return len(sigs)

def compare(week_start):
    ws = pd.Timestamp(week_start); we = ws + pd.Timedelta(days=7)
    rows = db.q("""select s.id, s.time_utc, s.dir, h.pl_rule, h.exit_how, h.secured,
                   coalesce((select tag from decisions d where d.signal_id=s.id order by tagged_at desc limit 1),'auto_skip') tag,
                   (select note from decisions d where d.signal_id=s.id order by tagged_at desc limit 1) note,
                   (select pnl_usd from trades t where t.signal_id=s.id limit 1) pnl_you, (select status from trades t where t.signal_id=s.id limit 1) trade_status, s.features
                   from signals s join hypo_outcomes h on h.signal_id=s.id and h.rules_version=%s
                   where s.source='live' and s.time_utc>=%s and s.time_utc<%s order by s.time_utc""", (RV, ws, we))
    lots = config.LOTS_DEFAULT; k = config.USD_PER_DOLLAR_PER_LOT
    rep = dict(alerts=len(rows), taken=0, skipped=0, unreconciled=0, both_right=0, both_wrong=0, bot_right_you_wrong=0, you_right_bot_wrong=0, pl_bot_usd=0.0, pl_you_usd=0.0)
    lines = []
    for r in rows:
        bot = r["pl_rule"] * lots * k; rep["pl_bot_usd"] += bot
        took = r["tag"] == "take"
        if took and (r["pnl_you"] is None or r.get("trade_status") == "unreconciled"):   # taken but no reconciled P&L -> report, never count as 0
            rep.setdefault("unreconciled", 0); rep["unreconciled"] += 1
            lines.append(f"#{r['id']} taken but no trade P&L on record — check guard/MT5"); continue
        you = (r["pnl_you"] or 0.0) if took else 0.0; rep["pl_you_usd"] += you
        rep["taken" if took else "skipped"] += 1
        if took:
            if bot > 0 and you > 0: rep["both_right"] += 1
            elif bot <= 0 and you <= 0: rep["both_wrong"] += 1
            elif bot > 0: rep["bot_right_you_wrong"] += 1
            else: rep["you_right_bot_wrong"] += 1
        else:
            if bot > 0: rep["bot_right_you_wrong"] += 1       # skipped a winner
            else: rep["you_right_bot_wrong"] += 1              # skipped a loser
        lines.append(f"#{r['id']} {r['time_utc']:%a %H:%M} {'BUY' if r['dir']>0 else 'SELL'} bot {bot:+.0f}$ ({r['exit_how']}, safe {r['secured']:.0f}) you: {r['tag']}{' '+r['note'] if r['note'] else ''} {('%+.0f$' % you) if took else ''}")
    return rep, lines

def gemini_summary(rep, lines):
    if not config.GEMINI_API_KEY: return ""
    try:
        from google import genai
        client = genai.Client(api_key=config.GEMINI_API_KEY)
        prompt = ("You are reviewing one week of a gold M30 alert system. Rule: VWAP cross + EMA 8/32 agree. "
                  "Write at most 10 plain lines: where the trader's skips/takes beat or lost to the bot, which skip reasons "
                  "recur, and ONE candidate rule change worth backtesting. Do not invent numbers.\n\n"
                  + json.dumps(rep) + "\n" + "\n".join(lines))
        r = client.models.generate_content(model=config.GEMINI_MODEL, contents=prompt)
        return r.text.strip()
    except Exception as e:
        return f"(summary unavailable: {e})"

def run(week_start, verify=True):
    if verify:
        jid = db.request_job("verify_week", {"week_start": week_start})
        import time; j = None
        for _ in range(60):
            j = db.job_status(jid)
            if j and j["done_at"]: break
            time.sleep(5)
        if j and j["done_at"] and j["ok"]:
            ws = pd.Timestamp(week_start); we = ws + pd.Timedelta(days=7)
            db.q("update bars set verified_week=true where symbol=%s and time_utc>=%s and time_utc<%s", (config.SYMBOL, ws, we))
        else:
            print("verify not completed:", j["result"] if j else "collector did not respond"); alerter.post(f"weekly verify for {week_start} did not complete — review built on unverified bars")
    n = fill_outcomes(week_start)
    rep, lines = compare(week_start)
    summary = gemini_summary(rep, lines)
    db.q("""insert into weekly_review(week_start,alerts,taken,skipped,both_right,both_wrong,bot_right_you_wrong,you_right_bot_wrong,pl_bot_usd,pl_you_usd,summary)
            values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict(week_start) do update set alerts=excluded.alerts,taken=excluded.taken,
            skipped=excluded.skipped,both_right=excluded.both_right,both_wrong=excluded.both_wrong,bot_right_you_wrong=excluded.bot_right_you_wrong,
            you_right_bot_wrong=excluded.you_right_bot_wrong,pl_bot_usd=excluded.pl_bot_usd,pl_you_usd=excluded.pl_you_usd,summary=excluded.summary,created_at=now()""",
         (week_start, rep["alerts"], rep["taken"], rep["skipped"], rep["both_right"], rep["both_wrong"], rep["bot_right_you_wrong"], rep["you_right_bot_wrong"], rep["pl_bot_usd"], rep["pl_you_usd"], summary))
    msg = (f"**Weekly review {week_start}** · alerts {rep['alerts']} · taken {rep['taken']} · skipped {rep['skipped']}\n"
           f"both right {rep['both_right']} · both wrong {rep['both_wrong']} · bot right/you wrong {rep['bot_right_you_wrong']} · you right/bot wrong {rep['you_right_bot_wrong']}\n"
           f"$ if you followed the bot blindly: {rep['pl_bot_usd']:+.0f} · $ you made: {rep['pl_you_usd']:+.0f}" + (f" · ⚠️ {rep['unreconciled']} taken trade(s) without P&L" if rep['unreconciled'] else "") + "\n" + "\n".join(lines[:25]) + ("\n" + summary if summary else ""))
    alerter.post(msg[:1900]); print(msg)

if __name__ == "__main__":
    ws = sys.argv[sys.argv.index("--week") + 1] if "--week" in sys.argv else (dt.date.today() - dt.timedelta(days=dt.date.today().weekday() + 7)).isoformat()
    run(ws, verify="--no-verify" not in sys.argv)
