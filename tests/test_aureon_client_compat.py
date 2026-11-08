"""The real Aureon V1.6.3 Windows client against the Assembler, over HTTP (docs/34).

`tests/fixtures/aureon_v163/aureon` is the V1.6.3 package's own code, unmodified: its
AssemblerClient (HTTPS rules, no redirects, retries), ApiStore (every request body the box
sends), the collector (bars, `backfill_range` and `mt5_history` jobs), the read-only deal
history adapter, the guard and its risk engine. The Assembler runs in uvicorn on 127.0.0.1
(loopback HTTP is the only plain-HTTP target the V1.6.3 client accepts) with PostgreSQL and
Redis. Only `MetaTrader5` is replaced, by V1.6.3's own test fake (tests/conftest.py of the
package): no real terminal, account or order is involved. What this cannot show: a real MT5
terminal's data, the broker's timestamps, the public HTTPS path and Cloudflare/Tailscale in
front — those are listed as real-infrastructure checks in docs/34.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import importlib
import json
import os
import socket
import sys
import threading
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.main import create_app

pytestmark = pytest.mark.db

CLIENT_DIR = Path(__file__).parent / "fixtures" / "aureon_v163"
TOK = {
    "AUREON_API_COLLECTOR_TOKEN": "co-Ty6Ha1Kr8Vm3Pq9Zs4Bn7Lx2Wc5Jd0Ef",
    "AUREON_API_GUARD_TOKEN": "gu-Mb5Rw2Xn8Kd1Tq7Fz4Hp9Vs6Lc3Jy0Ga",
    "AUREON_API_READ_TOKEN": "rd-Lq3Zp8Tw1Ns6Vb4Hk9Rd2Mf7Gc5Jx0Ya",
}
TABLES = (
    "weekly_report_files, replay_detections, mt5_deals, weekly_reports, api_audit, alerts, risk_events, risk_state, "
    "control, jobs, service_heartbeats, trade_events, trades, decisions, signals, indicators, bars"
)


# ---------------- the V1.6.3 package's FakeMT5 (tests/conftest.py), same behaviour ----------------
class FakeMT5:
    TRADE_ACTION_SLTP = 1
    TRADE_ACTION_DEAL = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_REQUOTE = 1
    TRADE_RETCODE_PRICE_CHANGED = 2
    TRADE_RETCODE_CONNECTION = 3
    TRADE_RETCODE_PRICE_OFF = 4
    TRADE_RETCODE_TIMEOUT = 10012
    TRADE_RETCODE_INVALID_STOPS = 10016
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 60

    def __init__(
        self, trade_mode: int = 0, pos: Any = None, deals: Any = None, hedging: bool = True
    ) -> None:
        self.trade_mode = trade_mode
        self.pos = pos
        self.deals = deals or []
        self.sent: list[Any] = []
        self.bid = 4000.0
        self.balance = 1000.0
        self.equity = 1000.0
        self.margin_mode = 2 if hedging else 0
        self.send_hook = None
        self.other: list[Any] = []
        self.rates: dict[int, list[dict[str, Any]]] = {}
        self.history: list[Any] = []
        self.fail_init = False

    def initialize(self) -> bool:
        return not self.fail_init

    def symbol_select(self, s: Any, e: Any) -> bool:
        return True

    def shutdown(self) -> None:
        pass

    def last_error(self) -> tuple[int, str]:
        return (0, "")

    def account_info(self) -> Any:
        return types.SimpleNamespace(
            trade_mode=self.trade_mode,
            balance=self.balance,
            equity=self.equity,
            login=777,
            margin_mode=self.margin_mode,
            currency="USD",
        )

    def symbol_info(self, s: Any) -> Any:
        return types.SimpleNamespace(
            point=0.01,
            digits=2,
            trade_stops_level=0,
            trade_freeze_level=0,
            filling_mode=2,
            volume_step=0.01,
            volume_min=0.01,
            volume_max=100,
        )

    def symbol_info_tick(self, s: Any) -> Any:
        return types.SimpleNamespace(bid=self.bid, ask=self.bid + 0.3, time=1700000000)

    def positions_get(self, **k: Any) -> list[Any]:
        allp = ([self.pos] if self.pos else []) + self.other
        if "ticket" in k:
            return [p for p in allp if p.ticket == k["ticket"]]
        if "symbol" in k:
            return [p for p in allp if p.symbol == k["symbol"]]
        return allp

    def history_deals_get(self, *a: Any, **k: Any) -> list[Any]:
        return self.history or self.deals

    def order_send(self, req: dict[str, Any]) -> Any:
        self.sent.append(req)
        if self.send_hook:
            return self.send_hook(req)
        if req["action"] == self.TRADE_ACTION_SLTP:
            self.pos.sl = req["sl"]
        else:
            self.pos = None
        return types.SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="", price=4000.0)

    # copy_rates_* for the collector (the package's tests stub these per test)
    def copy_rates_range(self, sym: Any, tf: int, a: Any, b: Any) -> Any:
        return self.rates.get(tf, [])

    def copy_rates_from_pos(self, sym: Any, tf: int, start: int, n: int) -> Any:
        return self.rates.get(tf, [])[-n:]


def pos(sl: float = 0.0, ticket: int = 1, vol: float = 0.1) -> Any:
    return types.SimpleNamespace(
        ticket=ticket,
        symbol="XAUUSD",
        type=0,
        sl=sl,
        tp=0.0,
        volume=vol,
        price_open=4000.0,
        time=0,
        profit=0.0,
        swap=0.0,
    )


# ---------------- the Assembler in uvicorn, the client package ----------------
def free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return int(sk.getsockname()[1])


class Server:
    def __init__(self, settings: Settings, port: int) -> None:
        self.settings, self.port = settings, port
        self.srv: uvicorn.Server | None = None
        self.th: threading.Thread | None = None

    def start(self) -> None:
        app = create_app(self.settings)
        self.srv = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"
            )
        )
        self.th = threading.Thread(target=self.srv.run, daemon=True)
        self.th.start()
        for _ in range(200):
            if self.srv.started:
                return
            time.sleep(0.05)
        raise RuntimeError("uvicorn did not start")

    def stop(self) -> None:
        assert self.srv and self.th
        self.srv.should_exit = True
        self.th.join(10)


def sql(url: str, q: str, p: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        eng = create_async_engine(url)
        try:
            async with eng.begin() as c:
                r = await c.execute(text(q), p or {})
                return [dict(x) for x in r.mappings().all()] if r.returns_rows else []
        finally:
            await eng.dispose()

    return asyncio.run(run())


@pytest.fixture
def stack(
    db_clean: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    sql(
        db_clean,
        f"TRUNCATE {', '.join('aureon.' + t.strip() for t in TABLES.split(','))} RESTART IDENTITY CASCADE",
    )
    sql(
        db_clean,
        "insert into aureon.control(key,value,updated_by) values('desired','RUN','init') on conflict do nothing",
    )
    port = free_port()
    s = Settings.model_validate({**settings.model_dump(), "DATABASE_URL": db_clean, **TOK})
    srv = Server(s, port)
    srv.start()
    # the Windows box's environment (.env.windows.example), loopback URL
    env = {
        "STORE_MODE": "api", "ASSEMBLER_API_URL": f"http://127.0.0.1:{port}", "ASSEMBLER_API_TIMEOUT": "5",
        "ASSEMBLER_API_COLLECTOR_TOKEN": TOK["AUREON_API_COLLECTOR_TOKEN"], "ASSEMBLER_API_GUARD_TOKEN": TOK["AUREON_API_GUARD_TOKEN"],
        "MT5_TIME_POLICY": "utc", "SYMBOL": "XAUUSD", "MT5_PAPER": "true", "AUREON_SERVICE": "collector",
    }  # fmt: skip
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.syspath_prepend(str(CLIENT_DIR))
    fake = FakeMT5(pos=None)
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    for m in [m for m in sys.modules if m == "aureon" or m.startswith("aureon.")]:
        monkeypatch.delitem(sys.modules, m)
    pkg = {
        name: importlib.import_module(f"aureon.{name}")
        for name in ("config", "store", "api_client", "collector", "mt5_history")
    }
    assert pkg["store"].store.__class__.__name__ == "ApiStore"
    yield {"db": db_clean, "srv": srv, "port": port, "mt5": fake, **pkg}
    with contextlib.suppress(Exception):
        srv.stop()
    for m in [m for m in sys.modules if m == "aureon" or m.startswith("aureon.")]:
        sys.modules.pop(m, None)


def request_job(url: str, kind: str, payload: dict[str, Any]) -> int:
    return int(
        sql(
            url,
            "insert into aureon.jobs(kind,payload) values(:k,cast(:p as jsonb)) returning id",
            {"k": kind, "p": json.dumps(payload)},
        )[0]["id"]
    )


def rates(times: list[dt.datetime], start_px: float = 4000.0) -> list[dict[str, Any]]:
    out = []
    px = start_px
    for k, t in enumerate(times):
        c = px + (1 if k % 3 else -1)
        out.append(
            dict(
                time=int(t.replace(tzinfo=dt.UTC).timestamp()),
                open=px,
                high=max(px, c) + 1,
                low=min(px, c) - 1,
                close=c,
                tick_volume=100 + k,
                spread=5,
            )
        )
        px = c
    return out


def closed_times(n: int, step_min: int) -> list[dt.datetime]:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None, second=0, microsecond=0)
    last = now - dt.timedelta(minutes=now.minute % step_min) - dt.timedelta(minutes=step_min * 2)
    return [last - dt.timedelta(minutes=step_min * i) for i in range(n)][::-1]


# ---------------- the client's own safety rules ----------------
def test_client_refuses_insecure_or_redirected_targets(stack: dict[str, Any]) -> None:
    AC = stack["api_client"].AssemblerClient
    with pytest.raises(ValueError, match="HTTPS"):
        AC("http://spasta.online", "t")  # remote plain HTTP refused by the client
    c = AC(f"http://127.0.0.1:{stack['port']}", TOK["AUREON_API_READ_TOKEN"])
    assert c.get("api/v1/status")["rules_version"] == "R3.0"
    with pytest.raises(stack["api_client"].APIClientError) as e:
        AC(f"http://127.0.0.1:{stack['port']}", "wrong-token").get("api/v1/status")
    assert e.value.status == 401 and not e.value.transient and "wrong-token" not in str(e.value)


# ---------------- collector: bars, heartbeat, jobs, deal history ----------------
def test_collector_uploads_bars_and_heartbeats(stack: dict[str, Any]) -> None:
    col, store = stack["collector"], stack["store"].store
    rows = col._rates_to_rows(rates(closed_times(60, 30)), 30)  # the collector's own validation
    store.upsert_bars(rows, "M30")
    store.upsert_bars(rows, "M30")  # replay: idempotent
    got = sql(stack["db"], "select count(*) n, min(source) s from aureon.bars where tf='M30'")[0]
    assert got == {"n": 60, "s": "live"}
    sql(
        stack["db"],
        "insert into aureon.service_heartbeats(service,state,last_beat) values('scheduler','DEGRADED',now())",
    )
    st = store.get_state()
    assert st == "DEGRADED"
    store.heartbeat("collector", st)  # V1.6.3's server rejected this echo; the Assembler accepts it
    hb = sql(stack["db"], "select state from aureon.service_heartbeats where service='collector'")
    assert hb == [{"state": "DEGRADED"}]


def test_collector_runs_backfill_range_job_end_to_end(stack: dict[str, Any]) -> None:
    col, mt5 = stack["collector"], stack["mt5"]
    m30, h1 = closed_times(40, 30), closed_times(20, 60)
    mt5.rates = {mt5.TIMEFRAME_M30: rates(m30), mt5.TIMEFRAME_H1: rates(h1)}
    jid = request_job(
        stack["db"],
        "backfill_range",
        {"start": m30[0].isoformat(), "end": m30[-1].isoformat(), "tfs": ["M30", "H1"]},
    )
    col.run_jobs()  # claim over HTTP, fetch from "MT5", upload with source=backfill, finish
    j = sql(
        stack["db"], "select state, ok, result, claimed_by from aureon.jobs where id=:i", {"i": jid}
    )[0]
    assert (
        j["state"] == "done"
        and j["ok"] is True
        and json.loads(j["result"]) == {"M30": 40, "H1": 20}
    )
    assert j["claimed_by"].startswith("collector@")
    n = sql(
        stack["db"],
        "select tf, count(*) n from aureon.bars where source='backfill' group by tf order by tf",
    )
    assert n == [{"tf": "H1", "n": 20}, {"tf": "M30", "n": 40}]
    col.run_jobs()  # nothing left: a no-op
    assert len(sql(stack["db"], "select 1 from aureon.jobs")) == 1


def test_collector_mt5_history_job_uploads_deals_idempotently(stack: dict[str, Any]) -> None:
    col, mt5 = stack["collector"], stack["mt5"]
    t0 = int(dt.datetime(2026, 11, 3, 10, tzinfo=dt.UTC).timestamp())

    def d(
        ticket: int,
        pid: int,
        typ: int,
        entry: int,
        t: int,
        price: float,
        profit: float = 0.0,
        comment: str = "",
    ) -> Any:
        return types.SimpleNamespace(ticket=ticket, order=ticket, position_id=pid, symbol="XAUUSD", type=typ, entry=entry, time=t, volume=0.1,
                                     price=price, profit=profit, commission=-0.1, swap=0.0, fee=0.0, comment=comment, magic=0)  # fmt: skip

    mt5.history = [
        d(1, 100, 0, 0, t0, 4000.0, comment="aureon:r1"),
        d(2, 100, 1, 1, t0 + 3600, 4010.0, 10.0),
        d(3, 200, 0, 0, t0 + 7200, 4020.0),
    ]
    payload = {"start": "2026-11-02T00:00:00", "end": "2026-11-09T00:00:00"}
    j1 = request_job(stack["db"], "mt5_history", payload)
    col.run_jobs()
    j2 = request_job(stack["db"], "mt5_history", payload)
    col.run_jobs()  # the same week again: no duplicate deals
    jobs = sql(stack["db"], "select id, state, result from aureon.jobs order by id")
    assert [(r["id"], r["state"], json.loads(r["result"])) for r in jobs] == [
        (j1, "done", {"deals": 3}),
        (j2, "done", {"deals": 3}),
    ]
    deals = sql(
        stack["db"],
        "select account_id, deal_ticket, position_id, time_utc, comment from aureon.mt5_deals order by deal_ticket",
    )
    assert (
        [r["deal_ticket"] for r in deals] == [1, 2, 3]
        and deals[0]["account_id"] == "777"
        and deals[0]["comment"] == "aureon:r1"
    )
    assert deals[0]["time_utc"] == dt.datetime(2026, 11, 3, 10, tzinfo=dt.UTC)  # UTC kept


def test_collector_failure_is_reported_and_retried(stack: dict[str, Any]) -> None:
    col, mt5 = stack["collector"], stack["mt5"]
    mt5.fail_init = True  # terminal not running
    jid = request_job(
        stack["db"], "mt5_history", {"start": "2026-11-02T00:00:00", "end": "2026-11-09T00:00:00"}
    )
    col.run_jobs()
    j = sql(stack["db"], "select state, attempts, result from aureon.jobs where id=:i", {"i": jid})[
        0
    ]
    assert (
        j["state"] == "pending" and j["attempts"] == 1 and "MT5 init failed" in j["result"]
    )  # back in the queue
    mt5.fail_init = False
    mt5.history = []
    col.run_jobs()
    assert sql(stack["db"], "select state from aureon.jobs where id=:i", {"i": jid}) == [
        {"state": "done"}
    ]


# ---------------- guard: trades, events, risk, heartbeat ----------------
def load_guard(stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fake: FakeMT5) -> Any:
    monkeypatch.setenv("AUREON_SERVICE", "guard")
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    store_mod = importlib.reload(stack["store"])  # the guard process's own ApiStore (guard token)
    for name in ("orders", "risk", "alerter", "guard"):
        sys.modules.pop(f"aureon.{name}", None)
    g = importlib.import_module("aureon.guard")
    assert g.store is store_mod.store and g.store.c.session.headers["Authorization"].endswith(
        TOK["AUREON_API_GUARD_TOKEN"]
    )
    return g


def test_guard_attaches_position_and_persists_trade_risk_and_heartbeat(
    stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeMT5(pos=pos(sl=3985.0))
    g = load_guard(stack, monkeypatch, fake)
    g.loop_once()
    trades = sql(
        stack["db"], "select id, mt5_ticket, dir, lots, sl, status, signal_id from aureon.trades"
    )
    assert trades == [
        {
            "id": 1,
            "mt5_ticket": 1,
            "dir": 1,
            "lots": 0.1,
            "sl": 3985.0,
            "status": "open",
            "signal_id": None,
        }
    ]
    ev = sql(stack["db"], "select event, note from aureon.trade_events")
    assert ev[0]["event"] == "attach" and "EXTERNAL" in ev[0]["note"]
    risk = sql(stack["db"], "select account_id, state, equity, equity_at from aureon.risk_state")
    assert (
        len(risk) == 1
        and risk[0]["account_id"] == "777"
        and risk[0]["equity"] == 1000.0
        and risk[0]["equity_at"] is not None
    )
    assert [r["event"] for r in sql(stack["db"], "select event from aureon.risk_events")] == [
        "baseline"
    ]
    hb = sql(
        stack["db"], "select state, note from aureon.service_heartbeats where service='guard'"
    )[0]
    assert hb["state"] == "LIVE" and hb["note"].startswith("RISK=")
    g.loop_once()  # second loop: nothing duplicated
    assert len(sql(stack["db"], "select 1 from aureon.trades")) == 1
    assert len(sql(stack["db"], "select 1 from aureon.risk_events")) == 1
    assert not [
        r for r in fake.sent if r["action"] == fake.TRADE_ACTION_DEAL
    ]  # the guard opened nothing


def test_guard_exit_sends_close_and_records_it_through_the_api(
    stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeMT5(pos=pos(sl=3985.0))
    g = load_guard(stack, monkeypatch, fake)
    g.loop_once()
    # an opposite VWAP cross on a closed bar after entry: the guard's own exit rule
    t = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
    t -= dt.timedelta(minutes=t.minute % 30 + 30)
    sql(
        stack["db"],
        "insert into aureon.bars(symbol,tf,time_utc,open,high,low,close,tick_volume,spread) values('XAUUSD','M30',:t,4000,4001,3990,3992,10,5)",
        {"t": t},
    )
    sql(
        stack["db"],
        "insert into aureon.indicators(symbol,tf,time_utc,rules_version,atr14,vwap_side,bar_of_day) values('XAUUSD','M30',:t,'R3.0',10,-1,10)",
        {"t": t},
    )
    sql(stack["db"], "update aureon.trades set entry_time=:e", {"e": t - dt.timedelta(hours=1)})
    fake.deals = [types.SimpleNamespace(entry=1, volume=0.1, price=3992.0, profit=-8.0, commission=-0.1, swap=0.0, time=int(t.timestamp()) + 60,
                                        symbol="XAUUSD", type=1, position_id=1, ticket=2, order=2)]  # fmt: skip
    g.loop_once()
    tr = sql(
        stack["db"], "select status, exit_reason, pnl_usd, close_request_id from aureon.trades"
    )[0]
    events = [
        r["event"] for r in sql(stack["db"], "select event from aureon.trade_events order by id")
    ]
    assert "close_sent" in events  # V1.6.3's server answered 422 to this event
    assert events == ["attach", "close_sent", "exit"]
    assert (
        tr["status"] == "closed"
        and tr["exit_reason"] == "opp_vwap_cross"
        and tr["close_request_id"]
    )
    assert abs(tr["pnl_usd"] - (-8.1)) < 1e-9  # broker evidence: profit -8.0, commission -0.1


def test_guard_cannot_unlock_and_api_outage_then_reconnect(
    stack: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeMT5(pos=pos(sl=3985.0))
    g = load_guard(stack, monkeypatch, fake)
    g.loop_once()
    sql(stack["db"], "update aureon.risk_state set state='LOCKED'")
    eng = g._engine()
    row = eng.load()
    with pytest.raises(stack["api_client"].APIClientError) as e:
        g.store.risk_upsert({**row, "state": "UNLOCKED"})
    assert e.value.status == 409  # only an admin clears a lock
    # the Assembler goes away: every client call fails as a transient error ...
    stack["srv"].stop()
    with pytest.raises(stack["api_client"].APIClientError) as e:
        g.store.open_trades()
    assert e.value.transient
    with pytest.raises(stack["api_client"].APIClientError):
        g.loop_once()  # the guard's loop skips (its supervisor alerts); the broker-held stop stays
    assert fake.pos is not None and fake.pos.sl == 3985.0
    # ... and when it is back on the same address, the same client resumes with no state lost
    stack["srv"] = Server(stack["srv"].settings, stack["port"])
    stack["srv"].start()
    assert [t["mt5_ticket"] for t in g.store.open_trades()] == [1]
    g.loop_once()
    assert len(sql(stack["db"], "select 1 from aureon.trades")) == 1


@pytest.mark.skipif(
    not (CLIENT_DIR / "aureon" / "store.py").exists(), reason="client fixture missing"
)
def test_every_api_store_route_is_served(stack: dict[str, Any]) -> None:
    """Each path ApiStore uses exists on the Assembler with the role the box holds."""
    import re

    src = (CLIENT_DIR / "aureon" / "store.py").read_text()
    paths = sorted(
        set(re.findall(r'"(api/v1/[a-z0-9_/{}]+)', src))
        | {p.split("{")[0] for p in re.findall(r'f"(api/v1/[^"]+)"', src)}
    )
    served = {
        p.lstrip("/")
        for p in create_app(
            Settings.model_validate(
                {
                    **Settings(
                        ENV="test", DATABASE_URL=stack["db"], SECRET_KEY="t" * 40
                    ).model_dump(),
                    **TOK,
                }
            )
        ).openapi()["paths"]
    }
    for p in paths:
        pat = re.sub(r"\{[^}]+\}", "[^/]+", p.rstrip("/"))
        assert any(
            re.fullmatch(pat + "(/.*)?", s.replace("{jid}", "1").replace("{tid}", "1"))
            for s in served
        ), p
    assert len(paths) >= 18
    os.environ.pop("AUREON_SERVICE", None)
