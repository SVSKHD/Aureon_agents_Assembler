"""Storage for the Aureon V1.6.3 contract on the Assembler's PostgreSQL (schema `aureon`).

An async port of V1.6.3 aureon/db.py and the DbStore half of aureon/store.py: the same
statements and the same semantics (atomic job claim with a lease token, fenced finish/renew,
idempotent upserts, `on conflict do nothing` for replays), run through the Assembler's
SQLAlchemy session instead of a psycopg pool. Callers own the transaction (commit).

Timestamps: every value written to a timestamptz column is timezone-aware UTC; a naive value
means UTC (V1.6.3 aureon/timeutil.py).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.aureon import config

UTC = dt.UTC
Row = dict[str, Any]


def utc(x: Any) -> Any:
    """datetime / ISO string -> aware UTC datetime (naive means UTC); anything else unchanged."""
    if isinstance(x, str):
        x = dt.datetime.fromisoformat(x)
    if isinstance(x, dt.datetime):
        return x.replace(tzinfo=UTC) if x.tzinfo is None else x.astimezone(UTC)
    return x


async def q(
    db: AsyncSession, sql: str, params: Mapping[str, Any] | None = None, one: bool = False
) -> Any:
    """Run one statement. Rows come back as dicts; `one` returns the first row or None."""
    res = await db.execute(text(sql), dict(params or {}))
    if not getattr(res, "returns_rows", False):
        return None
    if one:
        r = res.mappings().first()
        return dict(r) if r is not None else None
    return [dict(r) for r in res.mappings().all()]


# ---------------- bars / heartbeats / state ----------------
async def upsert_bars(
    db: AsyncSession,
    rows: Sequence[Sequence[Any]],
    tf: str,
    symbol: str = config.SYMBOL,
    source: str = "live",
) -> None:
    if not rows:
        return
    await db.execute(
        text(
            """insert into aureon.bars(symbol,tf,time_utc,open,high,low,close,tick_volume,spread,source)
            values(:symbol,:tf,:t,:o,:h,:l,:c,:v,:sp,:source)
            on conflict(symbol,tf,time_utc) do update set open=excluded.open,high=excluded.high,
            low=excluded.low,close=excluded.close,tick_volume=excluded.tick_volume,
            spread=excluded.spread,source=excluded.source"""
        ),
        [
            dict(
                symbol=symbol,
                tf=tf,
                t=utc(r[0]),
                o=r[1],
                h=r[2],
                l=r[3],
                c=r[4],
                v=r[5],
                sp=r[6],
                source=source,
            )
            for r in rows
        ],
    )


async def heartbeat(db: AsyncSession, service: str, state: str, note: str = "") -> None:
    await q(
        db,
        """insert into aureon.service_heartbeats(service,state,last_beat,note)
           values(:s,:st,now(),:n) on conflict(service) do update
           set state=excluded.state,last_beat=now(),note=excluded.note""",
        {"s": service, "st": state, "n": note},
    )


async def get_state(db: AsyncSession) -> str:
    r = await q(
        db, "select state from aureon.service_heartbeats where service='scheduler'", one=True
    )
    return str(r["state"]) if r and r["state"] else "PAUSED"


async def ensure_rules_version(db: AsyncSession) -> None:
    await q(
        db,
        """insert into aureon.rules_versions(label,params,status)
           values(:l,cast(:p as jsonb),'live') on conflict(label) do nothing""",
        {"l": config.RULES_VERSION, "p": json.dumps(config.RULES)},
    )


# ---------------- jobs: the Assembler asks, the Windows MT5 box executes ----------------
JOB_KINDS = ("backfill", "verify_week", "backfill_range", "mt5_history")


async def request_job(db: AsyncSession, kind: str, payload: Mapping[str, Any] | None = None) -> int:
    if kind not in JOB_KINDS:
        raise ValueError(f"unknown job kind {kind}")
    r = await q(
        db,
        "insert into aureon.jobs(kind,payload) values(:k,cast(:p as jsonb)) returning id",
        {"k": kind, "p": json.dumps(dict(payload or {}))},
        one=True,
    )
    return int(r["id"])


async def job_status(db: AsyncSession, job_id: int) -> Row | None:
    r: Row | None = await q(db, "select * from aureon.jobs where id=:i", {"i": job_id}, one=True)
    return r


async def claim_job(
    db: AsyncSession, worker: str, lease_sec: int = 600, max_attempts: int = 3
) -> Row | None:
    """Atomic claim with a fresh lease token and generation. Expired leases are re-claimable;
    after max_attempts -> state 'dead'. FOR UPDATE SKIP LOCKED: two workers never get one job."""
    if not worker:
        raise ValueError("worker identity required")
    token = uuid.uuid4().hex
    await q(
        db,
        """update aureon.jobs set state='dead', done_at=now(), ok=false,
           result=coalesce(result,'')||' [dead: attempts exhausted]'
           where state in ('pending','running') and done_at is null and attempts>=:m
           and (lease_until is null or lease_until<now())""",
        {"m": max_attempts},
    )
    r: Row | None = await q(
        db,
        """update aureon.jobs set claimed_at=now(), claimed_by=:w, lease_token=:t,
           generation=generation+1, state='running',
           lease_until=now()+make_interval(secs => cast(:s as double precision)),
           attempts=attempts+1
           where id = (select id from aureon.jobs where state in ('pending','running')
                       and done_at is null and attempts < :m
                       and (lease_until is null or lease_until < now())
                       order by requested_at, id limit 1 for update skip locked)
           returning *""",
        {"w": worker, "t": token, "s": lease_sec, "m": max_attempts},
        one=True,
    )
    return r


async def renew_job(
    db: AsyncSession, job_id: int, worker: str, token: str, lease_sec: int = 600
) -> bool:
    r = await q(
        db,
        """update aureon.jobs set lease_until=now()+make_interval(secs => cast(:s as double precision))
           where id=:i and claimed_by=:w and lease_token=:t and done_at is null
           returning id""",
        {"s": lease_sec, "i": job_id, "w": worker, "t": token},
        one=True,
    )
    return bool(r)


async def finish_job(
    db: AsyncSession,
    job_id: int,
    ok: bool,
    result: str = "",
    worker: str | None = None,
    token: str | None = None,
) -> bool:
    """Fenced: worker AND lease token must match the current, unexpired lease; a stale worker
    gets False. A failure goes back to pending (retry) until attempts are exhausted."""
    if not worker or not token:
        raise ValueError("worker and lease token required")
    r = await q(
        db,
        """update aureon.jobs set done_at=now(), ok=:ok, result=:r, state=:st
           where id=:i and done_at is null and claimed_by=:w and lease_token=:t
           and lease_until>=now() returning id""",
        {"ok": ok, "r": (result or "")[:2000], "st": "done" if ok else "failed", "i": job_id,
         "w": worker, "t": token},
        one=True,
    )  # fmt: skip
    if not ok and r:
        await q(
            db,
            """update aureon.jobs set state='pending', done_at=null, lease_until=null
               where id=:i and attempts<3""",
            {"i": job_id},
        )
    return bool(r)


async def open_job(db: AsyncSession, kind: str, payload: Mapping[str, Any]) -> Row | None:
    """An unfinished job of this kind with exactly this payload (so a request is not doubled)."""
    r: Row | None = await q(
        db,
        """select * from aureon.jobs where kind=:k and payload=cast(:p as jsonb)
           and done_at is null and state in ('pending','running') order by id desc limit 1""",
        {"k": kind, "p": json.dumps(dict(payload))},
        one=True,
    )
    return r


# ---------------- control (operator intent, guard requests) ----------------
async def get_control(db: AsyncSession, key: str, default: str | None = None) -> str | None:
    r = await q(db, "select value from aureon.control where key=:k", {"k": key}, one=True)
    return str(r["value"]) if r else default


async def set_control(db: AsyncSession, key: str, value: str, by: str = "system") -> None:
    await q(
        db,
        """insert into aureon.control(key,value,updated_by) values(:k,:v,:b)
           on conflict(key) do update set value=excluded.value,updated_at=now(),
           updated_by=excluded.updated_by""",
        {"k": key, "v": value, "b": by},
    )


# ---------------- risk state ----------------
RISK_COLS = (
    "broker_day", "account_id", "ref_equity", "ref_balance", "ref_source", "realized",
    "floating", "equity", "balance", "drawdown", "deposits", "state", "locked_at",
    "lock_reason", "cleared_at", "cleared_by", "clear_reason", "equity_at", "history_suspect",
)  # fmt: skip
RISK_TIMES = ("locked_at", "cleared_at", "equity_at")


async def risk_get(db: AsyncSession, day: dt.date, acct: str) -> Row | None:
    r: Row | None = await q(
        db,
        "select * from aureon.risk_state where broker_day=:d and account_id=:a",
        {"d": day, "a": acct},
        one=True,
    )
    return r


async def risk_upsert(db: AsyncSession, r: Mapping[str, Any]) -> None:
    vals = {c: r.get(c) for c in RISK_COLS}
    for c in RISK_TIMES:
        if vals[c] is not None:
            vals[c] = utc(vals[c])
    if isinstance(vals["broker_day"], str):
        vals["broker_day"] = dt.date.fromisoformat(vals["broker_day"])
    cols = ",".join(RISK_COLS)
    binds = ",".join(f":{c}" for c in RISK_COLS)
    sets = ",".join(f"{c}=excluded.{c}" for c in RISK_COLS[2:])
    await q(
        db,
        f"""insert into aureon.risk_state({cols}) values({binds})
            on conflict(broker_day,account_id) do update set {sets}, updated_at=now()""",
        vals,
    )


async def risk_event(
    db: AsyncSession, day: dt.date, acct: str, event: str, detail: Any, actor: str
) -> None:
    await q(
        db,
        """insert into aureon.risk_events(broker_day,account_id,event,detail,actor)
           values(:d,:a,:e,cast(:j as jsonb),:act)""",
        {"d": day, "a": acct, "e": event, "j": json.dumps(detail, default=str), "act": actor},
    )


# ---------------- alerts (durable queue, idempotent by key) ----------------
async def alert_enqueue(db: AsyncSession, key: str, kind: str, payload: str) -> Row | None:
    r: Row | None = await q(
        db,
        """insert into aureon.alerts(alert_key,kind,payload) values(:k,:kind,:p)
           on conflict(alert_key) do nothing returning id""",
        {"k": key, "kind": kind, "p": payload},
        one=True,
    )
    return r


async def alert_claim(db: AsyncSession) -> Row | None:
    r: Row | None = await q(
        db,
        """update aureon.alerts set state='SENDING', attempts=attempts+1 where id=(
             select id from aureon.alerts where state in ('PENDING','SENDING')
             and next_attempt_at<=now() and (state='PENDING' or attempts<6)
             order by created_at limit 1 for update skip locked) returning *""",
        one=True,
    )
    return r


async def alert_done(db: AsyncSession, aid: int) -> None:
    await q(
        db,
        "update aureon.alerts set state='DELIVERED', delivered_at=now() where id=:i",
        {"i": aid},
    )


async def alert_retry(db: AsyncSession, aid: int, err: str, delay_s: float) -> None:
    await q(
        db,
        """update aureon.alerts set state='PENDING', last_error=:e,
           next_attempt_at=now()+make_interval(secs => cast(:s as double precision)) where id=:i""",
        {"e": err[:300], "s": delay_s, "i": aid},
    )


async def alert_failed(db: AsyncSession, aid: int, err: str) -> None:
    await q(
        db,
        "update aureon.alerts set state='FAILED', last_error=:e where id=:i",
        {"e": err[:300], "i": aid},
    )


async def api_audit(
    db: AsyncSession, role: str | None, method: str, path: str, status: int, client: str,
    detail: str = "",
) -> None:  # fmt: skip
    await q(
        db,
        """insert into aureon.api_audit(role,method,path,status,client,detail)
           values(:r,:m,:p,:s,:c,:d)""",
        {"r": role, "m": method, "p": path[:300], "s": status, "c": client, "d": detail[:300]},
    )


# ---------------- MT5 broker deal history ----------------
DEAL_COLS = (
    "server", "account_id", "deal_ticket", "order_ticket", "position_id", "symbol", "type",
    "entry", "time_utc", "volume", "price", "profit", "commission", "swap", "fee", "comment",
    "magic",
)  # fmt: skip


async def ingest_deals(db: AsyncSession, deals: Iterable[Mapping[str, Any]]) -> int:
    """Idempotent on (server, account, deal ticket): a replayed upload inserts nothing new.
    Returns how many rows were new."""
    rows = []
    for d in deals:
        row = {k: d.get(k) for k in DEAL_COLS}
        row["server"] = str(row["server"] or "")
        row["account_id"] = str(row["account_id"])
        row["time_utc"] = utc(row["time_utc"])
        rows.append(row)
    if not rows:
        return 0
    cols = ",".join(DEAL_COLS)
    new = 0
    for row in rows:
        r = await q(
            db,
            f"""insert into aureon.mt5_deals({cols}) values({",".join(":" + c for c in DEAL_COLS)})
                on conflict do nothing returning deal_ticket""",
            row,
            one=True,
        )
        new += 1 if r else 0
    return new


# ---------------- the guard's trade store (V1.6.3 DbStore) ----------------
async def last_closed_m30(db: AsyncSession) -> Row | None:
    r: Row | None = await q(
        db,
        """select b.time_utc,b.open,b.high,b.low,b.close,i.vwap_side,i.bar_of_day,i.atr14
           from aureon.bars b join aureon.indicators i
           on i.symbol=b.symbol and i.tf=b.tf and i.time_utc=b.time_utc and i.rules_version=:rv
           where b.symbol=:sym and b.tf='M30' order by b.time_utc desc limit 1""",
        {"rv": config.RULES_VERSION, "sym": config.SYMBOL},
        one=True,
    )
    return r


async def recent_signal(db: AsyncSession, direction: int) -> Row | None:
    r: Row | None = await q(
        db,
        """select id,suggested_sl,dir from aureon.signals where source='live'
           and (features->>'tradeable')='true' and dir=:d
           and time_utc > now()-interval '3 hours' order by time_utc desc limit 1""",
        {"d": direction},
        one=True,
    )
    return r


async def open_trades(db: AsyncSession) -> list[Row]:
    rows: list[Row] = await q(
        db,
        "select * from aureon.trades where status in ('open','closing','unreconciled') order by id",
    )
    return rows


async def trade_by_ticket(db: AsyncSession, ticket: int) -> Row | None:
    r: Row | None = await q(
        db, "select id,status from aureon.trades where mt5_ticket=:t", {"t": ticket}, one=True
    )
    return r


async def insert_trade(db: AsyncSession, t: Mapping[str, Any]) -> int:
    r = await q(
        db,
        """insert into aureon.trades(signal_id,mt5_ticket,dir,lots,entry_time,entry_price,sl,tp,status)
           values(:signal_id,:mt5_ticket,:dir,:lots,:entry_time,:entry_price,:sl,:tp,'open')
           on conflict(mt5_ticket) do nothing returning id""",
        {**{k: t.get(k) for k in ("signal_id", "mt5_ticket", "dir", "lots", "entry_price", "sl",
                                  "tp")},
         "entry_time": utc(t["entry_time"])},
        one=True,
    )  # fmt: skip
    if r:
        return int(r["id"])
    ex = await trade_by_ticket(db, int(t["mt5_ticket"]))  # duplicate ticket -> existing
    assert ex is not None
    return int(ex["id"])


TRADE_FIELDS = frozenset(
    {
        "status",
        "exit_time",
        "close_sent_at",
        "close_request_id",
        "exit_price",
        "pnl_usd",
        "exit_reason",
        "sl",
        "tp",
    }
)


async def update_trade(
    db: AsyncSession, trade_id: int, expected_status: str | None = None, **f: Any
) -> bool:
    """Atomic: with expected_status the row must still be in that status, else False (lost race)."""
    bad = set(f) - TRADE_FIELDS
    if bad:
        raise ValueError(f"not updatable: {sorted(bad)}")
    vals = {k: (utc(v) if k in ("exit_time", "close_sent_at") and v is not None else v)
            for k, v in f.items()}  # fmt: skip
    sets = ", ".join(f"{k}=:{k}" for k in vals)
    if expected_status is None:
        # Column names come from TRADE_FIELDS only (checked above); values are bound.
        sql = f"update aureon.trades set {sets} where id=:_id"  # noqa: S608
        await q(db, sql, {**vals, "_id": trade_id})
        return True
    r = await q(
        db,
        f"update aureon.trades set {sets} where id=:_id and status=:_exp returning id",  # noqa: S608
        {**vals, "_id": trade_id, "_exp": expected_status},
        one=True,
    )
    return bool(r)


async def event(
    db: AsyncSession, trade_id: int, ev: str, old_sl: float | None = None,
    new_sl: float | None = None, note: str = "",
) -> None:  # fmt: skip
    await q(
        db,
        """insert into aureon.trade_events(trade_id,event,old_sl,new_sl,note)
           values(:t,:e,:o,:n,:note)""",
        {"t": trade_id, "e": ev, "o": old_sl, "n": new_sl, "note": note},
    )


async def daily_pnl(db: AsyncSession) -> float:
    """Closed trades' P&L whose exit falls on today's UTC date (V1.6.3 ran its DB in UTC)."""
    r = await q(
        db,
        """select coalesce(sum(pnl_usd),0) pnl from aureon.trades where status='closed'
           and (exit_time at time zone 'UTC')::date = (now() at time zone 'UTC')::date""",
        one=True,
    )
    return float(r["pnl"]) if r else 0.0
