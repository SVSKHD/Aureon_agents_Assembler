"""Storage facade. STORE_MODE=db (Linux services) or STORE_MODE=api (Windows MT5 box through /api/v1/ingest/*)."""
import os, datetime as dt
from . import config

class DbStore:
    def __init__(self):
        from . import db; self.db = db
    def upsert_bars(self, rows, tf, source="live"): self.db.upsert_bars(rows, tf, source=source)
    def heartbeat(self, service, state, note=""): self.db.heartbeat(service, state, note)
    def get_state(self): return self.db.get_state()
    def last_closed_m30(self):
        return self.db.q("""select b.time_utc,b.open,b.high,b.low,b.close,i.vwap_side,i.bar_of_day,i.atr14 from bars b join indicators i
            on i.symbol=b.symbol and i.tf=b.tf and i.time_utc=b.time_utc and i.rules_version=%s
            where b.symbol=%s and b.tf='M30' order by b.time_utc desc limit 1""", (config.RULES_VERSION, config.SYMBOL), one=True)
    def recent_signal(self, direction):
        return self.db.q("""select id,suggested_sl,dir from signals where source='live' and (features->>'tradeable')='true'
            and dir=%s and time_utc > now()-interval '3 hours' order by time_utc desc limit 1""", (direction,), one=True)
    def open_trades(self): return self.db.q("select * from trades where status in ('open','closing','unreconciled')")
    def trade_by_ticket(self, ticket): return self.db.q("select id,status from trades where mt5_ticket=%s", (ticket,), one=True)
    def insert_trade(self, t):
        r = self.db.q("""insert into trades(signal_id,mt5_ticket,dir,lots,entry_time,entry_price,sl,tp,status)
            values(%s,%s,%s,%s,%s,%s,%s,%s,'open') on conflict(mt5_ticket) do nothing returning id""",
            (t["signal_id"], t["mt5_ticket"], t["dir"], t["lots"], t["entry_time"], t["entry_price"], t["sl"], t["tp"]), one=True)
        if r: return r["id"]
        return self.db.q("select id from trades where mt5_ticket=%s", (t["mt5_ticket"],), one=True)["id"]   # duplicate ticket -> existing
    def update_trade(self, trade_id, expected_status=None, **f):
        """Atomic: when expected_status is given the row must still be in that status, else returns False (lost race)."""
        cols = ", ".join(f"{k}=%s" for k in f)
        if expected_status is None:
            self.db.q(f"update trades set {cols} where id=%s", (*f.values(), trade_id)); return True
        r = self.db.q(f"update trades set {cols} where id=%s and status=%s returning id", (*f.values(), trade_id, expected_status), one=True)
        return bool(r)
    def event(self, trade_id, event, old_sl=None, new_sl=None, note=""):
        self.db.q("insert into trade_events(trade_id,event,old_sl,new_sl,note) values(%s,%s,%s,%s,%s)", (trade_id, event, old_sl, new_sl, note))
    def daily_pnl(self):
        return self.db.q("select coalesce(sum(pnl_usd),0) pnl from trades where status='closed' and exit_time::date=current_date", one=True)["pnl"]
    def claim_job(self, worker): return self.db.claim_job(worker)
    def finish_job(self, job_id, ok, result="", worker=None, token=None): return self.db.finish_job(job_id, ok, result, worker, token)
    def renew_job(self, job_id, worker, token): return self.db.renew_job(job_id, worker, token)
    def risk_get(self, day, acct): return self.db.risk_get(day, acct)
    def risk_upsert(self, r): self.db.risk_upsert(r)
    def risk_event(self, day, acct, event, detail, actor): self.db.risk_event(day, acct, event, detail, actor)
    def alert_enqueue(self, key, kind, payload): self.db.alert_enqueue(key, kind, payload)
    def ingest_deals(self, deals): self.db.ingest_deals(deals)
    def health_summary(self):
        hb = self.db.q("select service,state,last_beat,note from service_heartbeats")
        bars = self.db.q("select tf, max(time_utc) t, count(*) filter (where time_utc > now() - interval '1 day') n24 from bars where symbol=%s group by tf", (config.SYMBOL,))
        return {"state": self.db.get_state(), "heartbeats": hb, "bars": bars, "control_desired": self.db.get_control("desired", "RUN")}
    def get_control(self, key, default=None): return self.db.get_control(key, default)
    def set_control(self, key, value, by="guard"): self.db.set_control(key, value, by)

class ApiStore:
    def __init__(self):
        from .api_client import AssemblerClient
        if not os.getenv("ASSEMBLER_API_URL"): raise RuntimeError("STORE_MODE=api needs ASSEMBLER_API_URL (and a collector/guard token); for direct Postgres use STORE_MODE=db + DATABASE_URL")
        role_env = "ASSEMBLER_API_GUARD_TOKEN" if os.getenv("AUREON_SERVICE") == "guard" else "ASSEMBLER_API_COLLECTOR_TOKEN"
        token = os.getenv(role_env) or os.environ["ASSEMBLER_API_SVC_TOKEN"]
        self.c = AssemblerClient(os.environ["ASSEMBLER_API_URL"], token,
                                 timeout=float(os.getenv("ASSEMBLER_API_TIMEOUT", "15")))
    @staticmethod
    def _j(v): return v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else v
    def upsert_bars(self, rows, tf, source="live"):
        for i in range(0, len(rows), 500):
            self.c.post("api/v1/ingest/bars", {"tf": tf, "source": source, "rows": [[self._j(x) for x in r] for r in rows[i:i+500]]})
    def heartbeat(self, service, state, note=""): self.c.post("api/v1/ingest/heartbeat", {"service": service, "state": state, "note": note})
    def get_state(self): return self.c.get("api/v1/ingest/state")["state"]
    def last_closed_m30(self): return self.c.get("api/v1/ingest/last_bar")
    def recent_signal(self, direction): return self.c.get("api/v1/ingest/recent_signal", params={"dir": direction})
    def open_trades(self): return self.c.get("api/v1/ingest/trades_open")
    def trade_by_ticket(self, ticket): return self.c.get("api/v1/ingest/trade_by_ticket", params={"ticket": ticket})
    def insert_trade(self, t): return self.c.post("api/v1/ingest/trade", {k: self._j(v) for k, v in t.items()})["id"]
    def update_trade(self, trade_id, expected_status=None, **f):
        body = {k: self._j(v) for k, v in f.items()}
        if expected_status: body["expected_status"] = expected_status
        try: self.c.patch(f"api/v1/ingest/trade/{trade_id}", body); return True
        except Exception as e:
            if getattr(e, "status", None) == 409: return False
            raise
    def event(self, trade_id, event, old_sl=None, new_sl=None, note=""):
        self.c.post("api/v1/ingest/trade_event", {"trade_id": trade_id, "event": event, "old_sl": old_sl, "new_sl": new_sl, "note": note})
    def daily_pnl(self): return self.c.get("api/v1/ingest/daily_pnl")["pnl"]
    def claim_job(self, worker): return self.c.post("api/v1/ingest/jobs/claim", {"worker": worker})
    def finish_job(self, job_id, ok, result="", worker=None, token=None): return self.c.post(f"api/v1/ingest/jobs/{job_id}/finish", {"ok": ok, "result": result, "worker": worker, "token": token}).get("ok", False)
    def renew_job(self, job_id, worker, token): return self.c.post(f"api/v1/ingest/jobs/{job_id}/renew", {"worker": worker, "token": token}).get("ok", False)
    def risk_get(self, day, acct): return self.c.get("api/v1/ingest/risk", params={"day": str(day), "account_id": acct}) or None
    def risk_upsert(self, r): self.c.post("api/v1/ingest/risk", {k: self._j(v) for k, v in r.items()})
    def risk_event(self, day, acct, event, detail, actor): self.c.post("api/v1/ingest/risk_event", {"broker_day": str(day), "account_id": acct, "event": event, "detail": detail, "actor": actor})
    def alert_enqueue(self, key, kind, payload): self.c.post("api/v1/ingest/alert", {"alert_key": key, "kind": kind, "payload": payload})
    def health_summary(self): return self.c.get("api/v1/ingest/health_summary")
    def ingest_deals(self, deals):
        for i in range(0, len(deals), 200): self.c.post("api/v1/ingest/mt5_deals", {"deals": [{k: self._j(v) for k, v in d.items()} for d in deals[i:i+200]]})
    def get_control(self, key, default=None):
        r = self.c.get("api/v1/ingest/control", params={"key": key}); return r["value"] if r and r.get("value") is not None else default
    def set_control(self, key, value, by="guard"): self.c.post("api/v1/ingest/control", {"key": key, "value": str(value), "by": by})

_store = None
def get_store():
    """Created on first use, so importing a module that needs the store never requires API/DB configuration at import time."""
    global _store
    if _store is None: _store = ApiStore() if config.STORE_MODE == "api" else DbStore()
    return _store

class _LazyStore:
    def __getattr__(self, name): return getattr(get_store(), name)
store = _LazyStore()
