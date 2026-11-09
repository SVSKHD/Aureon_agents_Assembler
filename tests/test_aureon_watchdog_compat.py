"""The real Aureon V1.6.4 Windows watchdog against the Assembler, over HTTP (docs/34).

`tests/fixtures/aureon_v164/aureon` is the V1.6.4 package, unmodified. The watchdog
(`aureon/watchdog.py`) is built with its real `ApiStore`, which calls
GET /api/v1/ingest/health_summary on the Assembler running in uvicorn with PostgreSQL;
`Watchdog.tick()` then parses that answer with V1.6.4's own code. Only the child processes
are faked (subprocess.Popen), as V1.6.4's tests/test_watchdog.py does, and the watchdog's
clock is pinned to a weekday so "market closed" does not depend on when the suite runs.
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from tests.test_aureon_client_compat import TABLES, TOK, Server, free_port, sql

pytestmark = pytest.mark.db

CLIENT_DIR = Path(__file__).parent / "fixtures" / "aureon_v164"
WEEKDAY = dt.datetime(2026, 11, 10, 10, 0, tzinfo=dt.UTC)  # Tuesday, session open


class FakeProc:
    """V1.6.4 tests/test_watchdog.py FakeProc: a child that stays alive."""

    def __init__(self, args: list[str], **k: Any) -> None:
        self.args, self.pid, self.rc = args, 4242, None
        self.stdout = iter([])

    def poll(self) -> int | None:
        return self.rc

    def wait(self, t: float | None = None) -> int | None:
        return self.rc

    def send_signal(self, s: int) -> None:
        self.rc = -2

    def terminate(self) -> None:
        self.rc = -15

    def kill(self) -> None:
        self.rc = -9

    @property
    def returncode(self) -> int | None:
        return self.rc


@pytest.fixture
def wd(
    db_clean: str, settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
    srv = Server(
        Settings.model_validate({**settings.model_dump(), "DATABASE_URL": db_clean, **TOK}), port
    )
    srv.start()
    for k, v in {
        "STORE_MODE": "api", "ASSEMBLER_API_URL": f"http://127.0.0.1:{port}", "ASSEMBLER_API_TIMEOUT": "5",
        "ASSEMBLER_API_COLLECTOR_TOKEN": TOK["AUREON_API_COLLECTOR_TOKEN"], "ASSEMBLER_API_GUARD_TOKEN": TOK["AUREON_API_GUARD_TOKEN"],
        "MT5_TIME_POLICY": "utc", "SYMBOL": "XAUUSD", "MT5_PAPER": "true", "AUREON_SERVICE": "collector",
        "WATCHDOG_LOG_DIR": str(tmp_path / "logs"),
    }.items():  # fmt: skip
        monkeypatch.setenv(k, v)
    monkeypatch.syspath_prepend(str(CLIENT_DIR))
    for m in [m for m in sys.modules if m == "aureon" or m.startswith("aureon.")]:
        monkeypatch.delitem(sys.modules, m)
    W = importlib.import_module("aureon.watchdog")
    store = importlib.import_module("aureon.store")
    monkeypatch.setattr(W.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(W, "now", lambda: WEEKDAY)
    yield {"W": W, "store": store, "db": db_clean, "srv": srv}
    srv.stop()
    for m in [m for m in sys.modules if m == "aureon" or m.startswith("aureon.")]:
        sys.modules.pop(m, None)


def seed(url: str, hb_age_s: float = 5, m30_age_min: float = 10, h1_age_min: float = 30) -> None:
    sql(url, "insert into aureon.service_heartbeats(service,state,last_beat,note) values('collector','LIVE',:t,'') on conflict(service) do update set last_beat=excluded.last_beat",
        {"t": WEEKDAY - dt.timedelta(seconds=hb_age_s)})  # fmt: skip
    sql(url, "insert into aureon.service_heartbeats(service,state,last_beat) values('scheduler','LIVE',:t) on conflict(service) do update set last_beat=excluded.last_beat",
        {"t": WEEKDAY})  # fmt: skip
    for tf, age in (("M30", m30_age_min), ("H1", h1_age_min)):
        sql(url, "insert into aureon.bars(symbol,tf,time_utc,open,high,low,close,tick_volume,spread) values('XAUUSD',:tf,:t,4000,4001,3999,4000,1,1)",
            {"tf": tf, "t": WEEKDAY - dt.timedelta(minutes=age)})  # fmt: skip
    sql(
        url,
        "insert into aureon.indicators(symbol,tf,time_utc,rules_version) values('XAUUSD','M30',:t,'R3.0')",
        {"t": WEEKDAY - dt.timedelta(minutes=m30_age_min)},
    )


def watchdog(env: dict[str, Any]) -> Any:
    w = env["W"].Watchdog(["collector"], store=env["store"].store)  # the real ApiStore
    w.start_all()
    return w


def test_watchdog_gets_200_and_reports_running(wd: dict[str, Any]) -> None:
    seed(wd["db"])
    w = watchdog(wd)
    assert w.tick() == {"collector": "RUNNING"}
    assert w.backend_error == "" and w.backend["state"] == "LIVE"
    s = w.services["collector"]
    assert s.last_ack == "M30 bar 10m · H1 bar 30m · ind 10m"
    assert s.last_hb == WEEKDAY - dt.timedelta(seconds=5)
    dash = w.dashboard()
    assert "scheduler LIVE" in dash and "desired RUN" in dash
    audit = sql(
        wd["db"],
        "select role, status from aureon.api_audit where path='/api/v1/ingest/health_summary'",
    )
    assert audit == []  # successful reads are not audited (V1.6.3 rule), but they were served:
    served = sql(wd["db"], "select count(*) n from aureon.alerts where kind='watchdog'")
    assert served[0]["n"] >= 1  # its own start notification went through the API too


def test_empty_backend_is_never_running(wd: dict[str, Any]) -> None:
    w = watchdog(wd)
    w.services["collector"].started_at = time.time() - 600  # past the 120 s start grace
    assert w.tick() == {"collector": "DEGRADED"}
    assert w.services["collector"].last_ack == "no bars acknowledged yet"
    assert (
        w.backend["state"] is None
        and w.backend["heartbeats"] == []
        and w._ind_age(WEEKDAY) == "n/a"
    )


def test_stale_heartbeat_and_stale_bars_are_degraded(wd: dict[str, Any]) -> None:
    seed(wd["db"], hb_age_s=900)
    w = watchdog(wd)
    w.services["collector"].started_at = time.time() - 600
    assert (
        w.tick() == {"collector": "DEGRADED"}
        and w.services["collector"].last_error == "heartbeat stale"
    )
    sql(wd["db"], "delete from aureon.bars")
    sql(wd["db"], "delete from aureon.service_heartbeats")
    seed(wd["db"], hb_age_s=5, m30_age_min=200, h1_age_min=200)
    w2 = watchdog(wd)
    w2.services["collector"].started_at = time.time() - 600
    assert w2.tick() == {"collector": "DEGRADED"}
    assert "bars not advancing" in w2.services["collector"].last_error


def test_operator_pause_is_honoured(wd: dict[str, Any]) -> None:
    seed(wd["db"])
    sql(wd["db"], "update aureon.control set value='PAUSE' where key='desired'")
    w = watchdog(wd)
    assert w.tick() == {"collector": "PAUSED"}


def test_rejected_credentials_fail_without_restart(
    wd: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(wd["db"])
    monkeypatch.setenv("ASSEMBLER_API_COLLECTOR_TOKEN", "x" * 40)  # wrong token on the box
    store = importlib.reload(wd["store"])
    w = wd["W"].Watchdog(["collector"], store=store.store)
    w.start_all()
    assert w.tick() == {"collector": "FAILED"}
    assert "401" in w.backend_error and w.services["collector"].auth_failed
