"""Aureon V1.6.3 on the Assembler against PostgreSQL + Redis (docs/34-aureon-v163.md).

Every endpoint's contract and status codes, role isolation, persistence and idempotency
(bars, deals, trades, alerts), the trade state machine and its race, risk LOCKED/admin clear
with audit, the collector job queue (claim, renew, finish, fencing, expiry, retries, dead,
concurrent claims), the scheduler (collector offline -> reconnect, backfill ack, gaps), the
detector, the weekend review end to end (coverage, recovery jobs, deal history, matching,
reports, dedup, force, restart recovery, auto trigger), alert delivery, api_audit, the rate
limits and the body limit. Run with scripts/test_db.sh.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import numpy as np
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.aureon import alerter, config, db, detector, scheduler, weekend_review
from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.db

TOK = {
    "reader": "rd-Lq3Zp8Tw1Ns6Vb4Hk9Rd2Mf7Gc5Jx0Ya",
    "writer": "wr-Pn4Xc7Bv2Lk9Qz6Tm1Hs8Dj5Rf3Gw0Ue",
    "collector": "co-Ty6Ha1Kr8Vm3Pq9Zs4Bn7Lx2Wc5Jd0Ef",
    "guard": "gu-Mb5Rw2Xn8Kd1Tq7Fz4Hp9Vs6Lc3Jy0Ga",
    "service": "sv-Qe9Jt3Wm6Bx1Kp8Zr5Hd2Nv7Lf4Cy0Ts",
    "admin": "ad-Zk7Pv4Ry1Mq8Xs5Lb2Hw9Td6Nc3Jf0Ge",
}
SETTING = {
    "reader": "AUREON_API_READ_TOKEN",
    "writer": "AUREON_API_WRITE_TOKEN",
    "collector": "AUREON_API_COLLECTOR_TOKEN",
    "guard": "AUREON_API_GUARD_TOKEN",
    "service": "AUREON_API_SERVICE_TOKEN",
    "admin": "AUREON_API_ADMIN_TOKEN",
}
AUREON_TABLES = (
    "weekly_report_files, replay_detections, mt5_deals, weekly_reports, api_audit, alerts, "
    "risk_events, risk_state, control, jobs, service_heartbeats, weekly_review, hypo_outcomes, "
    "trade_events, trades, decisions, signals, rules_versions, market_calendar, indicators, bars"
)
MON = dt.datetime(2026, 11, 2)


def H(role: str | None) -> dict[str, str]:
    return {"authorization": f"Bearer {TOK[role]}"} if role else {}


class Env:
    def __init__(self, c: AsyncClient, app: Any) -> None:
        self.c, self.app = c, app

    @property
    def maker(self) -> async_sessionmaker[AsyncSession]:
        m: async_sessionmaker[AsyncSession] = self.app.state.sessionmaker
        return m

    async def call(self, role: str | None, method: str, path: str, **kw: Any) -> httpx.Response:
        return await self.c.request(method, path, headers=H(role) | kw.pop("headers", {}), **kw)

    async def sql(self, q: str, p: dict[str, Any] | None = None, one: bool = False) -> Any:
        async with self.maker() as s, s.begin():
            return await db.q(s, q, p or {}, one=one)


async def _clean(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                f"TRUNCATE {', '.join('aureon.' + t.strip() for t in AUREON_TABLES.split(','))} RESTART IDENTITY CASCADE"
            )
        )
        await conn.execute(
            text("insert into aureon.control(key,value,updated_by) values('desired','RUN','init')")
        )


@pytest.fixture
def overrides() -> dict[str, Any]:
    return {}


@pytest.fixture
async def env(db_clean: str, settings: Settings, overrides: dict[str, Any]) -> AsyncIterator[Env]:
    engine = create_async_engine(db_clean)
    await _clean(engine)
    s = Settings.model_validate(
        {
            **settings.model_dump(),
            "DATABASE_URL": db_clean,
            **{SETTING[r]: t for r, t in TOK.items()},
            **overrides,
        }
    )
    app = create_app(s)
    async with (
        LifespanManager(app) as mgr,
        AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c,
    ):
        yield Env(c, app)
    await _clean(engine)
    await engine.dispose()


# ---------------- helpers ----------------
def bar_rows(times: list[dt.datetime], seed: int = 3, start_px: float = 4000.0) -> list[list[Any]]:
    rng = np.random.default_rng(seed)
    px = start_px
    out = []
    for k, t in enumerate(times):
        drift = 1.5 * np.sin(k / 6) + rng.normal(0, 2)
        o, c = px, px + drift
        h, lo = max(o, c) + rng.uniform(0, 2), min(o, c) - rng.uniform(0, 2)
        out.append(
            [
                t.isoformat(),
                round(o, 2),
                round(h, 2),
                round(lo, 2),
                round(c, 2),
                int(rng.integers(500, 5000)),
                5,
            ]
        )
        px = c
    return out


async def post_bars(env: Env, tf: str, rows: list[list[Any]], source: str = "live") -> None:
    for i in range(0, len(rows), 500):
        r = await env.call(
            "collector",
            "POST",
            "/api/v1/ingest/bars",
            json={"tf": tf, "source": source, "rows": rows[i : i + 500]},
        )
        assert r.status_code == 201, r.text


def session_times(start: dt.datetime, end: dt.datetime, tf: str) -> list[dt.datetime]:
    return scheduler.expected_bars(start, end, tf)


async def live_signal(env: Env, minutes_ago: int = 10, tradeable: bool = True, d: int = 1) -> int:
    t = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)
    r = await env.sql(
        """insert into aureon.signals(symbol,tf,time_utc,rules_version,source,kind,dir,price,atr,suggested_sl,features)
           values('XAUUSD','M30',:t,'R3.0','live','vwap_cross',:d,4000,10,15,cast(:f as jsonb)) returning id""",
        {
            "t": t,
            "d": d,
            "f": json.dumps(
                {
                    "tradeable": tradeable,
                    "ema_agree": True,
                    "side200": 1,
                    "vwap_flips8": 1,
                    "hour_utc": 10,
                    "bar_of_day": 9,
                }
            ),
        },
        one=True,
    )
    return int(r["id"])


# ---------------- auth: roles, invalid tokens, isolation ----------------
ROUTES: list[tuple[str, str, dict[str, Any], set[str]]] = [
    ("GET", "/api/v1/status", {}, {"reader", "writer", "admin"}),
    ("GET", "/api/v1/signals", {}, {"reader", "writer", "admin"}),
    ("GET", "/api/v1/trades", {}, {"reader", "writer", "admin"}),
    ("GET", "/api/v1/risk", {}, {"reader", "writer", "admin"}),
    ("GET", "/api/v1/review", {}, {"reader", "writer", "admin"}),
    ("GET", "/api/v1/review/latest", {}, {"reader", "writer", "admin"}),
    ("GET", "/api/v1/review/reports", {}, {"reader", "writer", "admin"}),
    ("POST", "/api/v1/decisions", {"json": {"signal_id": 999999, "tag": "take"}}, {"writer"}),
    ("POST", "/api/v1/ingest/bars", {"json": {"tf": "M30", "rows": []}}, {"collector"}),
    ("POST", "/api/v1/ingest/mt5_deals", {"json": {"deals": []}}, {"collector"}),
    ("POST", "/api/v1/ingest/jobs/claim", {"json": {"worker": "w"}}, {"collector"}),
    (
        "POST",
        "/api/v1/ingest/jobs/1/renew",
        {"json": {"worker": "w", "token": "abcdefgh"}},
        {"collector"},
    ),
    (
        "POST",
        "/api/v1/ingest/jobs/1/finish",
        {"json": {"ok": True, "worker": "w", "token": "abcdefgh"}},
        {"collector"},
    ),
    (
        "POST",
        "/api/v1/ingest/heartbeat",
        {"json": {"service": "guard", "state": "LIVE"}},
        {"collector", "guard", "service"},
    ),
    ("GET", "/api/v1/ingest/state", {}, {"collector", "guard", "service"}),
    ("GET", "/api/v1/ingest/last_bar", {}, {"collector", "guard", "service"}),
    (
        "POST",
        "/api/v1/ingest/alert",
        {"json": {"alert_key": "k", "kind": "adhoc", "payload": "x"}},
        {"collector", "guard", "service"},
    ),
    (
        "GET",
        "/api/v1/ingest/control",
        {"params": {"key": "reconcile_request"}},
        {"collector", "guard", "service"},
    ),
    (
        "POST",
        "/api/v1/ingest/control",
        {"json": {"key": "reconcile_request", "value": ""}},
        {"collector", "guard", "service"},
    ),
    ("GET", "/api/v1/ingest/recent_signal", {"params": {"dir": 1}}, {"guard"}),
    ("GET", "/api/v1/ingest/trades_open", {}, {"guard"}),
    ("GET", "/api/v1/ingest/trade_by_ticket", {"params": {"ticket": 1}}, {"guard"}),
    ("GET", "/api/v1/ingest/daily_pnl", {}, {"guard"}),
    ("GET", "/api/v1/ingest/health_summary", {}, {"collector", "guard", "service"}),
    (
        "POST",
        "/api/v1/ingest/trade_event",
        {"json": {"trade_id": 999999, "event": "lock"}},
        {"guard"},
    ),
    (
        "GET",
        "/api/v1/ingest/risk",
        {"params": {"day": "2026-11-02", "account_id": "777"}},
        {"guard"},
    ),
    (
        "POST",
        "/api/v1/ingest/risk_event",
        {"json": {"broker_day": "2026-11-02", "account_id": "777", "event": "x", "actor": "t"}},
        {"guard"},
    ),
    ("PATCH", "/api/v1/ingest/trade/999999", {"json": {"sl": 3990.0}}, {"guard"}),
    (
        "POST",
        "/api/v1/admin/risk/clear",
        {"json": {"account_id": "777", "reason": "provider allows"}},
        {"admin"},
    ),
]


async def test_invalid_and_missing_tokens(env: Env) -> None:
    assert (await env.call(None, "GET", "/api/v1/status")).status_code == 401
    assert (
        await env.c.get("/api/v1/status", headers={"authorization": "Bearer nope"})
    ).status_code == 401
    assert (
        await env.c.get("/api/v1/status", headers={"authorization": f"Basic {TOK['reader']}"})
    ).status_code == 401
    # a browser session never authorizes these routes
    assert (
        await env.c.get("/api/v1/ingest/trades_open", cookies={"__Host-spasta_session": "x"})
    ).status_code == 401


async def test_role_isolation_matrix(env: Env) -> None:
    for method, path, kw, allowed in ROUTES:
        for role in TOK:
            r = await env.call(role, method, path, **kw)
            if role in allowed:
                assert r.status_code not in (401, 403), (role, method, path, r.status_code, r.text)
            else:
                assert r.status_code == 403, (role, method, path, r.status_code)


async def test_disabled_without_tokens(db_clean: str, settings: Settings) -> None:
    s = Settings.model_validate({**settings.model_dump(), "DATABASE_URL": db_clean})
    app = create_app(s)
    async with (
        LifespanManager(app) as mgr,
        AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c,
    ):
        assert (await c.get("/api/v1/status", headers=H("reader"))).status_code == 503
        assert (await c.get("/api/v1/aureon/healthz")).status_code == 200


# ---------------- bars ----------------
async def test_bar_validation_and_idempotent_upsert(env: Env) -> None:
    good = ["2026-11-02T01:00:00", 4000, 4010, 3995, 4005, 100, 5]
    ok = await env.call(
        "collector", "POST", "/api/v1/ingest/bars", json={"tf": "M30", "rows": [good]}
    )
    assert ok.status_code == 201 and ok.json() == {"n": 1}
    dup = await env.call(
        "collector",
        "POST",
        "/api/v1/ingest/bars",
        json={"tf": "M30", "rows": [good, [*good[:4], 4007, 100, 5]]},
    )
    assert dup.status_code == 201
    rows = await env.sql("select close, time_utc from aureon.bars")
    assert (
        len(rows) == 1 and rows[0]["close"] == 4007 and rows[0]["time_utc"].tzinfo is not None
    )  # last wins, UTC
    tz = await env.call(
        "collector",
        "POST",
        "/api/v1/ingest/bars",
        json={"tf": "M30", "rows": [["2026-11-02T03:00:00+02:00", 4000, 4010, 3995, 4005, 1, 1]]},
    )
    assert (
        tz.status_code == 201
        and len(await env.sql("select 1 from aureon.bars where time_utc='2026-11-02T01:00:00Z'"))
        == 1
    )
    bad = [
        ["2026-11-02T01:00:00", 4000, 3990, 3995, 4005, 100, 5],  # high < close
        ["2026-11-02T01:17:00", 4000, 4010, 3995, 4005, 100, 5],  # not aligned
        ["2026-11-02T01:00:00", -4000, 4010, 3995, 4005, 100, 5],  # bounds
        ["2026-11-02T01:00:00", 4000, 4010, 3995, 4005, -1, 5],  # volume
        ["2026-11-02T01:00:00", 4000, 4010, 3995],  # arity
        ["nope", 4000, 4010, 3995, 4005, 1, 1],
    ]
    for row in bad:
        assert (
            await env.call(
                "collector", "POST", "/api/v1/ingest/bars", json={"tf": "M30", "rows": [row]}
            )
        ).status_code == 422, row
    fut = (
        (dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(hours=3))
        .replace(minute=0, second=0, microsecond=0)
        .isoformat()
    )
    assert (
        await env.call(
            "collector",
            "POST",
            "/api/v1/ingest/bars",
            json={"tf": "M30", "rows": [[fut, 4000, 4010, 3995, 4005, 1, 5]]},
        )
    ).status_code == 422
    assert (
        await env.call("collector", "POST", "/api/v1/ingest/bars", json={"tf": "M5", "rows": []})
    ).status_code == 422
    assert (
        await env.call(
            "collector", "POST", "/api/v1/ingest/bars", json={"tf": "M30", "rows": [good] * 501}
        )
    ).status_code == 422
    # collector backfill jobs upload with source "backfill" (V1.6.3 collector.backfill_range)
    assert (
        await env.call(
            "collector",
            "POST",
            "/api/v1/ingest/bars",
            json={"tf": "H1", "source": "backfill", "rows": [good]},
        )
    ).status_code == 201


# ---------------- trades ----------------
async def test_trade_create_update_read_and_state_machine(env: Env) -> None:
    sid = await live_signal(env)
    body = {
        "signal_id": sid,
        "mt5_ticket": 7,
        "dir": 1,
        "lots": 0.1,
        "entry_time": "2026-11-02T01:00:00",
        "entry_price": 4000.0,
        "sl": 4010.0,
    }
    assert (
        await env.call("guard", "POST", "/api/v1/ingest/trade", json=body)
    ).status_code == 422  # sl wrong side
    assert (
        await env.call(
            "guard", "POST", "/api/v1/ingest/trade", json={**body, "sl": 3985.0, "dir": 2}
        )
    ).status_code == 422
    assert (
        await env.call(
            "guard",
            "POST",
            "/api/v1/ingest/trade",
            json={**body, "sl": 3985.0, "signal_id": 999999},
        )
    ).status_code == 422
    r = await env.call("guard", "POST", "/api/v1/ingest/trade", json={**body, "sl": 3985.0})
    assert r.status_code == 201
    tid = r.json()["id"]
    again = await env.call("guard", "POST", "/api/v1/ingest/trade", json={**body, "sl": 3985.0})
    assert again.json() == {"id": tid, "existing": True}  # duplicate ticket -> the existing row
    assert (
        await env.call("guard", "GET", "/api/v1/ingest/trade_by_ticket", params={"ticket": 7})
    ).json() == {"id": tid, "status": "open"}
    assert (
        await env.call("guard", "GET", "/api/v1/ingest/trade_by_ticket", params={"ticket": 8})
    ).json() is None
    assert [
        t["mt5_ticket"]
        for t in (await env.call("guard", "GET", "/api/v1/ingest/trades_open")).json()
    ] == [7]

    p = f"/api/v1/ingest/trade/{tid}"
    assert (await env.call("guard", "PATCH", p, json={"status": "closed"})).status_code == 422
    assert (await env.call("guard", "PATCH", p, json={})).status_code == 422
    assert (
        await env.call("guard", "PATCH", p, json={"sl": 9000.0})
    ).status_code == 422  # implausible
    assert (await env.call("guard", "PATCH", p, json={"sl": 3995.0})).status_code == 200
    assert (
        await env.call("guard", "PATCH", p, json={"status": "closing", "exit_price": 4010.0})
    ).status_code == 422
    assert (
        await env.call(
            "guard",
            "PATCH",
            p,
            json={
                "status": "closing",
                "close_sent_at": dt.datetime.now(dt.UTC).isoformat(),
                "close_request_id": "abc",
            },
        )
    ).status_code == 200
    ev = await env.call(
        "guard",
        "POST",
        "/api/v1/ingest/trade_event",
        json={"trade_id": tid, "event": "close_sent", "note": "flat rid=abc"},
    )
    assert ev.status_code == 201  # V1.6.3's guard sends close_sent
    assert (
        await env.call(
            "guard", "POST", "/api/v1/ingest/trade_event", json={"trade_id": tid, "event": "nuke"}
        )
    ).status_code == 422
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    full = {
        "status": "closed",
        "exit_reason": "opp_vwap_cross",
        "exit_price": 4010.0,
        "pnl_usd": 50,
        "exit_time": now.isoformat(),
    }
    assert (
        await env.call("guard", "PATCH", p, json={**full, "expected_status": "open"})
    ).status_code == 409  # it is closing
    assert (
        await env.call("guard", "PATCH", p, json={**full, "exit_reason": "unreconciled"})
    ).status_code == 422
    future = {**full, "exit_time": (now + dt.timedelta(hours=1)).isoformat()}
    assert (
        await env.call("guard", "PATCH", p, json={**future, "expected_status": "closing"})
    ).status_code == 422
    assert (
        await env.call("guard", "PATCH", p, json={**full, "expected_status": "closing"})
    ).status_code == 200
    assert (
        await env.call("guard", "PATCH", p, json={"status": "open"})
    ).status_code == 409  # closed is final
    assert (await env.call("guard", "GET", "/api/v1/ingest/daily_pnl")).json() == {"pnl": 50.0}
    assert (await env.call("guard", "GET", "/api/v1/ingest/trades_open")).json() == []
    trade = (await env.call("reader", "GET", f"/api/v1/signals/{sid}")).json()["trade"]
    assert trade["status"] == "closed" and [e["event"] for e in trade["events"]] == ["close_sent"]
    assert (
        await env.call("guard", "PATCH", "/api/v1/ingest/trade/999999", json={"sl": 3990.0})
    ).status_code == 404


async def test_unreconciled_round_trip(env: Env) -> None:
    tid = (
        await env.call(
            "guard",
            "POST",
            "/api/v1/ingest/trade",
            json={
                "mt5_ticket": 9,
                "dir": -1,
                "lots": 0.1,
                "entry_time": "2026-11-02T01:00:00",
                "entry_price": 4000.0,
                "sl": 4015.0,
            },
        )
    ).json()["id"]
    p = f"/api/v1/ingest/trade/{tid}"
    assert (
        await env.call(
            "guard",
            "PATCH",
            p,
            json={"status": "unreconciled", "exit_reason": "unreconciled", "exit_price": 4001.0},
        )
    ).status_code == 422
    assert (
        await env.call(
            "guard",
            "PATCH",
            p,
            json={
                "status": "unreconciled",
                "exit_reason": "unreconciled",
                "expected_status": "open",
            },
        )
    ).status_code == 200
    back = await env.call(
        "guard",
        "PATCH",
        p,
        json={"status": "open", "exit_reason": None, "expected_status": "unreconciled"},
    )
    assert back.status_code == 200  # the guard re-adopts a position the broker still shows
    row = await env.sql(
        "select status, exit_reason from aureon.trades where id=:i", {"i": tid}, one=True
    )
    assert row == {"status": "open", "exit_reason": None}


async def test_concurrent_trade_updates_one_wins(env: Env) -> None:
    tid = (
        await env.call(
            "guard",
            "POST",
            "/api/v1/ingest/trade",
            json={
                "mt5_ticket": 11,
                "dir": 1,
                "lots": 0.1,
                "entry_time": "2026-11-02T01:00:00",
                "entry_price": 4000.0,
                "sl": 3985.0,
            },
        )
    ).json()["id"]
    body = {
        "status": "closing",
        "close_sent_at": dt.datetime.now(dt.UTC).isoformat(),
        "close_request_id": "r",
        "expected_status": "open",
    }
    rs = await asyncio.gather(
        *(env.call("guard", "PATCH", f"/api/v1/ingest/trade/{tid}", json=body) for _ in range(4))
    )
    assert sorted(r.status_code for r in rs) == [200, 409, 409, 409]


# ---------------- risk ----------------
async def test_risk_lock_admin_clear_and_audit(env: Env) -> None:
    from app.aureon.risk import broker_day

    day = broker_day().isoformat()
    row = {
        "broker_day": day,
        "account_id": "777",
        "state": "LOCKED",
        "ref_equity": 1000.0,
        "equity": 930.0,
        "drawdown": -70.0,
        "equity_at": dt.datetime.now(dt.UTC).isoformat(),
        "locked_at": dt.datetime.now(dt.UTC).isoformat(),
        "lock_reason": "drawdown",
    }
    assert (
        await env.call("guard", "POST", "/api/v1/ingest/risk", json={**row, "state": "NOPE"})
    ).status_code == 422
    assert (
        await env.call("guard", "POST", "/api/v1/ingest/risk", json={**row, "broker_day": "11/02"})
    ).status_code == 422
    assert (await env.call("guard", "POST", "/api/v1/ingest/risk", json=row)).status_code == 201
    got = (
        await env.call(
            "guard", "GET", "/api/v1/ingest/risk", params={"day": day, "account_id": "777"}
        )
    ).json()
    assert got["state"] == "LOCKED" and got["ref_equity"] == 1000.0
    assert (
        await env.call(
            "guard", "GET", "/api/v1/ingest/risk", params={"day": day, "account_id": "888"}
        )
    ).json() is None
    assert (
        await env.call("guard", "POST", "/api/v1/ingest/risk", json={**row, "state": "UNLOCKED"})
    ).status_code == 409  # guard cannot unlock
    assert (
        await env.call(
            "guard",
            "POST",
            "/api/v1/ingest/risk_event",
            json={
                "broker_day": day,
                "account_id": "777",
                "event": "lock",
                "detail": {"drawdown": -70},
                "actor": "guard",
            },
        )
    ).status_code == 201
    short = await env.call(
        "admin", "POST", "/api/v1/admin/risk/clear", json={"account_id": "777", "reason": "no"}
    )
    assert short.status_code == 422
    ok = await env.call(
        "admin",
        "POST",
        "/api/v1/admin/risk/clear",
        json={"account_id": "777", "reason": "provider allows re-entry"},
    )
    assert ok.status_code == 200
    st = await env.sql(
        "select state, cleared_by, clear_reason from aureon.risk_state where account_id='777'",
        one=True,
    )
    assert (
        st["state"] == "UNLOCKED"
        and st["cleared_by"].startswith("api-admin@")
        and st["clear_reason"] == "provider allows re-entry"
    )
    events = [r["event"] for r in await env.sql("select event from aureon.risk_events order by id")]
    assert events == ["lock", "admin_clear"]
    audit = await env.sql(
        "select event_type, details from audit.audit_events where event_type='aureon_risk_clear'"
    )
    assert len(audit) == 1 and audit[0]["details"]["account_id"] == "777"
    assert (
        await env.call(
            "admin",
            "POST",
            "/api/v1/admin/risk/clear",
            json={"account_id": "777", "reason": "again please"},
        )
    ).status_code == 409
    risk = (await env.call("reader", "GET", "/api/v1/risk")).json()
    assert risk[0]["state"] == "UNLOCKED" and "cleared_by" not in risk[0]


# ---------------- collector jobs ----------------
async def _job(
    env: Env, kind: str = "backfill_range", payload: dict[str, Any] | None = None
) -> int:
    async with env.maker() as s, s.begin():
        return await db.request_job(
            s, kind, payload or {"start": "2026-11-02T00:00:00", "end": "2026-11-09T00:00:00"}
        )


async def test_job_claim_renew_finish_and_fencing(env: Env) -> None:
    jid = await _job(env)
    c = (
        await env.call(
            "collector", "POST", "/api/v1/ingest/jobs/claim", json={"worker": "collector@box"}
        )
    ).json()
    assert (
        c["id"] == jid
        and c["kind"] == "backfill_range"
        and c["state"] == "running"
        and len(c["lease_token"]) == 32
    )
    assert (
        c["payload"] == {"start": "2026-11-02T00:00:00", "end": "2026-11-09T00:00:00"}
        and c["attempts"] == 1
    )
    assert (
        await env.call("collector", "POST", "/api/v1/ingest/jobs/claim", json={"worker": "other"})
    ).json() is None
    assert (
        await env.call("collector", "POST", "/api/v1/ingest/jobs/claim", json={"worker": ""})
    ).status_code == 422
    tok = c["lease_token"]
    assert (
        await env.call(
            "collector",
            "POST",
            f"/api/v1/ingest/jobs/{jid}/renew",
            json={"worker": "collector@box", "token": tok},
        )
    ).json() == {"ok": True}
    assert (
        await env.call(
            "collector",
            "POST",
            f"/api/v1/ingest/jobs/{jid}/renew",
            json={"worker": "collector@box", "token": "wrongtoken"},
        )
    ).json() == {"ok": False}
    stale = await env.call(
        "collector",
        "POST",
        f"/api/v1/ingest/jobs/{jid}/finish",
        json={"ok": True, "worker": "other", "token": tok},
    )
    assert stale.json() == {"ok": False, "ignored": True}
    assert (
        await env.call("collector", "POST", f"/api/v1/ingest/jobs/{jid}/finish", json={"ok": True})
    ).status_code == 422
    done = await env.call(
        "collector",
        "POST",
        f"/api/v1/ingest/jobs/{jid}/finish",
        json={"ok": True, "result": '{"M30": 220}', "worker": "collector@box", "token": tok},
    )
    assert done.json() == {"ok": True, "ignored": False}
    j = await env.sql(
        "select state, ok, result, done_at from aureon.jobs where id=:i", {"i": jid}, one=True
    )
    assert j["state"] == "done" and j["ok"] is True and j["done_at"] is not None
    again = await env.call(
        "collector",
        "POST",
        f"/api/v1/ingest/jobs/{jid}/finish",
        json={"ok": False, "worker": "collector@box", "token": tok},
    )
    assert again.json()["ignored"] is True  # a finished job cannot be rewritten


async def test_job_failure_retries_expiry_reclaims_and_dead_after_three(env: Env) -> None:
    jid = await _job(env, "mt5_history")
    c1 = (
        await env.call("collector", "POST", "/api/v1/ingest/jobs/claim", json={"worker": "w1"})
    ).json()
    r = await env.call(
        "collector",
        "POST",
        f"/api/v1/ingest/jobs/{jid}/finish",
        json={
            "ok": False,
            "result": "RuntimeError: MT5 init failed",
            "worker": "w1",
            "token": c1["lease_token"],
        },
    )
    assert r.json()["ok"] is True
    j = await env.sql(
        "select state, done_at, attempts from aureon.jobs where id=:i", {"i": jid}, one=True
    )
    assert j["state"] == "pending" and j["done_at"] is None and j["attempts"] == 1  # retried later
    c2 = (
        await env.call("collector", "POST", "/api/v1/ingest/jobs/claim", json={"worker": "w2"})
    ).json()
    assert c2["id"] == jid and c2["attempts"] == 2 and c2["lease_token"] != c1["lease_token"]
    await env.sql(
        "update aureon.jobs set lease_until=now()-interval '1 second' where id=:i", {"i": jid}
    )  # w2 went offline
    late = await env.call(
        "collector",
        "POST",
        f"/api/v1/ingest/jobs/{jid}/finish",
        json={"ok": True, "worker": "w2", "token": c2["lease_token"]},
    )
    assert late.json()["ignored"] is True  # expired lease: fenced
    c3 = (
        await env.call("collector", "POST", "/api/v1/ingest/jobs/claim", json={"worker": "w3"})
    ).json()
    assert c3["id"] == jid and c3["attempts"] == 3
    old = await env.call(
        "collector",
        "POST",
        f"/api/v1/ingest/jobs/{jid}/finish",
        json={"ok": True, "worker": "w2", "token": c2["lease_token"]},
    )
    assert old.json()["ignored"] is True  # the earlier holder cannot finish the re-claimed job
    await env.sql(
        "update aureon.jobs set lease_until=now()-interval '1 second' where id=:i", {"i": jid}
    )
    assert (
        await env.call("collector", "POST", "/api/v1/ingest/jobs/claim", json={"worker": "w4"})
    ).json() is None
    j = await env.sql("select state, ok, result from aureon.jobs where id=:i", {"i": jid}, one=True)
    assert j["state"] == "dead" and j["ok"] is False and "attempts exhausted" in j["result"]


async def test_concurrent_claims_never_share_a_job(env: Env) -> None:
    ids = [await _job(env, "backfill", {"n": i}) for i in range(5)]
    rs = await asyncio.gather(
        *(
            env.call("collector", "POST", "/api/v1/ingest/jobs/claim", json={"worker": f"w{i}"})
            for i in range(10)
        )
    )
    got = [r.json()["id"] for r in rs if r.json()]
    assert sorted(got) == sorted(ids) and len(set(got)) == 5


# ---------------- deals ----------------
def deal_row(
    ticket: int,
    pid: int,
    typ: int,
    entry: int,
    t: dt.datetime,
    price: float = 4000.0,
    profit: float = 0.0,
    comment: str = "",
) -> dict[str, Any]:
    return dict(account_id="777", deal_ticket=ticket, order_ticket=ticket, position_id=pid, symbol="XAUUSD", type=typ, entry=entry,
                time_utc=t.replace(tzinfo=dt.UTC).isoformat(), volume=0.1, price=price, profit=profit, commission=-0.1, swap=0.0, fee=0.0, comment=comment, magic=0)  # fmt: skip


async def test_mt5_deals_idempotent(env: Env) -> None:
    t = MON + dt.timedelta(hours=10)
    deals = [
        deal_row(1, 100, 0, 0, t),
        deal_row(2, 100, 1, 1, t + dt.timedelta(hours=1), 4010.0, 10.0),
        deal_row(3, 200, 0, 0, t),
    ]
    r1 = await env.call("collector", "POST", "/api/v1/ingest/mt5_deals", json={"deals": deals})
    assert r1.status_code == 201 and r1.json() == {"n": 3, "inserted": 3}
    r2 = await env.call("collector", "POST", "/api/v1/ingest/mt5_deals", json={"deals": deals})
    assert r2.json() == {"n": 3, "inserted": 0}
    other_broker = {**deals[0], "server": "Other-Live"}  # same login + ticket on another server
    assert (
        await env.call(
            "collector", "POST", "/api/v1/ingest/mt5_deals", json={"deals": [other_broker]}
        )
    ).json()["inserted"] == 1
    assert len(await env.sql("select 1 from aureon.mt5_deals")) == 4
    for bad in (
        {**deals[0], "time_utc": "x"},
        {k: v for k, v in deals[0].items() if k != "deal_ticket"},
        {**deals[0], "account_id": ""},
    ):
        assert (
            await env.call("collector", "POST", "/api/v1/ingest/mt5_deals", json={"deals": [bad]})
        ).status_code == 422
    assert (
        await env.call(
            "collector", "POST", "/api/v1/ingest/mt5_deals", json={"deals": [deals[0]] * 201}
        )
    ).status_code == 422


# ---------------- service routes ----------------
async def test_heartbeat_state_control_and_alerts(env: Env) -> None:
    for st in (
        "LIVE",
        "PAUSED",
        "RESUMING",
        "DEGRADED",
        "FAILED",
    ):  # the collector echoes the shared state
        assert (
            await env.call(
                "collector",
                "POST",
                "/api/v1/ingest/heartbeat",
                json={"service": "collector", "state": st},
            )
        ).status_code == 201
    assert (
        await env.call(
            "guard",
            "POST",
            "/api/v1/ingest/heartbeat",
            json={"service": "scheduler", "state": "LIVE"},
        )
    ).status_code == 422
    assert (
        await env.call(
            "guard",
            "POST",
            "/api/v1/ingest/heartbeat",
            json={"service": "guard", "state": "LIVE", "note": "x" * 201},
        )
    ).status_code == 422
    assert (await env.call("service", "GET", "/api/v1/ingest/state")).json() == {"state": "PAUSED"}
    assert (
        await env.call("guard", "GET", "/api/v1/ingest/control", params={"key": "desired"})
    ).status_code == 422  # operator intent
    assert (
        await env.call(
            "guard", "POST", "/api/v1/ingest/control", json={"key": "desired", "value": "PAUSE"}
        )
    ).status_code == 422
    assert (
        await env.call(
            "guard",
            "POST",
            "/api/v1/ingest/control",
            json={"key": "lockout_until", "value": "2026-11-08"},
        )
    ).status_code == 201
    assert (
        await env.call("guard", "GET", "/api/v1/ingest/control", params={"key": "lockout_until"})
    ).json() == {"key": "lockout_until", "value": "2026-11-08"}
    assert (
        await env.call(
            "guard", "GET", "/api/v1/ingest/control", params={"key": "day_start:2026-11-02"}
        )
    ).json()["value"] is None
    for _ in range(2):
        assert (
            await env.call(
                "guard",
                "POST",
                "/api/v1/ingest/alert",
                json={"alert_key": "risk:x", "kind": "risk", "payload": "🛑 locked"},
            )
        ).status_code == 201
    assert len(await env.sql("select 1 from aureon.alerts")) == 1  # idempotent by key
    assert (
        await env.call(
            "guard",
            "POST",
            "/api/v1/ingest/alert",
            json={"alert_key": "k2", "kind": "adhoc", "payload": "x" * 1901},
        )
    ).status_code == 422


# ---------------- reads + decisions ----------------
async def test_read_contracts_and_decisions(env: Env) -> None:
    st = (await env.call("reader", "GET", "/api/v1/status")).json()
    assert (
        set(st) == {"state", "rules_version", "heartbeats", "open_trades", "risk", "last_bar"}
        and st["rules_version"] == "R3.0"
    )
    assert (
        await env.call("reader", "GET", "/api/v1/signals", params={"week": "2026-13-40"})
    ).status_code == 422
    assert (
        await env.call("reader", "GET", "/api/v1/signals", params={"limit": 5000})
    ).status_code == 422
    assert (
        await env.call("reader", "GET", "/api/v1/trades", params={"offset": -1})
    ).status_code == 422
    for params, code in (({"tf": "M5", "from": "2026-11-01T00:00", "to": "2026-11-02T00:00"}, 400), ({"tf": "M30", "from": "nope", "to": "2026-11-02T00:00"}, 422),
                         ({"tf": "M30", "from": "2026-11-02T00:00", "to": "2026-11-01T00:00"}, 422), ({"tf": "M30", "from": "2026-01-01T00:00", "to": "2026-11-01T00:00"}, 400)):  # fmt: skip
        assert (
            await env.call("reader", "GET", "/api/v1/chart", params=params)
        ).status_code == code, params
    assert (await env.call("reader", "GET", "/api/v1/signals/999999")).status_code == 404
    assert (await env.call("reader", "GET", "/api/v1/review/latest")).json() == {}
    assert (
        await env.call("reader", "GET", "/api/v1/review", params={"limit": 105})
    ).status_code == 422
    sid = await live_signal(env)
    week = (
        dt.datetime.now(dt.UTC).date() - dt.timedelta(days=dt.datetime.now(dt.UTC).weekday())
    ).isoformat()
    listed = (await env.call("reader", "GET", "/api/v1/signals", params={"week": week})).json()
    assert [s["id"] for s in listed] == [sid] and listed[0]["tag"] is None
    assert (
        await env.call("writer", "POST", "/api/v1/decisions", json={"signal_id": sid, "tag": "buy"})
    ).status_code == 422
    assert (
        await env.call(
            "writer", "POST", "/api/v1/decisions", json={"signal_id": sid, "tag": "take"}
        )
    ).status_code == 201
    assert (
        await env.call(
            "writer", "POST", "/api/v1/decisions", json={"signal_id": sid, "tag": "take"}
        )
    ).json() == {"ok": True, "unchanged": True}
    assert (
        await env.call(
            "writer",
            "POST",
            "/api/v1/decisions",
            json={"signal_id": sid, "tag": "skip", "note": "news"},
        )
    ).status_code == 201
    assert (await env.call("reader", "GET", "/api/v1/signals", params={"week": week})).json()[0][
        "tag"
    ] == "skip"
    old = await live_signal(env, minutes_ago=180)
    assert (
        await env.call(
            "writer", "POST", "/api/v1/decisions", json={"signal_id": old, "tag": "take"}
        )
    ).status_code == 409
    assert (
        await env.call(
            "writer", "POST", "/api/v1/decisions", json={"signal_id": 999999, "tag": "take"}
        )
    ).status_code == 404
    rec = (await env.call("guard", "GET", "/api/v1/ingest/recent_signal", params={"dir": 1})).json()
    assert rec["id"] == sid and rec["suggested_sl"] == 15
    assert (
        await env.call("guard", "GET", "/api/v1/ingest/recent_signal", params={"dir": 2})
    ).status_code == 422


# ---------------- audit, rate limit, body size ----------------
async def test_api_audit_records_writes_and_denials(env: Env) -> None:
    await env.call(
        "guard", "POST", "/api/v1/ingest/heartbeat", json={"service": "guard", "state": "LIVE"}
    )
    await env.call(None, "GET", "/api/v1/status")
    await env.call("collector", "GET", "/api/v1/status")
    await env.call("reader", "GET", "/api/v1/status")
    rows = await env.sql("select role, method, path, status from aureon.api_audit order by id")
    assert {
        "role": "guard",
        "method": "POST",
        "path": "/api/v1/ingest/heartbeat",
        "status": 201,
    } in rows
    assert {"role": None, "method": "GET", "path": "/api/v1/status", "status": 401} in rows
    assert {"role": "collector", "method": "GET", "path": "/api/v1/status", "status": 403} in rows
    assert not [r for r in rows if r["status"] == 200]  # successful reads are not audited (V1.6.3)
    dump = json.dumps(await env.sql("select * from aureon.api_audit"), default=str)
    assert not any(t in dump for t in TOK.values())  # never a token


@pytest.mark.parametrize(
    "overrides", [{"AUREON_API_RATE_PER_MINUTE": 5, "AUREON_API_MAX_BODY_BYTES": 2048}]
)
async def test_rate_limit_and_body_size(env: Env) -> None:
    for _ in range(5):
        assert (await env.call("reader", "GET", "/api/v1/status")).status_code == 200
    assert (await env.call("reader", "GET", "/api/v1/status")).status_code == 429
    assert (await env.call("writer", "GET", "/api/v1/status")).status_code == 200  # per role
    for _ in range(30):
        await env.c.get("/api/v1/status", headers={"authorization": "Bearer guess"})
    assert (
        await env.c.get("/api/v1/status", headers={"authorization": "Bearer guess"})
    ).status_code == 429
    big = {"tf": "M30", "rows": [["2026-11-02T01:00:00", 4000, 4010, 3995, 4005, 100, 5]] * 100}
    assert (await env.call("collector", "POST", "/api/v1/ingest/bars", json=big)).status_code == 413


# ---------------- scheduler ----------------
async def _beat(env: Env, svc: str, age_s: int = 0) -> None:
    await env.sql(
        """insert into aureon.service_heartbeats(service,state,last_beat,note) values(:s,'LIVE',now()-make_interval(secs => cast(:a as double precision)),'')
           on conflict(service) do update set last_beat=excluded.last_beat""",
        {"s": svc, "a": age_s},
    )


async def test_scheduler_collector_offline_reconnect_and_backfill_ack(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def always(s: Any, t: Any) -> bool:
        return True

    monkeypatch.setattr(scheduler, "in_session", always)
    now = dt.datetime.now(dt.UTC)
    out = await scheduler.tick(env.maker, now)
    assert out["state"] == "RESUMING" and "not heartbeating" in out["resume"]  # collector offline
    await _beat(env, "collector")
    out = await scheduler.tick(env.maker, now)
    assert "backfill requested" in out["resume"]
    job = await env.sql("select * from aureon.jobs", one=True)
    assert job["kind"] == "backfill" and job["payload"] == {"n": 2000}
    out = await scheduler.tick(env.maker, now)
    assert (
        "waiting" in out["resume"] and out["state"] == "RESUMING"
    )  # elapsed time alone never resumes
    c = (
        await env.call("collector", "POST", "/api/v1/ingest/jobs/claim", json={"worker": "w"})
    ).json()
    end = now.replace(tzinfo=None)
    rows = bar_rows(
        session_times(end - dt.timedelta(days=4), end - dt.timedelta(minutes=60), "M30")
    )
    await post_bars(env, "M30", rows)
    await env.call(
        "collector",
        "POST",
        f"/api/v1/ingest/jobs/{c['id']}/finish",
        json={"ok": True, "worker": "w", "token": c["lease_token"]},
    )
    out = await scheduler.tick(env.maker, now)
    assert out["resume"] is None and out["state"] == "LIVE"
    assert (await env.call("service", "GET", "/api/v1/ingest/state")).json() == {"state": "LIVE"}
    keys = [r["alert_key"] for r in await env.sql("select alert_key from aureon.alerts")]
    assert any(k.startswith("live:") for k in keys)
    await _beat(env, "collector", age_s=600)  # collector stops
    out = await scheduler.tick(env.maker, now)
    assert out["state"] == "DEGRADED"
    await _beat(env, "collector")
    out = await scheduler.tick(env.maker, now)
    assert out["state"] == "RESUMING"  # back: re-backfill what was missed, then LIVE


async def test_scheduler_pause_and_gap_backfill(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    async def always(s: Any, t: Any) -> bool:
        return True

    monkeypatch.setattr(scheduler, "in_session", always)
    now = dt.datetime.now(dt.UTC)
    await env.sql("update aureon.control set value='PAUSE' where key='desired'")
    assert (await scheduler.tick(env.maker, now))["state"] == "PAUSED"
    await env.sql("update aureon.control set value='RUN' where key='desired'")
    for svc in ("collector", "guard", "detector", "alerter"):
        await _beat(env, svc)
    await env.sql(
        "insert into aureon.service_heartbeats(service,state,last_beat) values('scheduler','LIVE',now()) on conflict(service) do update set state='LIVE'"
    )
    end = now.replace(tzinfo=None)
    times = session_times(end - dt.timedelta(days=4), end - dt.timedelta(minutes=60), "M30")
    hole = times[len(times) // 2 : len(times) // 2 + 3]
    await post_bars(
        env, "M30", [r for r, t in zip(bar_rows(times), times, strict=True) if t not in hole]
    )
    out = await scheduler.tick(env.maker, now)
    assert out["state"] == "LIVE" and "backfill_range job" in (out["gapfill"] or "")
    job = await env.sql("select kind, payload from aureon.jobs", one=True)
    assert job["kind"] == "backfill_range" and job["payload"]["start"] == hole[0].isoformat()
    out = await scheduler.tick(env.maker, now)
    assert (
        "pending" in out["gapfill"] and len(await env.sql("select 1 from aureon.jobs")) == 1
    )  # not doubled


# ---------------- detector ----------------
async def test_detector_live_once_feeds_the_guard(env: Env) -> None:
    end = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(minutes=60)
    times = session_times(end - dt.timedelta(days=21), end, "M30")
    await post_bars(env, "M30", bar_rows(times))
    async with env.maker() as s, s.begin():
        assert await detector.live_once(s) == []  # first run: full replay (source=replay)
    n_ind = len(await env.sql("select 1 from aureon.indicators"))
    assert n_ind == len(times)
    lb = (await env.call("guard", "GET", "/api/v1/ingest/last_bar")).json()
    assert (
        lb["atr14"] > 0
        and lb["vwap_side"] in (-1, 0, 1)
        and lb["time_utc"].startswith(times[-1].isoformat()[:16])
    )
    chart = (
        await env.call(
            "reader",
            "GET",
            "/api/v1/chart",
            params={"tf": "M30", "from": times[-10].isoformat(), "to": times[-1].isoformat()},
        )
    ).json()
    assert len(chart["bars"]) == 10 and chart["bars"][0]["ema8"] is not None
    async with env.maker() as s, s.begin():
        assert await detector.live_once(s) == []  # nothing new: idempotent
        bad = await detector.verify_last_days(s, 7)
    assert bad == []


# ---------------- weekend review ----------------
async def _week_bars(env: Env, hole: tuple[dt.datetime, dt.datetime] | None = None) -> None:
    start, end = MON - dt.timedelta(days=14), MON + dt.timedelta(days=5)
    m30 = session_times(start, end, "M30")
    h1 = session_times(start, end, "H1")
    keep = (lambda t: not (hole[0] <= t < hole[1])) if hole else (lambda t: True)
    await post_bars(env, "M30", [r for r, t in zip(bar_rows(m30), m30, strict=True) if keep(t)])
    await post_bars(env, "H1", bar_rows(h1, seed=5))


async def _serve_history(env: Env, deals: list[dict[str, Any]], ok: bool = True) -> None:
    c = (
        await env.call(
            "collector", "POST", "/api/v1/ingest/jobs/claim", json={"worker": "collector@box"}
        )
    ).json()
    assert c["kind"] == "mt5_history"
    if deals:
        await env.call("collector", "POST", "/api/v1/ingest/mt5_deals", json={"deals": deals})
    await env.call(
        "collector",
        "POST",
        f"/api/v1/ingest/jobs/{c['id']}/finish",
        json={"ok": ok, "result": "x", "worker": "collector@box", "token": c["lease_token"]},
    )


async def test_weekend_review_end_to_end_and_dedup(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "EMA_WARMUP_BARS", 200)
    await env.sql(
        "insert into aureon.risk_state(broker_day,account_id,state) values('2026-11-06','777','UNLOCKED')"
    )
    await _week_bars(env)
    r1 = await weekend_review.step(env.maker, week_start="2026-11-02")
    assert (
        r1["phase"] == "wait_history" and r1["state"] == "RUNNING"
    )  # coverage complete: straight to history
    async with env.maker() as s:
        dets = await weekend_review.replay_week(s, MON, MON + dt.timedelta(days=7))
    q = [d for d in dets if d["category"] == "qualified"]
    first = q[0]
    t0 = first["eligible_time"]
    buy = first["dir"] > 0
    deals = [
        deal_row(
            1,
            100,
            0 if buy else 1,
            0,
            t0 + dt.timedelta(minutes=12),
            first["price"] + 0.5,
            comment="aureon:r1",
        ),
        deal_row(
            2, 100, 1 if buy else 0, 1, t0 + dt.timedelta(hours=2), first["price"] + 8, profit=8.0
        ),
        deal_row(3, 200, 0, 0, MON + dt.timedelta(days=3, hours=15), 4100.0, comment="manual"),
        deal_row(4, 200, 1, 1, MON + dt.timedelta(days=3, hours=16), 4090.0, profit=-10.0),
    ]
    await _serve_history(env, deals)
    r2 = await weekend_review.step(env.maker, week_start="2026-11-02")
    assert r2["state"] == "COMPLETED", r2
    s = r2["summary"]
    cc = s["classification_counts"]
    assert (
        cc.get("DETECTED_AND_TRADED", 0) == 1
        and cc.get("TRADED_WITHOUT_MATCHING_DETECTION", 0) == 1
    )
    assert s["actual"]["trades"] == 2 and abs(s["actual"]["net_pnl"] - (-2.0 - 0.4)) < 1e-9
    assert (
        s["replay"]["qualified"] == len(q)
        and s["actual"]["net_pnl"] != s["replay"]["hypo_pnl_per_oz"]
    )
    rid = r2["report_id"]
    files = (await env.call("reader", "GET", f"/api/v1/review/reports/{rid}")).json()
    assert files["file_names"] == [
        "actual_trades.csv",
        "comparison.csv",
        "detections.csv",
        "summary.html",
        "summary.json",
    ]
    html_ = await env.call("reader", "GET", f"/api/v1/review/reports/{rid}/files/summary.html")
    assert (
        html_.status_code == 200
        and html_.headers["content-type"].startswith("text/html")
        and "No orders were placed" in html_.text
    )
    assert "attachment" in html_.headers["content-disposition"]
    assert (
        await env.call("reader", "GET", f"/api/v1/review/reports/{rid}/files/nope.csv")
    ).status_code == 404
    n_det = len(
        await env.sql("select 1 from aureon.replay_detections where report_id=:r", {"r": rid})
    )
    assert n_det == len(dets)
    alerts = await env.sql("select alert_key, payload from aureon.alerts where kind='report'")
    assert (
        alerts
        and "Weekly review 2026-W45" in alerts[-1]["payload"]
        and "Hypothetical" in alerts[-1]["payload"]
    )
    assert (await weekend_review.step(env.maker, week_start="2026-11-02"))[
        "skipped"
    ] == "already completed"
    r3 = await weekend_review.step(env.maker, week_start="2026-11-02", force=True)
    assert r3["phase"] == "wait_history"
    await _serve_history(env, [])
    r4 = await weekend_review.step(env.maker, week_start="2026-11-02")
    assert r4["state"] == "COMPLETED"
    row = await env.sql(
        "select attempts from aureon.weekly_reports where id=:r", {"r": rid}, one=True
    )
    assert row["attempts"] == 2
    assert (
        len(await env.sql("select 1 from aureon.replay_detections where report_id=:r", {"r": rid}))
        == n_det
    )  # no duplication
    assert (
        len(await env.sql("select 1 from aureon.mt5_deals")) == 4
    )  # re-fetched deals deduplicated


async def test_weekend_review_missing_candles_and_history_failure_mark_incomplete(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "EMA_WARMUP_BARS", 200)
    await _week_bars(env, hole=(dt.datetime(2026, 11, 3, 10), dt.datetime(2026, 11, 3, 13)))
    r1 = await weekend_review.step(env.maker, week_start="2026-11-02", account_id="777")
    assert r1["phase"] == "wait_backfill"
    job = await env.sql(
        "select kind, payload from aureon.jobs where id=:i", {"i": r1["job_id"]}, one=True
    )
    assert job["kind"] == "backfill_range" and job["payload"]["tfs"] == ["M30", "H1"]
    assert (await weekend_review.step(env.maker, week_start="2026-11-02", account_id="777"))[
        "phase"
    ] == "wait_backfill"  # waits
    monkeypatch.setattr(config, "REVIEW_JOB_WAIT_SECONDS", 0)  # the collector never answered
    r2 = await weekend_review.step(env.maker, week_start="2026-11-02", account_id="777")
    assert r2["phase"] == "wait_history"
    r3 = await weekend_review.step(env.maker, week_start="2026-11-02", account_id="777")
    assert r3["state"] == "INCOMPLETE" and r3["summary"]["coverage"]["M30"]["missing"] == 6
    assert r3["summary"]["meta"]["history_fetch_ok"] is False
    last = await env.sql(
        "select payload from aureon.alerts where kind='report' order by id desc limit 1", one=True
    )
    assert "data incomplete" in last["payload"]
    # automatic trigger does not restart an INCOMPLETE week; an admin can
    auto = await weekend_review.step(
        env.maker, week_start="2026-11-02", account_id="777", auto=True
    )
    assert "already incomplete" in auto["skipped"]
    r = await env.call(
        "admin",
        "POST",
        "/api/v1/admin/review/run",
        json={"week": "2026-11-02", "account_id": "777"},
    )
    assert r.status_code == 202 and r.json()["state"] == "RUNNING"
    assert (
        await env.call("admin", "POST", "/api/v1/admin/review/run", json={"week": "2026-13-02"})
    ).status_code == 422
    assert (
        await env.call("reader", "POST", "/api/v1/admin/review/run", json={})
    ).status_code == 403
    assert (
        len(await env.sql("select 1 from audit.audit_events where event_type='aureon_review_run'"))
        == 1
    )


async def test_weekend_review_restart_resumes_and_auto_trigger(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "EMA_WARMUP_BARS", 200)
    await _week_bars(env)
    r1 = await weekend_review.step(env.maker, week_start="2026-11-02", account_id="777")
    assert r1["phase"] == "wait_history"
    # "restart": a fresh process only has the row; the scheduler's auto() advances it
    await _serve_history(env, [])
    out = await weekend_review.auto(env.maker, dt.datetime(2026, 11, 4, 10))  # weekday: resume only
    assert [o["state"] for o in out] == ["COMPLETED"]
    row = await env.sql("select attempts, state from aureon.weekly_reports", one=True)
    assert row == {"attempts": 1, "state": "COMPLETED"}  # resumed, not restarted
    sat = dt.datetime(2026, 11, 14, 10)  # Saturday after W46: starts W46 once
    out = await weekend_review.auto(env.maker, sat)
    assert out[-1]["phase"] == "wait_backfill"  # no W46 bars yet: recovery requested
    again = await weekend_review.auto(env.maker, sat)
    assert (
        len(await env.sql("select 1 from aureon.weekly_reports")) == 2
        and again[-1]["state"] == "RUNNING"
    )


async def test_review_failure_marks_failed_and_alerts(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "EMA_WARMUP_BARS", 200)
    await _week_bars(env)
    await weekend_review.step(env.maker, week_start="2026-11-02", account_id="777")
    await _serve_history(env, [])

    async def boom(*a: Any) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(weekend_review, "replay_week", boom)
    with pytest.raises(RuntimeError):
        await weekend_review.step(env.maker, week_start="2026-11-02", account_id="777")
    row = await env.sql("select state, error from aureon.weekly_reports", one=True)
    assert row["state"] == "FAILED" and "db down" in row["error"]
    assert (
        "FAILED"
        in (await env.sql("select payload from aureon.alerts order by id desc limit 1", one=True))[
            "payload"
        ]
    )


# ---------------- alerts delivery ----------------
async def test_alert_delivery_states(env: Env) -> None:
    async with env.maker() as s, s.begin():
        for k in ("a", "b", "c"):
            await alerter.enqueue(s, k, "adhoc", f"text {k}")
    codes = iter([204, 429, 400])
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(next(codes), headers={"Retry-After": "1"})
        )
    )
    st = env.app.state.settings.model_copy(
        update={
            "DISCORD_WEBHOOK_URL": __import__("pydantic").SecretStr("https://discord.test/hook")
        }
    )
    out = await alerter.deliver_pending(env.maker, st, http)
    assert out == {"delivered": 1, "retried": 1, "failed": 1}
    states = {
        r["alert_key"]: r["state"]
        for r in await env.sql("select alert_key, state from aureon.alerts")
    }
    assert states == {"a": "DELIVERED", "b": "PENDING", "c": "FAILED"}
    async with env.maker() as s, s.begin():
        await alerter.enqueue(s, "d", "adhoc", "no webhook")
    out = await alerter.deliver_pending(env.maker, env.app.state.settings, http)
    assert out["delivered"] == 0
    d = await env.sql("select state, attempts from aureon.alerts where alert_key='d'", one=True)
    assert d == {"state": "PENDING", "attempts": 0}  # kept, not burnt, until a webhook exists


async def test_signal_queue_fails_closed_on_risk(env: Env) -> None:
    sid = await live_signal(env)
    async with env.maker() as s, s.begin():
        assert await alerter.queue_signals(s) == 0  # no risk row today -> suppressed
    assert (await env.sql("select sent_at from aureon.signals where id=:i", {"i": sid}, one=True))[
        "sent_at"
    ] is not None
    sid2 = await live_signal(env, minutes_ago=5)
    await env.sql(
        "insert into aureon.risk_state(broker_day,account_id,state) values(:d,'777','UNLOCKED')",
        {"d": dt.date.fromisoformat(alerter.broker_day())},
    )
    async with env.maker() as s, s.begin():
        assert await alerter.queue_signals(s) == 1
    msg = (
        await env.sql(
            "select payload from aureon.alerts where alert_key=:k",
            {"k": f"signal:{sid2}"},
            one=True,
        )
    )["payload"]
    assert f"#{sid2} BUY XAUUSD M30" in msg


async def test_healthz_and_readyz(env: Env) -> None:
    assert (await env.c.get("/api/v1/aureon/healthz")).json() == {"ok": True}
    r = await env.call("reader", "GET", "/api/v1/aureon/readyz")
    assert r.status_code == 503 and "collector stale" in r.json()["detail"]["problems"]
    assert (await env.call(None, "GET", "/api/v1/aureon/readyz")).status_code == 401


# ---------------- V1.6.4 watchdog: GET /api/v1/ingest/health_summary ----------------
V164_KEYS = {
    "state",
    "heartbeats",
    "bars",
    "indicators_m30_latest",
    "signals",
    "risk",
    "control_desired",
}


async def test_health_summary_authorization(env: Env) -> None:
    assert (await env.call(None, "GET", "/api/v1/ingest/health_summary")).status_code == 401
    assert (
        await env.c.get("/api/v1/ingest/health_summary", headers={"authorization": "Bearer nope"})
    ).status_code == 401
    assert (
        await env.c.get(
            "/api/v1/ingest/health_summary", headers={"authorization": f"Basic {TOK['collector']}"}
        )
    ).status_code == 401
    for role in ("reader", "writer", "admin"):
        assert (await env.call(role, "GET", "/api/v1/ingest/health_summary")).status_code == 403, (
            role
        )
    for role in (
        "collector",
        "guard",
        "service",
    ):  # V1.6.4: auth_svc (+ the Assembler's service role)
        assert (await env.call(role, "GET", "/api/v1/ingest/health_summary")).status_code == 200, (
            role
        )


async def test_health_summary_empty_database_is_unknown_not_healthy(env: Env) -> None:
    await env.sql("delete from aureon.control")
    r = await env.call("collector", "GET", "/api/v1/ingest/health_summary")
    assert r.status_code == 200
    d = r.json()
    assert set(d) == V164_KEYS  # exactly V1.6.4's contract
    assert d["state"] is None  # no scheduler row: unknown (V1.6.4's server said "PAUSED")
    assert d["control_desired"] is None  # no control row: unknown (V1.6.4's server said "RUN")
    assert d["heartbeats"] == [] and d["bars"] == [] and d["indicators_m30_latest"] is None
    assert d["signals"] == {"t": None, "n24": 0} and d["risk"] is None


async def test_health_summary_reports_real_work(env: Env) -> None:
    end = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(minutes=60)
    m30 = session_times(end - dt.timedelta(days=2), end, "M30")
    h1 = session_times(end - dt.timedelta(days=2), end, "H1")
    await post_bars(env, "M30", bar_rows(m30))
    await post_bars(env, "M30", bar_rows(m30))  # replayed upload: no duplicates
    await post_bars(env, "H1", bar_rows(h1, seed=4))
    await env.call(
        "collector",
        "POST",
        "/api/v1/ingest/heartbeat",
        json={"service": "collector", "state": "LIVE", "note": "ok"},
    )
    await env.sql(
        "insert into aureon.service_heartbeats(service,state,last_beat) values('scheduler','LIVE',now())"
    )
    await env.sql(
        "insert into aureon.indicators(symbol,tf,time_utc,rules_version,atr14) values('XAUUSD','M30',:t,'R3.0',10)",
        {"t": m30[-1].replace(tzinfo=dt.UTC)},
    )
    await live_signal(env, minutes_ago=20)
    await env.sql(
        "insert into aureon.risk_state(broker_day,account_id,state,ref_source) values('2026-11-09','777','UNLOCKED','day_open')"
    )
    d = (await env.call("service", "GET", "/api/v1/ingest/health_summary")).json()
    assert set(d) == V164_KEYS and d["state"] == "LIVE" and d["control_desired"] == "RUN"
    bars = {b["tf"]: b for b in d["bars"]}
    assert set(bars) == {"M30", "H1"} and set(bars["M30"]) == {"tf", "t", "n24"}
    assert dt.datetime.fromisoformat(bars["M30"]["t"]) == m30[-1].replace(
        tzinfo=dt.UTC
    )  # UTC, offset included
    assert dt.datetime.fromisoformat(bars["H1"]["t"]) == h1[-1].replace(tzinfo=dt.UTC)
    in_24h = len(
        [
            t
            for t in m30
            if t.replace(tzinfo=dt.UTC) > dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
        ]
    )
    assert bars["M30"]["n24"] == in_24h  # counted once despite the replayed upload
    assert dt.datetime.fromisoformat(d["indicators_m30_latest"]) == m30[-1].replace(tzinfo=dt.UTC)
    assert d["signals"]["n24"] == 1 and d["signals"]["t"] is not None
    assert d["risk"] == {"state": "UNLOCKED", "ref_source": "day_open"}
    col = next(h for h in d["heartbeats"] if h["service"] == "collector")
    assert col == {
        "service": "collector",
        "state": "LIVE",
        "last_beat": col["last_beat"],
        "note": "ok",
    }
    diag = (
        await env.call(
            "collector", "GET", "/api/v1/ingest/health_summary", params={"detail": "true"}
        )
    ).json()["diagnostics"]
    assert (
        diag["heartbeat_ages"]["collector"]["stale"] is False
        and diag["heartbeat_ages"]["collector"]["age_s"] < 60
    )
    assert diag["last_ingest"]["bars"] is not None and diag["last_ingest"]["heartbeat"] is not None
    assert diag["last_ingest"]["mt5_deals"] is None and diag["errors"] == []


async def test_health_summary_stale_heartbeat_stopped_scheduler_and_errors(env: Env) -> None:
    await env.sql(
        "insert into aureon.service_heartbeats(service,state,last_beat,note) values('collector','LIVE',now()-interval '10 minutes','')"
    )
    await env.sql(
        "insert into aureon.service_heartbeats(service,state,last_beat) values('scheduler','LIVE',now()-interval '2 hours')"
    )  # stopped
    await env.sql(
        "insert into aureon.service_heartbeats(service,state,last_beat) values('guard',null,null)"
    )  # never beat
    async with env.maker() as s, s.begin():
        jid = await db.request_job(s, "mt5_history", {"start": "a", "end": "b"})
        await db.request_job(s, "backfill", {"n": 1})
        await db.alert_enqueue(s, "k1", "adhoc", "x")
    await env.sql(
        "update aureon.jobs set state='dead', done_at=now(), ok=false, result='RuntimeError: MT5 init failed [dead: attempts exhausted]', attempts=3 where id=:i",
        {"i": jid},
    )
    await env.sql(
        "update aureon.alerts set state='FAILED', last_error='HTTP 400' where alert_key='k1'"
    )
    d = (
        await env.call("guard", "GET", "/api/v1/ingest/health_summary", params={"detail": "true"})
    ).json()
    # the last recorded state is reported as recorded; its age shows it is not current
    assert d["state"] == "LIVE" and d["diagnostics"]["heartbeat_ages"]["scheduler"]["stale"] is True
    assert d["diagnostics"]["heartbeat_ages"]["collector"]["stale"] is True
    assert d["diagnostics"]["heartbeat_ages"]["collector"]["age_s"] > 500
    assert [h["service"] for h in d["heartbeats"]] == [
        "collector",
        "scheduler",
    ]  # never-beat row is not a beat
    jobs = d["diagnostics"]["jobs"]
    assert jobs["pending"] == 1 and jobs["recent_failures"][0]["id"] == jid
    assert {(e["source"], e["state"]) for e in d["diagnostics"]["errors"]} == {
        ("job:mt5_history", "dead"),
        ("alert", "FAILED"),
    }


async def test_health_summary_database_failure_is_503(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*a: Any, **k: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(db, "q", down)
    r = await env.call("collector", "GET", "/api/v1/ingest/health_summary")
    assert r.status_code == 503 and r.json() == {"detail": "database unavailable"}


async def test_empty_last_bar_means_no_detector_output_not_bad_auth(env: Env) -> None:
    """GET last_bar joins bars to M30 indicators: with bars stored but the detector not run
    (AUREON_WORKERS_ENABLED=false) it is null with HTTP 200. health_summary tells the cases apart."""
    r = await env.call("collector", "GET", "/api/v1/ingest/last_bar")
    assert r.status_code == 200 and r.json() is None  # empty database
    end = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(minutes=60)
    await post_bars(env, "M30", bar_rows(session_times(end - dt.timedelta(days=1), end, "M30")))
    r = await env.call("collector", "GET", "/api/v1/ingest/last_bar")
    assert r.status_code == 200 and r.json() is None  # bars, but no indicators yet
    d = (await env.call("collector", "GET", "/api/v1/ingest/health_summary")).json()
    assert [b["tf"] for b in d["bars"]] == ["M30"] and d["indicators_m30_latest"] is None
    async with env.maker() as s, s.begin():
        await detector.live_once(s)  # what the aureon-tick task does
    assert (await env.call("collector", "GET", "/api/v1/ingest/last_bar")).json()[
        "atr14"
    ] is not None
    bad = await env.c.get("/api/v1/ingest/last_bar", headers={"authorization": "Bearer wrong"})
    assert bad.status_code == 401  # a credential problem is a 401, never an empty 200
