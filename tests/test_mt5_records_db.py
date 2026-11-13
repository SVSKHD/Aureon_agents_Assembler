"""MT5 records API against the database (docs/36-mt5-records.md): insert and resend
(duplicates add no row), mixed batches, filters and keyset paging, stats, bars upsert and
correction, retention only when configured, migration 0031 up and down, every route end to
end, and the Claude tool's summary (account login redacted)."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ai.tools import investments
from app.ai.tools.registry import ToolContext
from app.auth.bootstrap import bootstrap_admin
from app.core.config import Settings
from app.main import create_app
from app.models.brokers import Mt5Bar, Mt5Candle, Mt5Record
from app.services import mt5_dataset
from app.services import mt5_records as svc
from tests.test_mt5_dataset_unit import GOLDEN
from tests.test_mt5_records_unit import TOKEN, bar, bars_doc, batch, row

pytestmark = pytest.mark.db
PASSWORD = "correct horse battery staple"
TABLES = ("mt5.records", "mt5.bars")
ROOT = Path(__file__).resolve().parents[1]


class Env:
    def __init__(self, maker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self.maker, self.settings = maker, settings
        self.store = svc.DbRecordsStore(maker)

    async def put(self, rows: list[dict[str, Any]]) -> tuple[int, svc.SplitBatch]:
        split = svc.split_batch(rows, datetime.now(UTC))
        return await self.store.store_records(split.rows, uuid.uuid4()), split

    async def put_bars(self, bars: list[dict[str, Any]], **kw: Any) -> svc.BarsResult:
        good, bad = svc.split_bars(svc.parse_bars_doc(bars_doc(bars, **kw)))
        assert bad == []
        return await self.store.store_bars(good)

    async def count(self, model: Any, *where: Any) -> int:
        async with self.maker() as db:
            return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)


@pytest.fixture
async def env(db_clean: str, settings: Settings) -> AsyncIterator[Env]:
    engine = create_async_engine(db_clean)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def clean() -> None:
        async with engine.begin() as conn:
            for t in TABLES:
                await conn.exec_driver_sql(f"DELETE FROM {t}")

    await clean()
    s = Settings.model_validate(
        {**settings.model_dump(), "MT5_RECORDS_TOKEN": TOKEN, "DATABASE_URL": db_clean}
    )
    yield Env(maker, s)
    await clean()
    await engine.dispose()


def at(minutes: int, **kw: Any) -> dict[str, Any]:
    """A row `minutes` after 2026-11-13 08:00 UTC."""
    t = datetime(2026, 11, 13, 8, 0, tzinfo=UTC) + timedelta(minutes=minutes)
    return row(utc_time=t.isoformat(), **kw)


# --- records ----------------------------------------------------------------------------------


async def test_insert_and_resend_is_a_duplicate(env: Env) -> None:
    rows = [at(1), at(2, kind="alert", channel="signals"), at(3, account_login=None)]
    new, _ = await env.put(rows)
    assert new == 3
    again, _ = await env.put(rows)
    assert again == 0
    assert await env.count(Mt5Record) == 3
    async with env.maker() as db:
        rec = await db.get(Mt5Record, uuid.UUID(rows[1]["record_id"]))
    assert rec is not None
    assert (rec.kind, rec.symbol, rec.account_login, rec.bot_version, rec.bot_id) == (
        "alert",
        "XAUUSD",
        12345678,
        "1.7.1",
        "aureon_mt5_2026",
    )
    assert rec.server_time == datetime(2026, 11, 13, 11, 30, 3) and rec.server_time.tzinfo is None
    assert rec.payload["channel"] == "signals" and rec.payload["label"] == "CROSS"
    assert "record_id" not in rec.payload and "version" not in rec.payload


async def test_mixed_batch_stores_only_the_good_rows(env: Env) -> None:
    good = at(1)
    new, split = await env.put([good, at(2, kind="NOPE"), dict(good), "junk", at(3)])
    assert new == 2 and split.repeated == 1 and len(split.rejected) == 2
    assert await env.count(Mt5Record) == 2


async def test_filters_and_keyset_paging(env: Env) -> None:
    rows = [at(i, kind="event" if i % 2 else "position") for i in range(10)]
    rows += [at(5, symbol="XAGUSD", label="WIDE GAP", account_login=999)]
    tied = [at(20), at(20), at(20)]  # same utc_time: record_id breaks the tie
    await env.put(rows + tied)
    async with env.maker() as db:
        seen: list[uuid.UUID] = []
        cursor = None
        while True:
            page, nxt = await svc.list_records(db, svc.RecordFilter(), 4, cursor)
            seen += [r.record_id for r in page]
            if nxt is None:
                break
            cursor = svc.decode_cursor(nxt)
        assert len(seen) == len(set(seen)) == 14
        everything, _ = await svc.list_records(db, svc.RecordFilter(), 100, None)
        assert [r.record_id for r in everything] == seen
        keys = [(r.utc_time, r.record_id) for r in everything]
        assert keys == sorted(keys, reverse=True)

        async def ids(**kw: Any) -> set[str]:
            page, _ = await svc.list_records(db, svc.RecordFilter(**kw), 100, None)
            return {r.record_id.hex for r in page}

        assert len(await ids(kinds=["position"])) == 5
        assert len(await ids(kinds=["position", "event"])) == 14
        assert await ids(symbol="XAGUSD") == {rows[-1]["record_id"]}
        assert await ids(account_login=999) == {rows[-1]["record_id"]}
        assert await ids(label="WIDE GAP") == {rows[-1]["record_id"]}
        assert len(await ids(label="CROSS")) == 13
        t0 = datetime(2026, 11, 13, 8, 2, tzinfo=UTC)
        t1 = datetime(2026, 11, 13, 8, 4, tzinfo=UTC)
        assert await ids(date_from=t0, date_to=t1) == {r["record_id"] for r in rows[2:5]}
        assert await ids(kinds=["sl_change"]) == set()


async def test_stats(env: Env) -> None:
    now = datetime.now(UTC)
    await env.put(
        [
            row(utc_time=now.isoformat(), kind="event"),
            row(utc_time=(now - timedelta(days=3)).isoformat(), kind="event"),
            row(utc_time=(now - timedelta(days=30)).isoformat(), kind="event", version="1.7.0"),
            row(utc_time=(now - timedelta(minutes=5)).isoformat(), kind="sl_change"),
        ]
    )
    async with env.maker() as db:
        st = await svc.record_stats(db)
    kinds = {k.kind: k for k in st.kinds}
    assert set(kinds) == {"event", "sl_change"} and st.total == 4
    ev = kinds["event"]
    assert ev.days_7 == 2 and ev.total == 3
    assert ev.today in (1, 2)  # 3 days ago is never today; now is (unless midnight passes)
    assert kinds["sl_change"].total == 1
    assert st.newest_utc_time is not None and st.lag_seconds is not None
    assert -5 < st.lag_seconds < 120
    assert st.bot_versions_24h == ["1.7.0", "1.7.1"]  # by receive time, not utc_time


async def test_stats_when_empty(env: Env) -> None:
    async with env.maker() as db:
        st = await svc.record_stats(db)
    assert st.kinds == [] and st.total == 0 and st.lag_seconds is None


async def test_retention_purge_only_when_configured(env: Env) -> None:
    old = datetime.now(UTC) - timedelta(days=40)
    await env.put([row(utc_time=old.isoformat()), row()])
    await env.put_bars([bar(ts="2020-01-06T00:00:00Z")])
    out = await svc.purge(env.maker, env.settings)  # MT5_RECORDS_RETENTION_DAYS=0
    assert out == {"records": 0, "retention_days": 0}
    assert await env.count(Mt5Record) == 2
    s = env.settings.model_copy(update={"MT5_RECORDS_RETENTION_DAYS": 30})
    out = await svc.purge(env.maker, s)
    assert out == {"records": 1, "retention_days": 30}
    assert await env.count(Mt5Record) == 1
    assert await env.count(Mt5Bar) == 1  # bars are never purged


async def test_scheduler_purge_task_runs_the_records_purge(env: Env) -> None:
    from app.workers import scheduler

    old = datetime.now(UTC) - timedelta(days=40)
    await env.put([row(utc_time=old.isoformat())])
    s = env.settings.model_copy(update={"MT5_RECORDS_RETENTION_DAYS": 30})

    class Ctx:
        maker = env.maker
        settings = s

    out = await scheduler.mt5_ingest_purge(Ctx())  # type: ignore[arg-type]
    assert out["mt5_records"] == {"records": 1, "retention_days": 30}
    assert {"candles", "events", "pushes", "failures"} <= set(out)  # push/v1 purge as before
    s0 = env.settings  # retention 0: the records purge is not even called
    Ctx.settings = s0
    assert "mt5_records" not in await scheduler.mt5_ingest_purge(Ctx())  # type: ignore[arg-type]


# --- bars -------------------------------------------------------------------------------------


async def test_bars_upsert_correction_and_unchanged(env: Env) -> None:
    b1, b2 = bar(), bar(ts="2026-11-13T09:30:00Z", close=4036.0)
    r = await env.put_bars([b1, b2])
    assert (r.inserted, r.updated, r.unchanged) == (2, 0, 0)
    r = await env.put_bars([b1, b2])
    assert (r.inserted, r.updated, r.unchanged) == (0, 0, 2)
    r = await env.put_bars(
        [bar(close=4039.9, tick_volume=None), b2, bar(ts="2026-11-13T10:00:00Z")]
    )
    assert (r.inserted, r.updated, r.unchanged) == (1, 1, 1)
    async with env.maker() as db:
        rows = (await db.scalars(select(Mt5Bar).order_by(Mt5Bar.ts))).all()
        assert len(rows) == 3
        assert str(rows[0].close) == "4039.900000" and rows[0].tick_volume is None
        assert rows[0].server_ts == datetime(2026, 11, 13, 11, 0)
        assert str(rows[0].ema200) == "4090.300000" and rows[0].source == "bot"
        # other symbol / timeframe are separate bars
    r = await env.put_bars([b1], timeframe="H1")
    assert r.inserted == 1
    assert await env.count(Mt5Bar) == 4
    assert await env.count(Mt5Candle) == 0  # push/v1's table is untouched


async def test_dataset_from_stored_bars(env: Env) -> None:
    golden = _golden_rows()
    for i in range(0, len(golden), 1000):
        await env.put_bars(golden[i : i + 1000])
    async with env.maker() as db:
        ds = await svc.crosses_dataset(db, "XAUUSD", "M30", None, None)
        assert ds.bars_used == len(golden) and len(ds.rows) == 75
        want = mt5_dataset.cross_rows(
            [
                mt5_dataset.Bar(
                    datetime.fromisoformat(g["ts"]), g["open"], g["high"], g["low"], g["close"]
                )
                for g in golden
            ],
            "XAUUSD",
            "M30",
        )
        assert [r["cross_ts"] for r in ds.rows] == [r["cross_ts"] for r in want]
        # a window: warm-up bars are loaded before `from`, crosses only inside [from, to]
        f = datetime(2026, 10, 1, tzinfo=UTC)
        t = datetime(2026, 10, 20, tzinfo=UTC)
        win = await svc.crosses_dataset(db, "XAUUSD", "M30", f, t)
        assert win.bars_from is not None and win.bars_from < f
        assert win.bars_used <= svc.DATASET_WARMUP_BARS + 20 * 48
        assert win.rows and all(f.isoformat() <= r["cross_ts"] <= t.isoformat() for r in win.rows)
        empty = await svc.crosses_dataset(db, "XAGUSD", "M30", None, None)
        assert empty.rows == [] and empty.bars_used == 0


def _golden_rows() -> list[dict[str, Any]]:
    with GOLDEN.open() as fh:
        return [
            {
                "ts": datetime.fromisoformat(r["time_utc"]).replace(tzinfo=UTC).isoformat(),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            }
            for r in csv.DictReader(fh)
        ]


# --- the API end to end -----------------------------------------------------------------------


async def test_api_end_to_end(env: Env) -> None:
    await bootstrap_admin(env.maker, "admin@spasta.online", PASSWORD)
    app = create_app(env.settings)
    auth = {"authorization": f"Bearer {TOKEN}", "content-type": "application/json"}
    async with (
        LifespanManager(app) as mgr,
        AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c,
    ):
        rows = [at(1), at(2, kind="position", event="opened", result=None), at(3, kind="BAD")]
        r = await c.post("/api/trading/mt5/records", content=json.dumps(batch(rows)), headers=auth)
        assert r.status_code == 200, r.text
        assert r.json()["accepted"] == 2 and len(r.json()["rejected"]) == 1
        r = await c.post("/api/trading/mt5/records", content=json.dumps(batch(rows)), headers=auth)
        assert r.json()["duplicate"] == 2 and r.json()["accepted"] == 0

        golden = _golden_rows()
        for i in range(0, len(golden), 1000):
            doc = bars_doc(golden[i : i + 1000])
            r = await c.post("/api/trading/mt5/bars", content=json.dumps(doc), headers=auth)
            assert r.status_code == 200 and r.json()["inserted"] == len(golden[i : i + 1000])

        # reads need a session
        assert (await c.get("/api/trading/mt5/records")).status_code == 401
        await c.post("/api/auth/login", json={"email": "admin@spasta.online", "password": PASSWORD})

        page = (await c.get("/api/trading/mt5/records", params={"limit": 1})).json()
        assert len(page["rows"]) == 1 and page["next_cursor"]
        assert page["rows"][0]["record_id"] == str(uuid.UUID(rows[1]["record_id"]))
        nxt = await c.get(
            "/api/trading/mt5/records", params={"limit": 1, "cursor": page["next_cursor"]}
        )
        assert nxt.json()["rows"][0]["record_id"] == str(uuid.UUID(rows[0]["record_id"]))
        assert nxt.json()["next_cursor"] is None
        flt = await c.get(
            "/api/trading/mt5/records",
            params=[("kind", "position"), ("kind", "alert"), ("symbol", "XAUUSD")],
        )
        assert [x["kind"] for x in flt.json()["rows"]] == ["position"]
        cross_until = {"label": "CROSS", "to": "2026-11-13T08:01:30Z"}
        lbl = await c.get("/api/trading/mt5/records", params=cross_until)
        assert len(lbl.json()["rows"]) == 1
        bad = await c.get("/api/trading/mt5/records", params={"cursor": "garbage"})
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_cursor"
        bad = await c.get("/api/trading/mt5/records", params={"kind": "Bad-Kind"})
        assert bad.status_code == 422
        bad = await c.get(
            "/api/trading/mt5/records", params={"from": "2026-11-14", "to": "2026-11-13"}
        )
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_range"

        st = (await c.get("/api/trading/mt5/records/stats")).json()
        assert st["total"] == 2 and {k["kind"] for k in st["kinds"]} == {"event", "position"}
        assert st["bot_versions_24h"] == ["1.7.1"]

        bars = await c.get(
            "/api/trading/mt5/bars",
            params={"symbol": "XAUUSD", "timeframe": "m30", "from": "2026-10-29T05:00:00Z",
                    "limit": 3},
        )  # fmt: skip
        body = bars.json()
        assert body["count"] == 3 and body["timeframe"] == "M30"
        assert [b["ts"][:16] for b in body["rows"]] == [
            "2026-10-29T05:00",
            "2026-10-29T05:30",
            "2026-10-29T06:00",
        ]
        assert body["rows"][1]["close"] == "4038.100000"
        csv_r = await c.get(
            "/api/trading/mt5/bars",
            params={"symbol": "XAUUSD", "format": "csv", "from": "2026-10-29T05:30:00Z",
                    "to": "2026-10-29T05:30:00Z"},
        )  # fmt: skip
        assert csv_r.headers["content-type"].startswith("text/csv")
        parsed = list(csv.DictReader(io.StringIO(csv_r.text)))
        assert len(parsed) == 1 and parsed[0]["close"] == "4038.100000"
        assert csv_r.headers["x-aureon-row-count"] == "1"

        ds = (await c.get("/api/trading/mt5/dataset/crosses", params={"symbol": "XAUUSD"})).json()
        assert ds["definitions_version"] == mt5_dataset.DEFINITIONS_VERSION
        assert ds["row_count"] == len(ds["rows"]) == 75 and ds["bars_used"] == len(golden)
        assert ds["feature_columns"] == list(mt5_dataset.FEATURES)
        assert ds["outcome_columns"] == list(mt5_dataset.OUTCOMES)
        assert ds["date_from"].startswith("2026-09-01") and ds["date_to"].startswith("2026-10-30")
        # the 29 Oct 05:30 cross is still open at the end of the data: not in the set
        assert all(not r["cross_ts"].startswith("2026-10-29T05:30") for r in ds["rows"])
        csv_ds = await c.get(
            "/api/trading/mt5/dataset/crosses", params={"symbol": "XAUUSD", "format": "csv"}
        )
        assert csv_ds.headers["x-aureon-row-count"] == "75"
        assert csv_ds.headers["x-aureon-definitions-version"] == mt5_dataset.DEFINITIONS_VERSION
        assert csv_ds.headers["x-aureon-date-from"].startswith("2026-09-01")
        assert csv_ds.text.splitlines()[0] == ",".join(mt5_dataset.COLUMNS)
        jl = await c.get(
            "/api/trading/mt5/dataset/crosses",
            params={"symbol": "XAUUSD", "format": "jsonl", "from": "2026-10-01T00:00:00Z"},
        )
        lines = [json.loads(x) for x in jl.text.splitlines()]
        assert lines and all(x["cross_ts"] >= "2026-10-01" for x in lines)
        assert jl.headers["x-aureon-row-count"] == str(len(lines))

        # session writes are not a thing here: the POSTs still demand the bearer token
        r = await c.post("/api/trading/mt5/records", content=json.dumps(batch([row()])),
                         headers={"content-type": "application/json"})  # fmt: skip
        assert r.status_code == 401


# --- the Claude tool --------------------------------------------------------------------------


async def test_mt5_records_summary_tool(env: Env) -> None:
    now = datetime.now(UTC)

    def r(minutes: int, kind: str = "event", **kw: Any) -> dict[str, Any]:
        return row(utc_time=(now - timedelta(minutes=minutes)).isoformat(), kind=kind, **kw)

    await env.put(
        [
            r(50, label="CROSS", side="SHORT", tag="below 200"),
            r(40, label="CROSS", side="LONG", tag="above 200"),
            r(39, label="CROSS", side="LONG", tag="below 200"),
            r(30, label="GAP WIDENING", side="LONG"),
            r(20, "position", event="opened", ticket=1, result=None, label=None),
            r(10, "position", event="closed", ticket=1, result=25.5, label=None),
            r(9, "position", event="closed", ticket=2, result=-10, label=None),
            r(8, "position", event="closed", ticket=3, result="n/a", label=None),
            r(5, "sl_change", ticket=1, old_sl=1, new_sl=2, label=None),
            r(60 * 24 * 30, label="CROSS", side="SHORT", tag="below 200"),  # outside 7 days
        ]
    )
    ctx = ToolContext(
        settings=env.settings, maker=env.maker, user_id=None, is_admin=True, agent="investments"
    )
    out = await investments.mt5_records_summary(ctx, {"symbol": "xauusd"})
    assert "crosses: 3 (long 2, short 1)" in out
    assert "above 200 1, below 200 2" in out
    assert "CROSS 3" in out and "GAP WIDENING 1" in out
    assert "1 opened, 3 closed, 1 won (33%), total result 15.5" in out
    assert "SL changes: 1" in out
    assert "12345678" not in out and "[ACCOUNT_ID]" in out  # redacted
    assert "no completed cross in the period" in out
    assert len(out) < 2000  # aggregates, never raw tables
    empty = await investments.mt5_records_summary(ctx, {"symbol": "eurusd", "days": 1})
    assert empty == "No MT5 bot records or bars stored for EURUSD yet."
    await env.put([r(3, symbol="XAUUSD.m", label="CROSS", side="LONG", tag="above 200")])
    out = await investments.mt5_records_summary(ctx, {"symbol": "xauusd.M"})
    assert out.startswith("MT5 bot records for XAUUSD.m") and "crosses: 1 (long 1" in out


async def test_summary_reports_the_dataset_share(env: Env) -> None:
    golden = _golden_rows()
    for i in range(0, len(golden), 1000):
        await env.put_bars(golden[i : i + 1000])
    # make the bars recent so the 90-day window covers them
    async with env.maker() as db, db.begin():
        shift = datetime.now(UTC) - datetime(2026, 10, 31, tzinfo=UTC)
        await db.execute(update(Mt5Bar).values(ts=Mt5Bar.ts + shift))
    ctx = ToolContext(
        settings=env.settings, maker=env.maker, user_id=None, is_admin=True, agent="investments"
    )
    out = await investments.mt5_records_summary(ctx, {"symbol": "XAUUSD", "days": 90})
    assert "completed crosses widened" in out and "of 75 completed crosses" in out


# --- migration 0031 up and down ----------------------------------------------------------------


def _psql(url: str, sql: str) -> str:
    r = subprocess.run(["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", url, "-c", sql],
                       capture_output=True, text=True, check=True)  # fmt: skip
    return r.stdout.strip()


def _alembic(url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env={**os.environ, "DATABASE_URL": url, "MIGRATIONS_DATABASE_URL": ""},
        timeout=300,
    )


def test_migration_0031_up_and_down(migrated: str) -> None:
    server = migrated.replace("+asyncpg", "").rsplit("/", 1)[0]
    scratch = f"aureon_m31_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    _psql(f"{server}/postgres", f'CREATE DATABASE "{scratch}"')
    url = migrated.rsplit("/", 1)[0] + f"/{scratch}"
    plain = url.replace("+asyncpg", "")
    exists = (
        "SELECT string_agg(table_name, ',' ORDER BY table_name) FROM information_schema.tables "
        "WHERE table_schema = 'mt5'"
    )
    try:
        _alembic(url, "upgrade", "head")
        tables = _psql(plain, exists).split(",")
        assert {"records", "bars", "candles", "events", "pushes"} <= set(tables)
        idx = _psql(
            plain, "SELECT indexdef FROM pg_indexes WHERE indexname='ix_mt5_records_payload'"
        )
        assert "gin" in idx and "jsonb_path_ops" in idx
        _alembic(url, "downgrade", "0030")
        tables = _psql(plain, exists).split(",")
        assert "records" not in tables and "bars" not in tables
        assert {"candles", "events", "pushes", "failures", "bot_snapshots"} <= set(tables)
        _alembic(url, "upgrade", "head")
        assert {"records", "bars"} <= set(_psql(plain, exists).split(","))
    finally:
        _psql(f"{server}/postgres", f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')


async def test_app_role_grants_cover_the_new_tables(env: Env) -> None:
    """Schema mt5 is in deploy/sql/roles.sql's DML schemas; the tables live there."""
    async with env.maker() as db:
        names = (
            await db.scalars(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='mt5' "
                    "AND table_name IN ('records','bars') ORDER BY 1"
                )
            )
        ).all()
    assert list(names) == ["bars", "records"]
    roles = (ROOT / "deploy" / "sql" / "roles.sql").read_text()
    assert "'mt5'" in roles
