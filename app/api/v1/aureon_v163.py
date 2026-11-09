"""Aureon V1.6.3 database API (docs/34-aureon-v163.md).

The contract of the V1.6.3 package's assembler/app.py, at the same paths (relative to the
client's ASSEMBLER_API_URL), with the same request bodies, query parameters, validation,
status codes and response shapes, on the Assembler's PostgreSQL (schema `aureon`).

    collector  POST /api/v1/ingest/bars | /mt5_deals | /jobs/claim | /jobs/{jid}/renew | /finish
    guard      GET  /api/v1/ingest/recent_signal | trades_open | trade_by_ticket | daily_pnl | risk
               POST /api/v1/ingest/trade | trade_event | risk | risk_event;  PATCH /trade/{tid}
    service    POST /api/v1/ingest/heartbeat | alert | control;  GET state | last_bar | control
               | health_summary (V1.6.4 watchdog)
    read       GET  /api/v1/status | signals | signals/{sid} | trades | chart | review |
               review/latest | risk | review/reports[/{rid}[/files/{name}]] | aureon/readyz
    write      POST /api/v1/decisions
    admin      POST /api/v1/admin/risk/clear | /api/v1/admin/review/run
    none       GET  /api/v1/aureon/healthz

Four compatibility fixes over V1.6.3's server, each for a request its own client sends that
its own server answered 422 (docs/34 lists them): the guard's trade event `close_sent`; the
guard's `status: open, exit_reason: null` when it re-adopts an unreconciled position; the
collector's heartbeat echoing the shared state DEGRADED or FAILED; and the collector's
`backfill_range` upload with bar source `backfill`.

Boundary: nothing here sends, modifies or closes an order, or talks to MT5. The Windows box
owns MT5; this API only stores what it reports and hands it work (jobs) to pick up.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ui import ui_module
from app.aureon import config, db
from app.aureon import weekend_review as wr
from app.aureon.auth import ADMIN, COLLECTOR, GUARD, READ, SERVICE, WRITE, AuditedRoute, require
from app.aureon.risk import admin_clear
from app.auth.deps import DbSession
from app.services import audit

# Hidden from the UI manifest by the safety floor (machine API, role bearer tokens).
UI_MODULE = ui_module("aureon", label="Aureon V1.6.3", group="markets", icon="activity", order=26)
router = APIRouter(prefix="/v1", tags=["aureon"], route_class=AuditedRoute)

Role = Annotated[str, Depends(require(*READ))]
WriteRole = Annotated[str, Depends(require(*WRITE))]
CollectorRole = Annotated[str, Depends(require(*COLLECTOR))]
GuardRole = Annotated[str, Depends(require(*GUARD))]
ServiceRole = Annotated[str, Depends(require(*SERVICE))]
AdminRole = Annotated[str, Depends(require(*ADMIN))]
LIMIT = Query(100, ge=1, le=500)
OFFSET = Query(0, ge=0)


def parse_dt(s: Any, name: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(s)
    except (ValueError, TypeError):
        raise HTTPException(422, f"{name}: invalid ISO datetime") from None


def week_bounds(week: str | None) -> tuple[dt.date, dt.date]:
    today = dt.datetime.now(dt.UTC).date()
    try:
        ws = dt.date.fromisoformat(week) if week else today - dt.timedelta(days=today.weekday())
    except ValueError:
        raise HTTPException(422, "week: use YYYY-MM-DD") from None
    return ws, ws + dt.timedelta(days=7)


def parse_day(s: str, name: str = "day") -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except (ValueError, TypeError):
        raise HTTPException(422, f"{name}: use YYYY-MM-DD") from None


def _aware(t: dt.datetime) -> dt.datetime:
    return t.replace(tzinfo=dt.UTC) if t.tzinfo is None else t.astimezone(dt.UTC)


# ---------------- health ----------------
@router.get("/aureon/healthz")
async def healthz(s: DbSession) -> dict[str, Any]:
    try:
        await db.q(s, "select 1 from aureon.control limit 1")
    except Exception:
        raise HTTPException(503, "db unreachable") from None
    return {"ok": True}


@router.get("/aureon/readyz")
async def readyz(s: DbSession, _r: Role) -> dict[str, Any]:
    """Workers alive + data fresh (last M30 bar < 2 h old while LIVE) + guard notes (V1.6.3)."""
    try:
        hb = {
            r["service"]: r
            for r in await db.q(
                s, "select service,state,last_beat,note from aureon.service_heartbeats"
            )
        }
        last = (
            await db.q(
                s,
                "select max(time_utc) t from aureon.bars where symbol=:s and tf='M30'",
                {"s": config.SYMBOL},
                one=True,
            )
        )["t"]
    except Exception:
        raise HTTPException(503, "db unreachable") from None
    now = dt.datetime.now(dt.UTC)
    problems = []
    for svc in ("scheduler", "detector", "alerter", "collector", "guard"):
        r = hb.get(svc)
        if (
            not r
            or not r["last_beat"]
            or (now - r["last_beat"]).total_seconds()
            > (config.HEARTBEAT_SEC * config.HEARTBEAT_MISS_LIMIT)
        ):
            problems.append(f"{svc} stale")
    state = (hb.get("scheduler") or {}).get("state") or "PAUSED"
    if state == "LIVE" and (last is None or (now - last).total_seconds() > 7200):
        problems.append("bars stale")
    gnote = (hb.get("guard") or {}).get("note") or ""
    for needle, msg in (
        ("UNPROTECTED", "guard reports unprotected position(s)"),
        ("RISK=UNKNOWN", "risk state UNKNOWN"),
        ("CLOSING", "close outcome pending"),
        ("UNRECONCILED", "guard reports unreconciled trade(s)"),
        ("RISK=LOCKED", "daily loss limit LOCKED"),
    ):
        if needle in gnote:
            problems.append(msg)
    if problems:
        raise HTTPException(503, {"state": state, "problems": problems})
    return {"ok": True, "state": state, "last_bar": last}


# ---------------- read ----------------
@router.get("/status")
async def status(s: DbSession, _r: Role) -> dict[str, Any]:
    return {
        "state": await db.get_state(s),
        "rules_version": config.RULES_VERSION,
        "heartbeats": await db.q(
            s, "select service,state,last_beat,note from aureon.service_heartbeats order by service"
        ),
        "open_trades": await db.q(
            s,
            """select id,signal_id,dir,lots,entry_time,entry_price,sl,status from aureon.trades
               where status in ('open','closing','unreconciled') order by id""",
        ),
        "risk": await db.q(
            s,
            """select state,drawdown,ref_source from aureon.risk_state
               order by broker_day desc, updated_at desc limit 1""",
            one=True,
        ),
        "last_bar": await db.q(
            s,
            """select time_utc,close from aureon.bars where symbol=:s and tf='M30'
               order by time_utc desc limit 1""",
            {"s": config.SYMBOL},
            one=True,
        ),
    }


@router.get("/signals")
async def signals(
    s: DbSession,
    _r: Role,
    week: str | None = None,
    tradeable: bool = True,
    limit: int = LIMIT,
    offset: int = OFFSET,
) -> list[dict[str, Any]]:
    ws, we = week_bounds(week)
    rows: list[dict[str, Any]] = await db.q(
        s,
        """select s.id,s.time_utc,s.kind,s.dir,s.price,s.atr,s.suggested_sl,s.features,s.sent_at,
           (select tag from aureon.decisions d where d.signal_id=s.id
            order by tagged_at desc limit 1) tag,
           h.pl_rule,h.exit_how,h.secured,h.mfe,h.mae, t.pnl_usd, t.status trade_status
           from aureon.signals s left join aureon.hypo_outcomes h
             on h.signal_id=s.id and h.rules_version=s.rules_version
           left join aureon.trades t on t.signal_id=s.id
           where s.source='live' and s.time_utc>=:ws and s.time_utc<:we
           and (cast(:tr as boolean)=false or (s.features->>'tradeable')='true')
           order by s.time_utc, s.id limit :lim offset :off""",
        {"ws": ws, "we": we, "tr": tradeable, "lim": limit, "off": offset},
    )
    return rows


@router.get("/signals/{sid}")
async def signal(sid: int, s: DbSession, _r: Role) -> dict[str, Any]:
    row = await db.q(s, "select * from aureon.signals where id=:i", {"i": sid}, one=True)
    if not row:
        raise HTTPException(404)
    row["decisions"] = await db.q(
        s,
        "select tag,via,note,tagged_at from aureon.decisions where signal_id=:i order by tagged_at",
        {"i": sid},
    )
    row["outcome"] = await db.q(
        s, "select * from aureon.hypo_outcomes where signal_id=:i", {"i": sid}
    )
    row["trade"] = await db.q(
        s,
        """select id,dir,lots,entry_time,entry_price,sl,tp,status,exit_time,exit_price,pnl_usd,
           exit_reason from aureon.trades where signal_id=:i""",
        {"i": sid},
        one=True,
    )
    if row["trade"]:
        row["trade"]["events"] = await db.q(
            s,
            """select time_utc,event,old_sl,new_sl,note from aureon.trade_events
               where trade_id=:t order by time_utc""",
            {"t": row["trade"]["id"]},
        )
    out: dict[str, Any] = row
    return out


class Decision(BaseModel):
    signal_id: int
    tag: str = Field(pattern="^(take|skip|late|news|doubt)$")
    note: str = Field(default="", max_length=200)


@router.post("/decisions", status_code=201)
async def decide(d: Decision, s: DbSession, _r: WriteRole) -> dict[str, Any]:
    sig = await db.q(
        s,
        "select time_utc from aureon.signals where id=:i and source='live'",
        {"i": d.signal_id},
        one=True,
    )
    if not sig:
        raise HTTPException(404, "signal not found")
    if sig["time_utc"] < dt.datetime.now(dt.UTC) - dt.timedelta(hours=2):
        raise HTTPException(409, "signal older than 2 hours — decisions closed")
    last = await db.q(
        s,
        "select tag from aureon.decisions where signal_id=:i order by tagged_at desc limit 1",
        {"i": d.signal_id},
        one=True,
    )
    if last and last["tag"] == d.tag:
        return {"ok": True, "unchanged": True}  # idempotent; a different tag supersedes
    await db.q(
        s,
        "insert into aureon.decisions(signal_id,tag,via,note) values(:i,:t,'spasta',:n)",
        {"i": d.signal_id, "t": d.tag, "n": d.note},
    )
    await s.commit()
    return {"ok": True}


@router.get("/trades")
async def trades(
    s: DbSession, _r: Role, week: str | None = None, limit: int = LIMIT, offset: int = OFFSET
) -> list[dict[str, Any]]:
    ws, we = week_bounds(week)
    rows: list[dict[str, Any]] = await db.q(
        s,
        """select t.id,t.signal_id,t.dir,t.lots,t.entry_time,t.entry_price,t.sl,t.tp,t.status,
           t.exit_time,t.exit_price,t.pnl_usd,t.exit_reason, s.time_utc signal_time, s.kind
           from aureon.trades t left join aureon.signals s on s.id=t.signal_id
           where t.entry_time>=:ws and t.entry_time<:we order by t.entry_time, t.id
           limit :lim offset :off""",
        {"ws": ws, "we": we, "lim": limit, "off": offset},
    )
    return rows


@router.get("/review/latest")
async def review_latest(s: DbSession, _r: Role) -> dict[str, Any]:
    r = await db.q(
        s, "select * from aureon.weekly_review order by week_start desc limit 1", one=True
    )
    return r or {}


@router.get("/review")
async def review_all(
    s: DbSession, _r: Role, limit: int = Query(12, ge=1, le=104)
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = await db.q(
        s, "select * from aureon.weekly_review order by week_start desc limit :l", {"l": limit}
    )
    return rows


@router.get("/chart")
async def chart(
    s: DbSession,
    _r: Role,
    tf: str = "M30",
    frm: str = Query(alias="from"),
    to: str = Query(...),
) -> dict[str, Any]:
    if tf not in ("M30", "H1"):
        raise HTTPException(400, "tf must be M30 or H1")
    f, t = parse_dt(frm, "from"), parse_dt(to, "to")
    if t <= f:
        raise HTTPException(422, "to must be after from")
    if (t - f).days > 60:
        raise HTTPException(400, "max 60 days per request")
    maxrows = 3000
    bars = await db.q(
        s,
        """select b.time_utc,b.open,b.high,b.low,b.close,b.tick_volume,i.ema8,i.ema32,i.ema200,
           i.atr14,i.vwap,i.vwap_side,i.vwap_flips8
           from aureon.bars b left join aureon.indicators i on i.symbol=b.symbol and i.tf=b.tf
             and i.time_utc=b.time_utc and i.rules_version=:rv
           where b.symbol=:sym and b.tf=:tf and b.time_utc>=:f and b.time_utc<=:t
           order by b.time_utc limit :m""",
        {"rv": config.RULES_VERSION, "sym": config.SYMBOL, "tf": tf, "f": _aware(f),
         "t": _aware(t), "m": maxrows},
    )  # fmt: skip
    sigs = await db.q(
        s,
        """select id,time_utc,kind,dir,price,suggested_sl,features from aureon.signals
           where source='live' and tf=:tf and time_utc>=:f and time_utc<=:t order by time_utc""",
        {"tf": tf, "f": _aware(f), "t": _aware(t)},
    )
    return {"tf": tf, "bars": bars, "signals": sigs, "truncated": len(bars) >= maxrows}


@router.get("/risk")
async def risk_read(s: DbSession, _r: Role) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = await db.q(
        s,
        """select broker_day,account_id,ref_source,state,drawdown,equity,updated_at
           from aureon.risk_state order by broker_day desc limit 14""",
    )
    return rows


# ---------------- ingest: collector ----------------
TF_MIN = {"M30": 30, "H1": 60}


class Bars(BaseModel):
    tf: str = Field(pattern="^(M30|H1)$")
    source: str = Field(default="live", pattern="^(live|csv|verify|backfill)$")
    rows: list[list[Any]] = Field(max_length=500)


def _validate_bar(r: list[Any], tf: str) -> tuple[Any, ...]:
    if len(r) != 7:
        raise HTTPException(422, "row must be [time_utc,o,h,l,c,tick_volume,spread]")
    t = parse_dt(r[0], "time_utc")
    if t.tzinfo:
        t = t.astimezone(dt.UTC).replace(tzinfo=None)
    if t > dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(minutes=1):
        raise HTTPException(422, f"bar in the future: {t}")
    if (t.minute % TF_MIN[tf]) != 0 or t.second or t.microsecond:
        raise HTTPException(422, f"time {t} not aligned to {tf}")
    try:
        o, h, lo, c = (float(x) for x in r[1:5])
        v, sp = int(r[5]), int(r[6])
    except (TypeError, ValueError):
        raise HTTPException(422, "numeric fields invalid") from None
    if not (config.PRICE_MIN < lo <= min(o, c) <= max(o, c) <= h < config.PRICE_MAX):
        raise HTTPException(422, f"OHLC relation/bounds invalid at {t}")
    if v < 0 or sp < 0 or sp > 100000:
        raise HTTPException(422, "volume/spread out of range")
    return (t, o, h, lo, c, v, sp)


@router.post("/ingest/bars", status_code=201)
async def ingest_bars(b: Bars, s: DbSession, _r: CollectorRole) -> dict[str, Any]:
    rows = [_validate_bar(r, b.tf) for r in b.rows]
    await db.upsert_bars(s, rows, b.tf, source=b.source)
    await s.commit()
    return {"n": len(rows)}


class Claim(BaseModel):
    worker: str = Field(min_length=1, max_length=100)


@router.post("/ingest/jobs/claim")
async def jobs_claim(c: Claim, s: DbSession, _r: CollectorRole) -> dict[str, Any] | None:
    job = await db.claim_job(s, c.worker)
    await s.commit()
    return job


class JobDone(BaseModel):
    ok: bool
    result: str = Field(default="", max_length=2000)
    worker: str = Field(min_length=1, max_length=100)
    token: str = Field(min_length=8, max_length=64)


@router.post("/ingest/jobs/{jid}/finish")
async def jobs_finish(jid: int, j: JobDone, s: DbSession, _r: CollectorRole) -> dict[str, Any]:
    accepted = await db.finish_job(s, jid, j.ok, j.result, j.worker, j.token)
    await s.commit()
    return {"ok": accepted, "ignored": not accepted}  # wrong worker/token/expired -> ignored


class JobRenew(BaseModel):
    worker: str = Field(min_length=1, max_length=100)
    token: str = Field(min_length=8, max_length=64)


@router.post("/ingest/jobs/{jid}/renew")
async def jobs_renew(jid: int, j: JobRenew, s: DbSession, _r: CollectorRole) -> dict[str, Any]:
    ok = await db.renew_job(s, jid, j.worker, j.token)
    await s.commit()
    return {"ok": ok}


class DealsIn(BaseModel):
    deals: list[dict[str, Any]] = Field(max_length=200)


@router.post("/ingest/mt5_deals", status_code=201)
async def ingest_mt5_deals(b: DealsIn, s: DbSession, _r: CollectorRole) -> dict[str, Any]:
    rows = []
    for d in b.deals:
        try:
            if not str(d["account_id"]).strip():
                raise ValueError("account_id empty")
            t = parse_dt(d["time_utc"], "time_utc")
            rows.append({**d, "time_utc": t, "deal_ticket": int(d["deal_ticket"]),
                         "volume": float(d["volume"]), "price": float(d["price"])})  # fmt: skip
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, f"bad deal row: {e}") from None
    new = await db.ingest_deals(s, rows)
    await s.commit()
    return {"n": len(rows), "inserted": new}


# ---------------- ingest: service (collector, guard, service) ----------------
class HB(BaseModel):
    service: str = Field(pattern="^(collector|guard)$")
    state: str = Field(pattern="^(LIVE|PAUSED|RESUMING|DEGRADED|FAILED)$")
    note: str = Field(default="", max_length=200)


@router.post("/ingest/heartbeat", status_code=201)
async def ingest_hb(h: HB, s: DbSession, _r: ServiceRole) -> dict[str, Any]:
    await db.heartbeat(s, h.service, h.state, h.note)
    await s.commit()
    return {"ok": True}


@router.get("/ingest/state")
async def ingest_state(s: DbSession, _r: ServiceRole) -> dict[str, Any]:
    return {"state": await db.get_state(s)}


@router.get("/ingest/health_summary")
async def ingest_health_summary(
    s: DbSession, _r: ServiceRole, detail: bool = False
) -> dict[str, Any]:
    """For the V1.6.4 Windows watchdog (aureon/watchdog.py, via ApiStore.health_summary()).

    The response is V1.6.4's own contract (aureon_v1/assembler/app.py), field for field:
    `state`, `heartbeats` [{service, state, last_beat, note}], `bars` [{tf, t, n24}],
    `indicators_m30_latest`, `signals` {t, n24}, `risk` {state, ref_source} | null,
    `control_desired`. One difference, on purpose: where V1.6.4's server filled in defaults
    for missing rows (scheduler "PAUSED", desired "RUN"), this answers null — nothing is
    reported that no record shows (the watchdog acts only on desired == "PAUSE").

    `?detail=true` (never sent by the watchdog) adds `diagnostics`: heartbeat ages, the last
    accepted upload per ingest endpoint, open/failed collector jobs and failed alerts.
    Every query reads an index (latest row per key, last-24 h ranges), so a 5-second poll
    stays cheap. A database failure answers 503, never a guessed state."""
    try:
        return await _health_summary(s, detail)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "database unavailable") from None


def _iso(t: dt.datetime | None) -> str | None:
    """UTC as `+00:00` ISO text: the watchdog parses these with datetime.fromisoformat, which
    rejects the JSON encoder's `Z` suffix on Python < 3.11."""
    return t.astimezone(dt.UTC).isoformat() if t is not None else None


async def _health_summary(s: AsyncSession, detail: bool) -> dict[str, Any]:
    sym = {"sym": config.SYMBOL}
    sched = await db.q(
        s, "select state from aureon.service_heartbeats where service='scheduler'", one=True
    )
    hbs = await db.q(
        s,
        """select service,state,last_beat,note from aureon.service_heartbeats
           where last_beat is not null order by service""",
    )
    bars = []
    for tf in ("H1", "M30"):  # index: bars(symbol, tf, time_utc)
        r = await db.q(
            s,
            """select max(time_utc) t from aureon.bars where symbol=:sym and tf=:tf""",
            {**sym, "tf": tf},
            one=True,
        )
        if r and r["t"] is not None:
            n = await db.q(
                s,
                """select count(*) n from aureon.bars where symbol=:sym and tf=:tf
                   and time_utc > now() - interval '1 day'""",
                {**sym, "tf": tf},
                one=True,
            )
            bars.append({"tf": tf, "t": _iso(r["t"]), "n24": int(n["n"])})
    ind = await db.q(
        s,
        """select max(time_utc) t from aureon.indicators
           where symbol=:sym and tf='M30' and rules_version=:rv""",
        {**sym, "rv": config.RULES_VERSION},
        one=True,
    )
    sig = await db.q(
        s,
        """select (select max(time_utc) from aureon.signals where source='live' and symbol=:sym) t,
           (select count(*) from aureon.signals where source='live' and symbol=:sym
            and time_utc > now() - interval '1 day') n24""",
        sym,
        one=True,
    )
    risk = await db.q(
        s,
        """select state, ref_source from aureon.risk_state
           order by broker_day desc, updated_at desc limit 1""",
        one=True,
    )
    desired = await db.q(s, "select value from aureon.control where key='desired'", one=True)
    out: dict[str, Any] = {
        "state": sched["state"] if sched else None,
        "heartbeats": [{**h, "last_beat": _iso(h["last_beat"])} for h in hbs],
        "bars": bars,
        "indicators_m30_latest": _iso(ind["t"]) if ind else None,
        "signals": {"t": _iso(sig["t"]), "n24": int(sig["n24"])},
        "risk": risk,
        "control_desired": desired["value"] if desired else None,
    }
    if detail:
        out["diagnostics"] = await _health_diagnostics(s, hbs)
    return out


async def _health_diagnostics(s: AsyncSession, hbs: list[dict[str, Any]]) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    stale_after = config.HEARTBEAT_SEC * config.HEARTBEAT_MISS_LIMIT
    ages = {
        h["service"]: {
            "age_s": round((now - h["last_beat"]).total_seconds(), 1),
            "stale": (now - h["last_beat"]).total_seconds() > stale_after,
        }
        for h in hbs
    }
    ingest = await db.q(
        s,
        """select path, max(time_utc) t from aureon.api_audit
           where time_utc > now() - interval '2 days' and method='POST'
           and status between 200 and 299
           and path in ('/api/v1/ingest/bars','/api/v1/ingest/mt5_deals','/api/v1/ingest/heartbeat')
           group by path""",
    )
    jobs_open = await db.q(
        s,
        """select state, count(*) n from aureon.jobs where done_at is null
           and state in ('pending','running') group by state""",
    )
    failed = await db.q(
        s,
        """select id, kind, state, attempts, result, coalesce(done_at, claimed_at) at
           from aureon.jobs where state in ('failed','dead')
           or (state='pending' and attempts>0 and result is not null)
           order by id desc limit 5""",
    )
    alerts_failed = await db.q(
        s,
        """select alert_key, last_error, created_at at from aureon.alerts
           where state='FAILED' order by id desc limit 5""",
    )
    names = {"/api/v1/ingest/bars": "bars", "/api/v1/ingest/mt5_deals": "mt5_deals",
             "/api/v1/ingest/heartbeat": "heartbeat"}  # fmt: skip
    return {
        "generated_at": now,
        "heartbeat_stale_after_s": stale_after,
        "heartbeat_ages": ages,
        "last_ingest": {
            n: next((r["t"] for r in ingest if r["path"] == p), None) for p, n in names.items()
        },
        "jobs": {
            "pending": next((int(r["n"]) for r in jobs_open if r["state"] == "pending"), 0),
            "running": next((int(r["n"]) for r in jobs_open if r["state"] == "running"), 0),
            "recent_failures": failed,
        },
        "errors": [
            {
                "source": f"job:{j['kind']}",
                "id": j["id"],
                "state": j["state"],
                "detail": j["result"],
                "at": j["at"],
            }
            for j in failed
        ]
        + [
            {
                "source": "alert",
                "id": a["alert_key"],
                "state": "FAILED",
                "detail": a["last_error"],
                "at": a["at"],
            }
            for a in alerts_failed
        ],
    }


@router.get("/ingest/last_bar")
async def ingest_last_bar(s: DbSession, _r: ServiceRole) -> dict[str, Any] | None:
    return await db.last_closed_m30(s)


class AlertIn(BaseModel):
    alert_key: str = Field(max_length=120)
    kind: str = Field(max_length=30)
    payload: str = Field(max_length=1900)


@router.post("/ingest/alert", status_code=201)
async def alert_in(a: AlertIn, s: DbSession, _r: ServiceRole) -> dict[str, Any]:
    await db.alert_enqueue(s, a.alert_key, a.kind, a.payload)
    await s.commit()
    return {"ok": True}


CONTROL_KEYS = r"^(reconcile_request|lockout_until|day_start:\d{4}-\d{2}-\d{2})$"


class ControlIn(BaseModel):
    key: str = Field(pattern=CONTROL_KEYS)
    value: str = Field(max_length=100)
    by: str = Field(default="guard", max_length=50)


@router.get("/ingest/control")
async def control_get(
    s: DbSession, _r: ServiceRole, key: str = Query(pattern=CONTROL_KEYS)
) -> dict[str, Any]:
    return {"key": key, "value": await db.get_control(s, key)}


@router.post("/ingest/control", status_code=201)
async def control_set(c: ControlIn, s: DbSession, _r: ServiceRole) -> dict[str, Any]:
    await db.set_control(s, c.key, c.value, c.by)
    await s.commit()
    return {"ok": True}


# ---------------- ingest: guard ----------------
@router.get("/ingest/recent_signal")
async def ingest_recent_signal(
    s: DbSession, _r: GuardRole, dir: int = Query(ge=-1, le=1)
) -> dict[str, Any] | None:
    return await db.recent_signal(s, dir)


@router.get("/ingest/trades_open")
async def ingest_trades_open(s: DbSession, _r: GuardRole) -> list[dict[str, Any]]:
    return await db.open_trades(s)


@router.get("/ingest/trade_by_ticket")
async def ingest_trade_by_ticket(
    s: DbSession, _r: GuardRole, ticket: int = Query(ge=1)
) -> dict[str, Any] | None:
    return await db.trade_by_ticket(s, ticket)


@router.get("/ingest/daily_pnl")
async def ingest_daily_pnl(s: DbSession, _r: GuardRole) -> dict[str, Any]:
    return {"pnl": await db.daily_pnl(s)}


class TradeIn(BaseModel):
    signal_id: int | None = None
    mt5_ticket: int = Field(ge=1)
    dir: int
    lots: float = Field(gt=0, le=100)
    entry_time: str
    entry_price: float = Field(gt=config.PRICE_MIN, lt=config.PRICE_MAX)
    sl: float | None = None
    tp: float | None = None

    @field_validator("dir")
    @classmethod
    def _dir(cls, v: int) -> int:
        if v not in (-1, 1):
            raise ValueError("dir must be -1 or 1")
        return v


@router.post("/ingest/trade", status_code=201)
async def ingest_trade(t: TradeIn, s: DbSession, _r: GuardRole) -> dict[str, Any]:
    d = t.model_dump()
    d["entry_time"] = parse_dt(d["entry_time"], "entry_time")
    if d["sl"] is not None and (
        (d["dir"] > 0 and d["sl"] >= d["entry_price"])
        or (d["dir"] < 0 and d["sl"] <= d["entry_price"])
    ):
        raise HTTPException(422, "sl on wrong side of entry")
    existing = await db.trade_by_ticket(s, d["mt5_ticket"])
    if existing:
        return {"id": existing["id"], "existing": True}
    if d["signal_id"] is not None and not await db.q(
        s, "select 1 from aureon.signals where id=:i", {"i": d["signal_id"]}, one=True
    ):
        raise HTTPException(422, "signal_id not found")
    tid = await db.insert_trade(s, d)
    await s.commit()
    return {"id": tid}


EXIT_REASONS = (
    "^(mt5_closed|mt5_closed_reconciled|unreconciled|opp_vwap_cross|flat_roll|flat_friday|"
    "daily_loss_limit|lock_level_breached|naked_failsafe)$"
)
STATUS = "^(open|closed|unreconciled|closing)$"


class TradePatch(BaseModel):
    expected_status: str | None = Field(default=None, pattern=STATUS)  # atomic precondition
    status: str | None = Field(default=None, pattern=STATUS)
    exit_time: str | None = None
    close_sent_at: str | None = None
    close_request_id: str | None = Field(default=None, max_length=32)
    exit_price: float | None = Field(default=None, gt=config.PRICE_MIN, lt=config.PRICE_MAX)
    pnl_usd: float | None = Field(default=None, ge=-1e6, le=1e6)
    exit_reason: str | None = Field(default=None, pattern=EXIT_REASONS)
    sl: float | None = Field(default=None, gt=config.PRICE_MIN, lt=config.PRICE_MAX)
    tp: float | None = Field(default=None, gt=config.PRICE_MIN, lt=config.PRICE_MAX)


@router.patch("/ingest/trade/{tid}")
async def ingest_trade_patch(
    tid: int, f: TradePatch, s: DbSession, _r: GuardRole
) -> dict[str, Any]:
    cur = await db.q(
        s, "select id,status,dir,entry_price from aureon.trades where id=:i", {"i": tid}, one=True
    )
    if not cur:
        raise HTTPException(404, "trade not found")
    expected = f.expected_status
    upd: dict[str, Any] = {
        k: v for k, v in f.model_dump().items() if v is not None and k != "expected_status"
    }
    if f.exit_reason is None and "exit_reason" in f.model_fields_set:
        upd["exit_reason"] = None  # explicit clear (unreconciled -> open)
    if not upd:
        raise HTTPException(422, "nothing to update")
    if expected and cur["status"] != expected:
        raise HTTPException(409, f"status is {cur['status']}, expected {expected}")
    if cur["status"] == "closed":
        raise HTTPException(409, "trade is closed; no further changes")
    new_status = upd.get("status", cur["status"])
    exit_fields = {"exit_time", "exit_price", "pnl_usd"} & set(upd)
    if new_status == "closed":
        missing = {"exit_time", "exit_price", "pnl_usd", "exit_reason"} - set(upd)
        if missing:
            raise HTTPException(422, f"closing requires {sorted(missing)}")
        if upd["exit_reason"] == "unreconciled":
            raise HTTPException(422, "closed trades need a real exit_reason")
    elif new_status == "unreconciled":
        if upd.get("exit_reason", "unreconciled") != "unreconciled" or exit_fields:
            raise HTTPException(422, "unreconciled carries no exit fields")
    elif new_status == "closing":
        if exit_fields or "exit_reason" in upd:
            raise HTTPException(422, "closing carries no exit fields")
        if "close_sent_at" in upd:
            upd["close_sent_at"] = parse_dt(upd["close_sent_at"], "close_sent_at")
    else:  # stays/returns open (unreconciled -> open: the broker still shows the position)
        # An explicit `exit_reason: null` clears the reason (V1.6.3's guard sends it when it
        # re-adopts a position); V1.6.3's server rejected that clear, which this fixes.
        if exit_fields or upd.get("exit_reason") is not None:
            raise HTTPException(422, "open trade cannot carry exit fields")
    if "close_sent_at" in upd and isinstance(upd["close_sent_at"], str):
        upd["close_sent_at"] = parse_dt(upd["close_sent_at"], "close_sent_at")
    if "sl" in upd and (
        (cur["dir"] > 0 and upd["sl"] >= cur["entry_price"] * 1.5)
        or (cur["dir"] < 0 and upd["sl"] <= cur["entry_price"] * 0.5)
    ):
        raise HTTPException(422, "sl implausible")
    if "exit_time" in upd:
        upd["exit_time"] = parse_dt(upd["exit_time"], "exit_time")
        if upd["exit_time"].replace(tzinfo=None) > dt.datetime.now(dt.UTC).replace(
            tzinfo=None
        ) + dt.timedelta(minutes=5):
            raise HTTPException(422, "exit_time in the future")
    ok = await db.update_trade(s, tid, expected_status=expected, **upd)
    if not ok:
        raise HTTPException(409, "concurrent update: status changed")
    await s.commit()
    return {"ok": True}


class EventIn(BaseModel):
    trade_id: int
    event: str = Field(pattern="^(attach|sl_set|lock|flat|exit|limit_block|close_sent)$")
    old_sl: float | None = None
    new_sl: float | None = None
    note: str = Field(default="", max_length=300)


@router.post("/ingest/trade_event", status_code=201)
async def ingest_event(e: EventIn, s: DbSession, _r: GuardRole) -> dict[str, Any]:
    if not await db.q(s, "select 1 from aureon.trades where id=:i", {"i": e.trade_id}, one=True):
        raise HTTPException(404, "trade not found")
    await db.event(s, e.trade_id, e.event, e.old_sl, e.new_sl, e.note)
    await s.commit()
    return {"ok": True}


class RiskRow(BaseModel):
    broker_day: str
    account_id: str = Field(max_length=40)
    ref_equity: float | None = None
    ref_balance: float | None = None
    ref_source: str | None = None
    realized: float | None = None
    floating: float | None = None
    equity: float | None = None
    balance: float | None = None
    drawdown: float | None = None
    deposits: float | None = None
    state: str = Field(pattern="^(UNLOCKED|WARNING|LOCKED|UNKNOWN)$")
    locked_at: str | None = None
    lock_reason: str | None = None
    cleared_at: str | None = None
    cleared_by: str | None = None
    clear_reason: str | None = None
    equity_at: str | None = None
    history_suspect: bool | None = None


@router.get("/ingest/risk")
async def risk_get(day: str, account_id: str, s: DbSession, _r: GuardRole) -> dict[str, Any] | None:
    return await db.risk_get(s, parse_day(day), account_id)


@router.post("/ingest/risk", status_code=201)
async def risk_set(r: RiskRow, s: DbSession, _r: GuardRole) -> dict[str, Any]:
    day = parse_day(r.broker_day, "broker_day")
    cur = await db.risk_get(s, day, r.account_id)
    if cur and cur["state"] == "LOCKED" and r.state != "LOCKED":
        raise HTTPException(409, "LOCKED can only be cleared by an admin")  # guard cannot unlock
    d = r.model_dump()
    d["broker_day"] = day
    for k in db.RISK_TIMES:
        if d[k] is not None:
            d[k] = parse_dt(d[k], k)
    await db.risk_upsert(s, d)
    await s.commit()
    return {"ok": True}


class RiskEvent(BaseModel):
    broker_day: str
    account_id: str
    event: str = Field(max_length=40)
    detail: dict[str, Any] = {}
    actor: str = Field(max_length=50)


@router.post("/ingest/risk_event", status_code=201)
async def risk_event(e: RiskEvent, s: DbSession, _r: GuardRole) -> dict[str, Any]:
    await db.risk_event(
        s, parse_day(e.broker_day, "broker_day"), e.account_id, e.event, e.detail, e.actor
    )
    await s.commit()
    return {"ok": True}


# ---------------- admin ----------------
class AdminClear(BaseModel):
    account_id: str
    reason: str = Field(min_length=5, max_length=200)


@router.post("/admin/risk/clear")
async def admin_risk_clear(
    c: AdminClear, request: Request, s: DbSession, _r: AdminRole
) -> dict[str, Any]:
    client = request.client.host if request.client else "-"
    ok = await admin_clear(s, c.account_id, f"api-admin@{client}", c.reason)
    if not ok:
        raise HTTPException(409, "no active lock")
    audit.record(
        s,
        "aureon_risk_clear",
        audit.ClientInfo.from_request(request),
        details={"account_id": c.account_id, "reason": c.reason},
    )
    await s.commit()
    return {"ok": True}


class ReviewRun(BaseModel):
    week: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    force: bool = False
    account_id: str | None = Field(default=None, max_length=40)


@router.post("/admin/review/run", status_code=202)
async def admin_review_run(
    b: ReviewRun, request: Request, s: DbSession, _r: AdminRole
) -> dict[str, Any]:
    """Start (or advance) the weekend review for a week. It is read-only toward MT5: missing
    candles and the broker's deals are requested as collector jobs, and the scheduler carries
    the review on until the report is written."""
    if b.week:
        parse_day(b.week, "week")
    maker = request.app.state.sessionmaker
    out = await wr.step(maker, week_start=b.week, force=b.force, account_id=b.account_id)
    audit.record(
        s,
        "aureon_review_run",
        audit.ClientInfo.from_request(request),
        details={"week": b.week, "force": b.force, "report_id": out.get("report_id")},
    )
    await s.commit()
    return {k: v for k, v in out.items() if k != "summary"}


# ---------------- weekly reports (V1.6.3 weekend review) ----------------
@router.get("/review/reports")
async def review_reports(
    s: DbSession, _r: Role, limit: int = Query(20, ge=1, le=200)
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = await db.q(
        s,
        """select id,account_id,symbol,week_start,strategy_version,state,phase,started_at,
           finished_at,attempts,error from aureon.weekly_reports
           order by week_start desc, id desc limit :l""",
        {"l": limit},
    )
    return rows


@router.get("/review/reports/{rid}")
async def review_report(rid: int, s: DbSession, _r: Role) -> dict[str, Any]:
    r = await db.q(s, "select * from aureon.weekly_reports where id=:i", {"i": rid}, one=True)
    if not r:
        raise HTTPException(404, "report not found")
    r["file_names"] = [
        x["name"]
        for x in await db.q(
            s,
            "select name from aureon.weekly_report_files where report_id=:i order by name",
            {"i": rid},
        )
    ]
    out: dict[str, Any] = r
    return out


@router.get("/review/reports/{rid}/files/{name}")
async def review_report_file(rid: int, name: str, s: DbSession, _r: Role) -> Response:
    f = await db.q(
        s,
        "select content_type, body from aureon.weekly_report_files where report_id=:i and name=:n",
        {"i": rid, "n": name},
        one=True,
    )
    if not f:
        raise HTTPException(404, "file not found")
    return Response(
        content=f["body"],
        media_type=f["content_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )
