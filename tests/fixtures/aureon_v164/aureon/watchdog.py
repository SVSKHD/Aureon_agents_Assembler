"""Aureon MT5 Unified Watchdog (V1.6.4) — one command supervises the Windows services.

    python -m aureon.watchdog            start, supervise, live dashboard (CTRL+C stops everything cleanly)
    python -m aureon.watchdog --check    preflight only (MT5, account, symbol, timestamp policy, Assembler, auth, config)
    python -m aureon.watchdog --status   one dashboard snapshot from backend acknowledgements, no processes started
    python -m aureon.watchdog --once     preflight + start + one supervision cycle + stop (CI / smoke test)

Services come from aureon/main.py (SERVICES). Default on Windows: collector only. The guard is OFF unless
WATCHDOG_ENABLE_GUARD=true AND the risk preflight passes — the watchdog itself never touches MT5 orders.
Health = backend acknowledgements (heartbeat age, newest M30/H1 bar, newest M30 indicator), not process existence.
Restarts: exponential backoff, bounded budget; HTTP 401/403 in a child's output => FAILED (auth), never retried blindly.
"""
from __future__ import annotations
import os, re, sys, time, json, signal, argparse, datetime as dt, subprocess, threading, logging
from logging.handlers import RotatingFileHandler
from . import config
from .timeutil import now, naive

STATES = ("STARTING", "RUNNING", "DEGRADED", "FAILED", "RESTARTING", "PAUSED", "MARKET_CLOSED", "STOPPED")
AUTH_PATTERNS = (r"\b401\b", r"\b403\b", r"invalid token", r"role .* not allowed", r"service token required", r"write token required")
HEARTBEAT_STALE_S = int(os.getenv("WATCHDOG_STALE_S", "180"))
BACKOFF_BASE_S = float(os.getenv("WATCHDOG_BACKOFF_BASE_S", "5")); BACKOFF_MAX_S = float(os.getenv("WATCHDOG_BACKOFF_MAX_S", "300"))
RESTART_BUDGET = int(os.getenv("WATCHDOG_RESTART_BUDGET", "6")); BUDGET_WINDOW_S = int(os.getenv("WATCHDOG_BUDGET_WINDOW_S", "1800"))
TICK_S = float(os.getenv("WATCHDOG_TICK_S", "5")); LOG_DIR = os.getenv("WATCHDOG_LOG_DIR", "logs")

def discover_services():
    """Names and CLI args from aureon/main.py — never guessed."""
    from . import main as m
    return {name: ["-m", "aureon.main", name] for name in m.SERVICES}

def configured_services():
    names = [s.strip() for s in os.getenv("WATCHDOG_SERVICES", "collector").split(",") if s.strip()]
    known = discover_services()
    unknown = [n for n in names if n not in known]
    if unknown: raise SystemExit(f"WATCHDOG_SERVICES names unknown to aureon.main: {unknown}; available: {sorted(known)}")
    if os.getenv("WATCHDOG_ENABLE_GUARD", "false").lower() == "true" and "guard" not in names: names.append("guard")
    if os.getenv("WATCHDOG_ENABLE_GUARD", "false").lower() != "true" and "guard" in names: names.remove("guard")
    return names

# ---------------- secrets & logging ----------------
def secret_values():
    vals = []
    for k, v in os.environ.items():
        if v and len(v) >= 8 and any(t in k.upper() for t in ("TOKEN", "PASSWORD", "SECRET", "KEY", "WEBHOOK")): vals.append(v)
    return vals

def redact(line, secrets=None):
    for v in (secrets if secrets is not None else secret_values()): line = line.replace(v, "***")
    return re.sub(r"(postgresql://[^:]+:)[^@]+@", r"\1***@", line)

def child_logger(name):
    os.makedirs(LOG_DIR, exist_ok=True)
    lg = logging.getLogger(f"aureon.child.{name}"); lg.setLevel(logging.INFO); lg.propagate = False
    if not lg.handlers:
        h = RotatingFileHandler(os.path.join(LOG_DIR, f"{name}.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s")); lg.addHandler(h)
    return lg

# ---------------- market ----------------
def market_closed(t=None):
    t = naive(t or now()); wd, hm = t.weekday(), t.strftime("%H:%M")
    return wd == 5 or (wd == 6 and hm < "23:00") or (wd == 4 and hm >= "22:00")

# ---------------- preflight ----------------
class Check:
    def __init__(self, name, ok, detail="", fatal=True): self.name, self.ok, self.detail, self.fatal = name, ok, detail, fatal
    def row(self): return f"  {'ok  ' if self.ok else ('FAIL' if self.fatal else 'warn')} {self.name:<22} {self.detail}"

def preflight(need_mt5=True, need_api=True):
    checks = []
    # config
    missing = [k for k in ("STORE_MODE", "SYMBOL", "MT5_TIME_POLICY") if not os.getenv(k)]
    if config.STORE_MODE == "api": missing += [k for k in ("ASSEMBLER_API_URL",) if not os.getenv(k)]
    if config.STORE_MODE == "api" and not (os.getenv("ASSEMBLER_API_COLLECTOR_TOKEN") or os.getenv("ASSEMBLER_API_SVC_TOKEN")): missing.append("ASSEMBLER_API_COLLECTOR_TOKEN")
    checks.append(Check("config", not missing, "all required keys present" if not missing else f"missing: {missing}"))
    checks.append(Check("paper mode", config.MT5_PAPER, "MT5_PAPER=true" if config.MT5_PAPER else "MT5_PAPER=false — REAL account changes allowed", fatal=False))
    # mt5
    acct = None
    if need_mt5:
        try:
            import MetaTrader5 as mt5
            if not mt5.initialize(): checks.append(Check("mt5 init", False, str(mt5.last_error())))
            else:
                a = mt5.account_info(); s = mt5.symbol_info(config.SYMBOL); sel = mt5.symbol_select(config.SYMBOL, True)
                checks.append(Check("mt5 account", a is not None, f"login {a.login} · {['demo','contest','REAL'][a.trade_mode]} · {a.currency} · equity {a.equity:.2f}" if a else "account_info None"))
                if a and a.trade_mode == 2 and config.MT5_PAPER: checks.append(Check("account vs MT5_PAPER", False, "REAL account while MT5_PAPER=true: guard would refuse every order change (by design)", fatal=False))
                checks.append(Check("symbol", bool(s) and sel, f"{config.SYMBOL} visible, digits {s.digits}, stops level {s.trade_stops_level}" if s else f"{config.SYMBOL} not found"))
                acct = a; mt5.shutdown()
        except ImportError: checks.append(Check("mt5 package", False, "MetaTrader5 not installed (Windows only)"))
        except Exception as e: checks.append(Check("mt5", False, f"{type(e).__name__}: {e}"))
        try:
            from .collector import check_timestamp_policy
            checks.append(Check("timestamp policy", True, f"{config.MT5_TIME_POLICY}: {check_timestamp_policy()}"))
        except Exception as e: checks.append(Check("timestamp policy", False, redact(str(e))))
    # api
    if need_api and config.STORE_MODE == "api":
        import requests
        base = os.getenv("ASSEMBLER_API_URL", "").rstrip("/")
        try:
            r = requests.get(f"{base}/healthz", timeout=10); checks.append(Check("assembler reachable", r.ok, f"{base}/healthz -> {r.status_code}"))
        except Exception as e: checks.append(Check("assembler reachable", False, f"{base}: {type(e).__name__}"))
        for role, env in (("collector", "ASSEMBLER_API_COLLECTOR_TOKEN"), ("guard", "ASSEMBLER_API_GUARD_TOKEN")):
            tok = os.getenv(env) or os.getenv("ASSEMBLER_API_SVC_TOKEN")
            if not tok: checks.append(Check(f"auth {role}", role != "guard" or os.getenv("WATCHDOG_ENABLE_GUARD", "false") != "true", "no token", fatal=(role == "collector"))); continue
            try:
                r = requests.get(f"{base}/api/v1/ingest/state", headers={"Authorization": f"Bearer {tok}"}, timeout=10)
                checks.append(Check(f"auth {role}", r.status_code == 200, f"HTTP {r.status_code}" + (" — token rejected (401/403): fix the token, restarting will not help" if r.status_code in (401, 403) else ""), fatal=(role == "collector")))
            except Exception as e: checks.append(Check(f"auth {role}", False, type(e).__name__, fatal=(role == "collector")))
    # guard gate
    if os.getenv("WATCHDOG_ENABLE_GUARD", "false").lower() == "true":
        ok = acct is not None and config.MT5_PAPER and all(c.ok for c in checks if c.name in ("config", "auth guard", "symbol"))
        checks.append(Check("guard risk preflight", ok, "guard enabled: paper account, symbol and guard auth verified" if ok else "guard NOT started: needs MT5 account, MT5_PAPER=true, symbol and guard token"))
    else: checks.append(Check("guard", True, "disabled (WATCHDOG_ENABLE_GUARD=false): no position changes will be made", fatal=False))
    return checks

# ---------------- supervised service ----------------
class Service:
    def __init__(self, name, args, secrets):
        self.name, self.args, self.secrets = name, args, secrets
        self.proc = None; self.state = "STOPPED"; self.pid = None; self.restarts = 0; self.restart_times = []; self.last_error = ""; self.last_hb = None; self.last_ack = ""
        self.started_at = None; self.next_start_at = 0.0; self.auth_failed = False; self.log = child_logger(name); self._reader = None

    def start(self):
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen([sys.executable, *self.args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, creationflags=flags, env=os.environ.copy())
        self.pid = self.proc.pid; self.state = "STARTING"; self.started_at = time.time()
        self._reader = threading.Thread(target=self._pump, daemon=True); self._reader.start()

    def _pump(self):
        for line in self.proc.stdout or []:
            line = redact(line.rstrip("\n"), self.secrets); self.log.info(line)
            low = line.lower()
            if any(re.search(p, low) for p in AUTH_PATTERNS): self.auth_failed = True; self.last_error = line[:160]
            elif "error" in low or "traceback" in low or "refuses" in low: self.last_error = line[:160]

    def alive(self): return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout=15):
        if not self.alive(): self.state = "STOPPED"; return
        try:
            if os.name == "nt": self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            else: self.proc.terminate()
            self.proc.wait(timeout)
        except Exception:
            try: self.proc.kill()
            except Exception: pass
        self.state = "STOPPED"

    def schedule_restart(self, reason):
        t = time.time(); self.restart_times = [x for x in self.restart_times if t - x < BUDGET_WINDOW_S]
        if len(self.restart_times) >= RESTART_BUDGET:
            self.state = "FAILED"; self.last_error = f"restart budget exhausted ({RESTART_BUDGET}/{BUDGET_WINDOW_S}s): {reason}"; return False
        delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2 ** len(self.restart_times))); self.restart_times.append(t); self.restarts += 1
        self.next_start_at = t + delay; self.state = "RESTARTING"; self.last_error = f"{reason}; retry in {delay:.0f}s"; return True

# ---------------- watchdog ----------------
class Watchdog:
    def __init__(self, names, store=None, notify=None, clock=time.time):
        all_args = discover_services(); self.secrets = secret_values(); self.clock = clock
        self.services = {n: Service(n, all_args[n], self.secrets) for n in names}
        self._store = store; self.notify = notify or self._notify; self.stop_flag = False; self.backend = {}; self.backend_error = ""

    @property
    def store(self):
        if self._store is None:
            from .store import get_store; self._store = get_store()
        return self._store

    def _notify(self, text, key):
        try: self.store.alert_enqueue(key, "watchdog", text)
        except Exception as e:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "notifications_fallback.log"), "a", encoding="utf-8") as f: f.write(f"{now().isoformat()} {text}  [api unavailable: {type(e).__name__}]\n")
            url = os.getenv("DISCORD_WEBHOOK_URL")
            if url:
                try: import requests; requests.post(url, json={"content": text[:1900]}, timeout=10)
                except Exception: pass

    def refresh_backend(self):
        try: self.backend = self.store.health_summary() or {}; self.backend_error = ""
        except Exception as e:
            self.backend_error = f"{type(e).__name__}: {redact(str(e), self.secrets)[:120]}"
            if any(re.search(p, self.backend_error.lower()) for p in AUTH_PATTERNS):
                for s in self.services.values(): s.auth_failed = True; s.last_error = "backend rejected credentials (401/403)"

    def evaluate(self):
        """Health from acknowledgements. Returns dict name -> state."""
        t = now(); closed = market_closed(t); desired = (self.backend or {}).get("control_desired", "RUN"); hbs = {h["service"]: h for h in (self.backend or {}).get("heartbeats", [])}
        bars = {b["tf"]: b for b in (self.backend or {}).get("bars", [])}
        for s in self.services.values():
            hb = hbs.get(s.name)
            if hb:
                lb = hb["last_beat"]; lb = dt.datetime.fromisoformat(lb) if isinstance(lb, str) else lb
                s.last_hb = lb if lb.tzinfo else lb.replace(tzinfo=dt.timezone.utc); age = (t - s.last_hb).total_seconds()
            else: age = None
            if s.auth_failed: s.state = "FAILED"; continue
            if s.state in ("RESTARTING", "FAILED", "STOPPED") and not s.alive(): continue
            if not s.alive():
                rc = s.proc.returncode if s.proc else None
                if not s.schedule_restart(f"exited rc={rc}"): self.notify(f"🚨 {s.name} FAILED: {s.last_error}", f"wd-fail:{s.name}:{t:%Y%m%d%H}")
                continue
            if desired == "PAUSE": s.state = "PAUSED"; continue
            if s.name == "collector":
                m30, h1 = bars.get("M30"), bars.get("H1")
                def age_of(b):
                    if not b or not b.get("t"): return None
                    x = b["t"]; x = dt.datetime.fromisoformat(x) if isinstance(x, str) else x; x = x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
                    return (t - x).total_seconds() / 60
                a30, a60 = age_of(m30), age_of(h1)
                s.last_ack = f"M30 bar {a30:.0f}m · H1 bar {a60:.0f}m · ind {self._ind_age(t)}" if a30 is not None and a60 is not None else "no bars acknowledged yet"
                if closed: s.state = "MARKET_CLOSED" if (age is None or age < HEARTBEAT_STALE_S) else "DEGRADED"; continue
                fresh_bars = a30 is not None and a30 < 90 and a60 is not None and a60 < 150
                stale_hb = age is None or age > HEARTBEAT_STALE_S
                s.state = "RUNNING" if (fresh_bars and not stale_hb) else ("STARTING" if (time.time() - (s.started_at or 0)) < 120 else "DEGRADED")
                if s.state == "DEGRADED": s.last_error = s.last_error or ("heartbeat stale" if stale_hb else "bars not advancing (backend has not acknowledged new M30/H1 bars)")
            else:
                s.last_ack = f"heartbeat {age:.0f}s ago · {hb.get('note','')}" if age is not None else "no heartbeat yet"
                if closed and s.name == "guard": s.state = "MARKET_CLOSED" if (age is None or age < HEARTBEAT_STALE_S) else "DEGRADED"; continue
                stale = age is None or age > HEARTBEAT_STALE_S
                s.state = "RUNNING" if not stale else ("STARTING" if (time.time() - (s.started_at or 0)) < 120 else "DEGRADED")
                if s.state == "DEGRADED": s.last_error = s.last_error or "heartbeat stale"
        return {n: s.state for n, s in self.services.items()}

    def _ind_age(self, t):
        x = (self.backend or {}).get("indicators_m30_latest")
        if not x: return "n/a"
        x = dt.datetime.fromisoformat(x) if isinstance(x, str) else x; x = x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
        return f"{(t - x).total_seconds()/60:.0f}m"

    def tick(self):
        for s in self.services.values():
            if s.state == "RESTARTING" and not s.alive() and time.time() >= s.next_start_at and not s.auth_failed:
                s.start(); self.notify(f"♻️ {s.name} restarted (#{s.restarts})", f"wd-restart:{s.name}:{s.restarts}")
        self.refresh_backend(); return self.evaluate()

    def start_all(self):
        for s in self.services.values(): s.start()
        self.notify(f"🟢 Aureon MT5 started: {', '.join(self.services)} · paper={config.MT5_PAPER} · guard={'on' if 'guard' in self.services else 'off'}", f"wd-start:{now():%Y%m%d%H%M}")

    def stop_all(self):
        for s in self.services.values(): s.stop()
        self.notify("🔴 Aureon MT5 stopped by operator", f"wd-stop:{now():%Y%m%d%H%M}")

    def dashboard(self):
        t = now(); lines = [f"Aureon MT5 — {t:%Y-%m-%d %H:%M:%S} UTC · market {'CLOSED' if market_closed(t) else 'open'} · scheduler {self.backend.get('state','?')} · desired {self.backend.get('control_desired','?')} · risk {(self.backend.get('risk') or {}).get('state','?')}"]
        if self.backend_error: lines.append(f"  backend: {self.backend_error}")
        lines.append(f"  {'service':<10}{'pid':<8}{'state':<14}{'last heartbeat':<22}{'restarts':<9}last ack / error")
        for s in self.services.values():
            hb = f"{(t - s.last_hb).total_seconds():.0f}s ago" if s.last_hb else "—"
            lines.append(f"  {s.name:<10}{str(s.pid or '-'):<8}{s.state:<14}{hb:<22}{s.restarts:<9}{s.last_ack}{(' · ' + s.last_error) if s.last_error else ''}")
        if "guard" not in self.services: lines.append("  guard     -       DISABLED      —                     -        set WATCHDOG_ENABLE_GUARD=true (+ risk preflight) to manage positions")
        return "\n".join(lines)

    def run(self, once=False):
        self.start_all(); self.install_signals()
        try:
            while not self.stop_flag:
                states = self.tick()
                print("\x1b[2J\x1b[H" + self.dashboard(), flush=True)
                for n, st in states.items():
                    s = self.services[n]
                    if st == "FAILED" and s.auth_failed and not getattr(s, "_auth_notified", False):
                        s._auth_notified = True; self.notify(f"⛔ {n}: authentication failure (401/403) — not retrying. Fix the token and restart the watchdog.", f"wd-auth:{n}")
                if once: break
                time.sleep(TICK_S)
        finally: self.stop_all()

    def install_signals(self):
        def handler(sig, frame): self.stop_flag = True
        signal.signal(signal.SIGINT, handler)
        if hasattr(signal, "SIGBREAK"): signal.signal(signal.SIGBREAK, handler)
        signal.signal(signal.SIGTERM, handler)

def main(argv=None):
    p = argparse.ArgumentParser(prog="aureon.watchdog"); p.add_argument("--check", action="store_true"); p.add_argument("--status", action="store_true"); p.add_argument("--once", action="store_true")
    a = p.parse_args(argv)
    if a.status:
        w = Watchdog([]); w.refresh_backend(); print(w.dashboard()); return 0
    checks = preflight(); print("Aureon preflight"); [print(c.row()) for c in checks]
    fatal = [c for c in checks if not c.ok and c.fatal]
    if fatal: print(f"\n{len(fatal)} fatal check(s) failed — not starting."); return 2
    if a.check: print("\npreflight OK"); return 0
    names = configured_services()
    if "guard" in names and not next((c for c in checks if c.name == "guard risk preflight"), Check("x", False)).ok: names.remove("guard"); print("guard not started: risk preflight failed")
    w = Watchdog(names); print(f"\nAureon MT5 started — supervising {names} (CTRL+C to stop)\n"); w.run(once=a.once); return 0

if __name__ == "__main__": raise SystemExit(main())
