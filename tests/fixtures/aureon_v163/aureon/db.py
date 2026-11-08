"""Direct Postgres access (STORE_MODE=db only). psycopg is imported lazily so api-mode hosts need not install it."""
import json, os
from . import config

SCHEMA = os.getenv("DB_SCHEMA", "aureon")
_pool = None

def pool():
    global _pool
    if _pool is None:
        if not config.DATABASE_URL: raise RuntimeError("DATABASE_URL not set (STORE_MODE=db required for direct DB access)")
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=int(os.getenv("DB_POOL_MAX", "8")),
                               kwargs={"row_factory": dict_row, "autocommit": True,
                                       "options": f"-c search_path={SCHEMA},public -c statement_timeout=10000"})
    return _pool

def conn(): return pool().connection()

def q(sql, params=None, one=False):
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params or ())
        if cur.description:
            return cur.fetchone() if one else cur.fetchall()

def upsert_bars(rows, tf, symbol=config.SYMBOL, source="live"):
    with conn() as c, c.cursor() as cur:
        cur.executemany("""insert into bars(symbol,tf,time_utc,open,high,low,close,tick_volume,spread,source)
            values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict(symbol,tf,time_utc) do update set open=excluded.open,high=excluded.high,low=excluded.low,
            close=excluded.close,tick_volume=excluded.tick_volume,spread=excluded.spread,source=excluded.source""",
            [(symbol, tf, *r, source) for r in rows])

def heartbeat(service, state, note=""):
    q("""insert into service_heartbeats(service,state,last_beat,note) values(%s,%s,now(),%s)
         on conflict(service) do update set state=excluded.state,last_beat=now(),note=excluded.note""", (service, state, note))

def get_state():
    r = q("select state from service_heartbeats where service='scheduler'", one=True)
    return r["state"] if r else "PAUSED"

def ensure_rules_version():
    q("insert into rules_versions(label,params,status) values(%s,%s,'live') on conflict(label) do nothing",
      (config.RULES_VERSION, json.dumps(config.RULES)))

# ---- jobs: Linux asks, Windows MT5 box executes (backfill / verify_week) ----
def request_job(kind, payload=None):
    r = q("insert into jobs(kind,payload) values(%s,%s) returning id", (kind, json.dumps(payload or {})), one=True); return r["id"]
def job_status(job_id): return q("select * from jobs where id=%s", (job_id,), one=True)
def claim_job(worker, lease_sec=600, max_attempts=3):
    """Atomic claim with a fresh lease token and generation. Expired leases are re-claimable; after max_attempts -> state 'dead'."""
    if not worker: raise ValueError("worker identity required")
    import uuid; token = uuid.uuid4().hex
    q("update jobs set state='dead', done_at=now(), ok=false, result=coalesce(result,'')||' [dead: attempts exhausted]' where state in ('pending','running') and attempts>=%s and (lease_until is null or lease_until<now())", (max_attempts,))
    return q("""update jobs set claimed_at=now(), claimed_by=%s, lease_token=%s, generation=generation+1, state='running',
                lease_until=now()+make_interval(secs => %s), attempts=attempts+1
                where id = (select id from jobs where state in ('pending','running') and done_at is null and attempts < %s
                            and (lease_until is null or lease_until < now()) order by requested_at limit 1 for update skip locked)
                returning *""", (worker, token, lease_sec, max_attempts), one=True)
def renew_job(job_id, worker, token, lease_sec=600):
    return bool(q("update jobs set lease_until=now()+make_interval(secs => %s) where id=%s and claimed_by=%s and lease_token=%s and done_at is null returning id", (lease_sec, job_id, worker, token), one=True))
def finish_job(job_id, ok, result="", worker=None, token=None):
    """Fenced: worker AND lease token must match the current lease; stale workers get False. worker=None is rejected."""
    if not worker or not token: raise ValueError("worker and lease token required")
    r = q("""update jobs set done_at=now(), ok=%s, result=%s, state=%s where id=%s and done_at is null and claimed_by=%s and lease_token=%s and lease_until>=now() returning id""",
          (ok, result[:2000], "done" if ok else "failed", job_id, worker, token), one=True)
    if not ok and r: q("update jobs set state='pending', done_at=null, lease_until=null where id=%s and attempts<3", (job_id,))   # retry later unless exhausted
    return bool(r)
def get_control(key, default=None):
    r = q("select value from control where key=%s", (key,), one=True); return r["value"] if r else default
def set_control(key, value, by="system"):
    q("insert into control(key,value,updated_by) values(%s,%s,%s) on conflict(key) do update set value=excluded.value,updated_at=now(),updated_by=excluded.updated_by", (key, value, by))

# ---- risk state ----
def risk_get(day, acct): return q("select * from risk_state where broker_day=%s and account_id=%s", (day, acct), one=True)
def risk_upsert(r):
    cols = ["broker_day","account_id","ref_equity","ref_balance","ref_source","realized","floating","equity","balance","drawdown","deposits","state","locked_at","lock_reason","cleared_at","cleared_by","clear_reason","equity_at","history_suspect"]
    vals = [r.get(c) for c in cols]
    q(f"""insert into risk_state({",".join(cols)}) values({",".join(["%s"]*len(cols))})
          on conflict(broker_day,account_id) do update set {",".join(f"{c}=excluded.{c}" for c in cols[2:])}, updated_at=now()""", vals)
def risk_event(day, acct, event, detail, actor): q("insert into risk_events(broker_day,account_id,event,detail,actor) values(%s,%s,%s,%s,%s)", (day, acct, event, json.dumps(detail, default=str), actor))
# ---- alerts ----
def alert_enqueue(key, kind, payload):
    return q("insert into alerts(alert_key,kind,payload) values(%s,%s,%s) on conflict(alert_key) do nothing returning id", (key, kind, payload), one=True)
def alert_claim():
    return q("""update alerts set state='SENDING', attempts=attempts+1 where id=(select id from alerts where state in ('PENDING','SENDING') and next_attempt_at<=now()
                and (state='PENDING' or attempts<6) order by created_at limit 1 for update skip locked) returning *""", one=True)
def alert_done(aid): q("update alerts set state='DELIVERED', delivered_at=now() where id=%s", (aid,))
def alert_retry(aid, err, delay_s): q("update alerts set state='PENDING', last_error=%s, next_attempt_at=now()+make_interval(secs => %s) where id=%s", (err[:300], delay_s, aid))
def alert_failed(aid, err): q("update alerts set state='FAILED', last_error=%s where id=%s", (err[:300], aid))
def api_audit(role, method, path, status, client, detail=""): q("insert into api_audit(role,method,path,status,client,detail) values(%s,%s,%s,%s,%s,%s)", (role, method, path, status, client, detail[:300]))

def ingest_deals(deals):
    cols = ["account_id","deal_ticket","order_ticket","position_id","symbol","type","entry","time_utc","volume","price","profit","commission","swap","fee","comment","magic"]
    with conn() as c, c.cursor() as cur:
        cur.executemany(f"insert into mt5_deals({','.join(cols)}) values({','.join(['%s']*len(cols))}) on conflict(account_id,deal_ticket) do nothing", [tuple(d.get(k) for k in cols) for d in deals])
