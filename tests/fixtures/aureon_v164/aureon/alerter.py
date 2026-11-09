"""Discord. Signal generation and delivery are separate: anything to send is ENQUEUED in `alerts` (durable, idempotent by key)
and a delivery loop moves PENDING -> SENDING -> DELIVERED | FAILED with bounded retries, jitter and Retry-After.
Operator commands (take/skip/pause/resume/reconcile/risk ...) are accepted only from DISCORD_AUTHORIZED_USER_IDS."""
import asyncio, time, random, datetime as dt
import requests
from . import config, db
from .timeutil import now

def _broker_day():
    h, m = map(int, config.SESSION_FIRST_BAR_UTC.split(":")); return (now().replace(tzinfo=None) - dt.timedelta(hours=h, minutes=m)).date().isoformat()
SESSION = lambda h: "Asia" if h < 7 else "London" if h < 13 else "NY" if h < 21 else "Roll"

# ---------------- enqueue (any service) ----------------
def enqueue(key, kind, text):
    """Durable, idempotent. Works in both store modes (guard on the MT5 box enqueues through the API)."""
    try:
        if config.STORE_MODE == "api":
            from .store import store; store.alert_enqueue(key, kind, text)
        else: db.alert_enqueue(key, kind, text)
    except Exception as e: print("[alerts] enqueue failed:", e, "|", text[:120])

def post(text, key=None):
    """Compatibility wrapper used across the code base: enqueue with a generated key (never a direct, unacknowledged send)."""
    enqueue(key or f"adhoc:{now().isoformat(timespec='seconds')}:{abs(hash(text)) % 10**6}", "adhoc", text)

# ---------------- delivery ----------------
def _send(text):
    """One HTTP attempt. Returns (ok, retry_after_seconds|None, error)."""
    if not config.DISCORD_WEBHOOK_URL: print("[discord]", text); return True, None, None
    try:
        r = requests.post(config.DISCORD_WEBHOOK_URL, json={"content": text[:1950]}, timeout=10)
        if 200 <= r.status_code < 300: return True, None, None
        if r.status_code == 429 or r.status_code >= 500:
            try: ra = float(r.headers.get("Retry-After") or r.json().get("retry_after", 2))
            except Exception: ra = 2.0
            return False, ra, f"HTTP {r.status_code}"
        return False, None, f"HTTP {r.status_code} {r.text[:120]}"                     # 4xx: permanent
    except requests.Timeout: return False, 5.0, "timeout"
    except requests.RequestException as e: return False, 5.0, f"network: {type(e).__name__}"

def deliver_pending(max_n=20):
    for _ in range(max_n):
        a = db.alert_claim()
        if not a: return
        ok, ra, err = _send(a["payload"])
        if ok: db.alert_done(a["id"]); continue
        if ra is None or a["attempts"] >= 6: db.alert_failed(a["id"], err); print(f"[alerts] FAILED #{a['id']}: {err}"); continue
        db.alert_retry(a["id"], err, min(300, ra * (2 ** (a["attempts"] - 1))) + random.uniform(0, 1))

# ---------------- signals -> alerts ----------------
def format_signal(s):
    f = s["features"]; d = "BUY" if s["dir"] > 0 else "SELL"
    sl_px = s["price"] - s["suggested_sl"] if s["dir"] > 0 else s["price"] + s["suggested_sl"]
    return (f"**#{s['id']} {d} {config.SYMBOL} M30** @ {s['price']:.2f}  ·  {s['time_utc']:%d %b %H:%M} UTC\n"
            f"SL {sl_px:.2f} ({s['suggested_sl']:.1f} = 1.5 ATR)  ·  exit: opposite VWAP cross / lock ladder\n"
            f"EMA 8/32 {'with' if f['ema_agree'] else 'against'} · 200 {'with' if f['side200']==s['dir'] else 'against'} · "
            f"VWAP flips {f['vwap_flips8']}/8 · {SESSION(f['hour_utc'])} · bar {f['bar_of_day']} of day\n"
            f"Reply `take {s['id']}` or `skip {s['id']} <reason>`  (no reply within 1 bar = skip)")

def risk_allows_entries():
    r = db.q("select state from risk_state where broker_day=%s order by updated_at desc limit 1", (_broker_day(),), one=True)
    return bool(r) and r["state"] in ("UNLOCKED", "WARNING")

def queue_signals():
    rows = db.q("""select * from signals where source='live' and sent_at is null and (features->>'tradeable')='true' and time_utc > now() - interval '2 hours' order by time_utc""")
    if not rows: return
    if not risk_allows_entries():                               # fail closed: LOCKED or UNKNOWN -> no entry alerts today
        db.q("update signals set sent_at=now() where id = any(%s)", ([r["id"] for r in rows],))
        enqueue(f"risk-blocked:{_broker_day()}", "risk", "ℹ️ entry signals suppressed: risk state is not UNLOCKED"); return
    for s in rows:
        enqueue(f"signal:{s['id']}", "signal", format_signal(s)); db.q("update signals set sent_at=now() where id=%s", (s["id"],))

def auto_skip():
    db.q("""insert into decisions(signal_id,tag,via,note) select s.id,'auto_skip','system','no reply within 1 bar' from signals s
            left join decisions d on d.signal_id=s.id where s.source='live' and s.sent_at is not null and d.id is null and s.sent_at < now() - interval '35 minutes'""")

def record(signal_id, tag, note="", via="discord"): db.q("insert into decisions(signal_id,tag,via,note) values(%s,%s,%s,%s)", (signal_id, tag, via, note))

# ---------------- operator commands ----------------
async def bot_loop():
    import discord
    if not config.DISCORD_AUTHORIZED_USER_IDS: enqueue("cfg:no-auth-ids", "ops", "⚠️ DISCORD_AUTHORIZED_USER_IDS is empty — all commands will be rejected until it is set")
    intents = discord.Intents.default(); intents.message_content = True; client = discord.Client(intents=intents)
    @client.event
    async def on_message(m):
        if m.author == client.user or m.channel.id != config.DISCORD_CHANNEL_ID: return
        if m.author.id not in config.DISCORD_AUTHORIZED_USER_IDS: await m.add_reaction("⛔"); return
        p = m.content.strip().split(); who = str(m.author.id)
        if not p: return
        cmd = p[0].lower()
        if cmd in ("take", "skip", "late", "news", "doubt") and len(p) >= 2 and p[1].lstrip("#").isdigit():
            record(int(p[1].lstrip("#")), cmd, " ".join(p[2:])); await m.add_reaction("✅")
        elif cmd == "pause": db.set_control("desired", "PAUSE", who); await m.reply("paused — no new alerts; guard keeps managing open trades")
        elif cmd == "resume": db.set_control("desired", "RUN", who); await m.reply("resume requested — scheduler will backfill, replay, then go LIVE")
        elif cmd == "reconcile" and len(p) == 2 and p[1].isdigit(): db.set_control("reconcile_request", p[1], who); await m.reply(f"guard will re-check ticket {p[1]}")
        elif cmd == "risk" and len(p) >= 2 and p[1] == "status":
            r = db.q("select * from risk_state where broker_day=%s order by updated_at desc limit 1", (_broker_day(),), one=True)
            await m.reply(f"risk {r['state']} · ref {r['ref_equity']} ({r['ref_source']}) · equity {r['equity']} · drawdown {r['drawdown']:+.2f}" if r else "no risk row for today yet")
        elif cmd == "risk" and len(p) >= 3 and p[1] == "clear" and m.author.id in config.DISCORD_ADMIN_USER_IDS:
            from .risk import admin_clear; ok = admin_clear(p[2], who, " ".join(p[3:]) or "operator clear"); await m.reply("lock cleared (audited)" if ok else "nothing to clear")
        elif cmd == "risk" and len(p) >= 4 and p[1] == "baseline" and m.author.id in config.DISCORD_ADMIN_USER_IDS:
            from .risk import admin_baseline; ok = admin_baseline(p[2], who, float(p[3])); await m.reply("baseline set (audited)" if ok else "no risk row")
        elif cmd == "risk": await m.reply("admin only: `risk clear <account> <reason>` · `risk baseline <account> <equity>`")
    await client.start(config.DISCORD_BOT_TOKEN)

def poll_loop():
    """Transient failures back off; the counter resets on every successful poll. Only PERSISTENT failure (10 in a row) exits."""
    fails = 0
    while True:
        try:
            if db.get_state() == "LIVE": queue_signals(); auto_skip()
            deliver_pending(); db.heartbeat("alerter", db.get_state()); fails = 0; delay = 20
        except Exception as e:
            fails += 1; delay = min(300, 20 * 2 ** min(fails, 4)) + random.uniform(0, 3)
            print(f"[alerter] {'transient' if fails < 10 else 'PERSISTENT'} failure #{fails}: {type(e).__name__}: {e} — retry in {delay:.0f}s")
            if fails >= 10: from . import selfheal; selfheal.handle("alerter", e); raise SystemExit(1)
        time.sleep(delay)

if __name__ == "__main__":
    import threading; threading.Thread(target=poll_loop, daemon=True).start()
    if config.DISCORD_BOT_TOKEN: asyncio.run(bot_loop())
    else:
        while True: time.sleep(3600)
