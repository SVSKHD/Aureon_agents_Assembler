"""Market terminal against a real database: watchlists (ownership, CSRF), the screener
(inline and queued runs, caching, sorting, pagination, coverage, large universes), on-demand
indicators agreeing with the screener, instrument filters/status, candle history requests
(Dhan validation, incremental windows, failures)."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.research import indicators as ind
from app.agents.research import screener as eng
from app.agents.trading.base import BrokerAdapter, BrokerError, NotSupported
from app.agents.trading.types import AdapterHealth, Candle, Timeframe, utc_now
from app.auth.bootstrap import bootstrap_admin
from app.auth.security import hash_password
from app.core.config import Settings
from app.main import create_app
from app.models.auth import User
from app.models.brokers import DhanInstrumentSyncRun
from app.models.trading import (
    CandleBackfillRequest,
    Instrument,
    MarketCandle,
    ScreenerResult,
    ScreenerRun,
)
from app.services import market_data
from app.services import screener as svc
from app.workers.research import SCREENER, ResearchWorker

pytestmark = pytest.mark.db
PASSWORD = "correct horse battery staple"
STEP = timedelta(minutes=15)


def last_closed_open() -> datetime:
    """Open time of the newest fully closed 15m bar."""
    now = datetime.now(UTC)
    floor = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    return floor - STEP


def cross_up_close(n_down: int = 60) -> list[float]:
    close = [200.0 - i for i in range(n_down)]
    while True:
        close.append(close[-1] + 4)
        e8 = ind.ema(pd.Series(close), 8).to_numpy()
        e32 = ind.ema(pd.Series(close), 32).to_numpy()
        if eng.find_cross(e8 - e32, 1, "bullish") == len(close) - 1:
            return close


class Env:
    def __init__(self, app: Any, clients: dict[str, AsyncClient], csrf: dict[str, str]) -> None:
        self.app, self.c, self.csrf = app, clients, csrf
        self.ids: dict[str, Any] = {}

    @property
    def maker(self) -> async_sessionmaker[AsyncSession]:
        m: async_sessionmaker[AsyncSession] = self.app.state.sessionmaker
        return m

    @property
    def settings(self) -> Settings:
        s: Settings = self.app.state.settings
        return s

    def send(self, who: str, method: str, url: str, **kw: Any) -> Any:
        return self.c[who].request(method, url, headers={"x-csrf-token": self.csrf[who]}, **kw)

    async def instrument(
        self,
        sym: str,
        seg: str = "NSE_EQ",
        itype: str = "EQUITY",
        active: bool = True,
        **kw: Any,
    ) -> int:
        async with self.maker() as db, db.begin():
            inst = Instrument(
                broker="dhan",
                broker_instrument_id=f"{seg}:{abs(hash(sym)) % 10_000_000}",
                symbol=sym,
                trading_symbol=sym,
                display_name=kw.pop("display_name", sym),
                exchange=seg.split("_")[0] if seg != "IDX_I" else "NSE",
                segment=seg,
                instrument_type=itype,
                currency="INR",
                lot_size=Decimal(1),
                tick_size=Decimal("0.05"),
                is_active=active,
                **kw,
            )
            db.add(inst)
            await db.flush()
            return inst.id

    async def candles(
        self,
        iid: int,
        close: list[float],
        *,
        end_open: datetime | None = None,
        tf: str = "15m",
        step: timedelta = STEP,
        volume: float = 1000,
    ) -> None:
        end_open = end_open or last_closed_open()
        start = end_open - step * (len(close) - 1)
        rows = [
            {
                "instrument_id": iid,
                "timeframe": tf,
                "ts": start + step * i,
                "open": Decimal(str(c)),
                "high": Decimal(str(c + 1)),
                "low": Decimal(str(c - 1)),
                "close": Decimal(str(c)),
                "volume": Decimal(str(volume)),
                "session": "Asia",
                "source": "broker",
            }
            for i, c in enumerate(close)
        ]
        async with self.maker() as db, db.begin():
            await db.execute(insert(MarketCandle), rows)


EnvFactory = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


@pytest.fixture
def make_env(db_clean: str, settings: Settings) -> EnvFactory:
    @contextlib.asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[Env]:
        engine = create_async_engine(db_clean)
        async with engine.begin() as conn:
            for t in (
                "trading.screener_runs",
                "trading.watchlists",
                "trading.candle_backfill_requests",
                "trading.instruments",
                "dhan.instrument_sync_runs",
            ):
                await conn.exec_driver_sql(f"DELETE FROM {t}")
        maker = async_sessionmaker(engine, expire_on_commit=False)
        admin = await bootstrap_admin(maker, "admin@spasta.online", PASSWORD)
        ids: dict[str, Any] = {"admin": admin.id}
        async with maker() as db, db.begin():
            for who in ("user", "other"):
                u = User(
                    email=f"{who}@spasta.online", password_hash=hash_password(PASSWORD), role_id=2
                )
                db.add(u)
                await db.flush()
                ids[who] = u.id
        await engine.dispose()
        app = create_app(Settings.model_validate({**settings.model_dump(), **overrides}))
        async with LifespanManager(app) as mgr:
            t = ASGITransport(app=mgr.app)
            clients = {
                w: AsyncClient(transport=t, base_url="https://test")
                for w in ("admin", "user", "other", "anon")
            }
            csrf: dict[str, str] = {"anon": ""}
            for who in ("admin", "user", "other"):
                r = await clients[who].post(
                    "/api/auth/login",
                    json={"email": f"{who}@spasta.online", "password": PASSWORD},
                )
                csrf[who] = r.json()["csrf_token"]
            env = Env(app, clients, csrf)
            env.ids = ids
            try:
                yield env
            finally:
                for c in clients.values():
                    await c.aclose()

    return factory


EMA_8_32 = {"type": "ema_cross", "fast": 8, "slow": 32, "direction": "bullish", "within_bars": 3}


def body(**kw: Any) -> dict[str, Any]:
    return {
        "universe": {"exchange": ["NSE"], "kind": ["equity"], "watchlist_id": None},
        "timeframe": "15m",
        "candle": "confirmed",
        "filters": [EMA_8_32],
        "sort": {"field": "change_pct", "dir": "desc"},
        "limit": 50,
        "offset": 0,
        **kw,
    }


# --- auth ---------------------------------------------------------------------------------


async def test_auth_and_csrf_are_required(make_env: EnvFactory) -> None:
    async with make_env() as env:
        anon = env.c["anon"]
        for method, url in (
            ("POST", "/api/trading/screener/run"),
            ("GET", "/api/trading/screener/status"),
            ("GET", "/api/trading/watchlists"),
            ("GET", "/api/trading/instruments/status"),
            ("GET", "/api/trading/candles/backfill"),
        ):
            r = await anon.request(method, url, json=body() if method == "POST" else None)
            assert r.status_code == 401, (url, r.status_code)
        # Logged in but no CSRF header: refused before anything runs.
        r = await env.c["user"].post("/api/trading/screener/run", json=body())
        assert r.status_code == 403
        r = await env.c["user"].post("/api/trading/watchlists", json={"name": "x"})
        assert r.status_code == 403


# --- watchlists ---------------------------------------------------------------------------


async def test_watchlists_crud_and_isolation(make_env: EnvFactory) -> None:
    async with make_env() as env:
        a = await env.instrument("INFY")
        b = await env.instrument("TCS")
        dead = await env.instrument("OLDCO")
        r = await env.send("user", "POST", "/api/trading/watchlists", json={"name": " Swing "})
        assert r.status_code == 201, r.text
        wl = r.json()
        assert wl["name"] == "Swing" and wl["items"] == []
        wid = wl["id"]
        dup = await env.send("user", "POST", "/api/trading/watchlists", json={"name": "Swing"})
        assert dup.status_code == 409
        for iid in (a, b, dead):
            r = await env.send(
                "user", "POST", f"/api/trading/watchlists/{wid}/items", json={"instrument_id": iid}
            )
            assert r.status_code == 201, r.text
        again = await env.send(
            "user", "POST", f"/api/trading/watchlists/{wid}/items", json={"instrument_id": a}
        )
        assert again.status_code == 409
        missing = await env.send(
            "user", "POST", f"/api/trading/watchlists/{wid}/items", json={"instrument_id": 999999}
        )
        assert missing.status_code == 404
        # The instrument is later delisted: the item stays, `instrument` becomes null.
        async with env.maker() as db, db.begin():
            inst = await db.get(Instrument, dead)
            assert inst is not None
            inst.is_active = False
        got = (await env.c["user"].get(f"/api/trading/watchlists/{wid}")).json()
        assert [i["instrument_id"] for i in got["items"]] == [a, b, dead]
        assert got["items"][0]["instrument"]["trading_symbol"] == "INFY"
        assert got["items"][2]["instrument"] is None

        # Another user (and an admin) cannot see, change or screen it.
        for who in ("other", "admin"):
            assert (await env.c[who].get(f"/api/trading/watchlists/{wid}")).status_code == 404
            assert (await env.c[who].get("/api/trading/watchlists")).json() == []
            r = await env.send(who, "PATCH", f"/api/trading/watchlists/{wid}", json={"name": "x"})
            assert r.status_code == 404
            r = await env.send(who, "DELETE", f"/api/trading/watchlists/{wid}/items/{a}")
            assert r.status_code == 404
            r = await env.send(
                who,
                "POST",
                "/api/trading/screener/run",
                json=body(universe={"watchlist_id": wid}),
            )
            assert r.status_code == 404

        r = await env.send("user", "PATCH", f"/api/trading/watchlists/{wid}", json={"name": "S2"})
        assert r.status_code == 200 and r.json()["name"] == "S2"
        r = await env.send("user", "DELETE", f"/api/trading/watchlists/{wid}/items/{b}")
        assert r.status_code == 204
        r = await env.send("user", "DELETE", f"/api/trading/watchlists/{wid}/items/{b}")
        assert r.status_code == 404
        r = await env.send("user", "DELETE", f"/api/trading/watchlists/{wid}")
        assert r.status_code == 204
        assert (await env.c["user"].get("/api/trading/watchlists")).json() == []


async def test_watchlist_limits(make_env: EnvFactory) -> None:
    async with make_env(WATCHLIST_MAX_PER_USER=1, WATCHLIST_MAX_ITEMS=1) as env:
        a, b = await env.instrument("A1"), await env.instrument("B1")
        wid = (
            await env.send("user", "POST", "/api/trading/watchlists", json={"name": "1"})
        ).json()["id"]
        r = await env.send("user", "POST", "/api/trading/watchlists", json={"name": "2"})
        assert r.status_code == 409
        url = f"/api/trading/watchlists/{wid}/items"
        assert (await env.send("user", "POST", url, json={"instrument_id": a})).status_code == 201
        r = await env.send("user", "POST", url, json={"instrument_id": b})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "watchlist_full"


# --- screener -----------------------------------------------------------------------------


async def test_screener_inline_run_matches_real_candles(make_env: EnvFactory) -> None:
    async with make_env() as env:
        up = cross_up_close()
        hit = await env.instrument("HIT")
        hit2 = await env.instrument("HIT2")
        flat = await env.instrument("FLAT")
        short = await env.instrument("SHORT")
        await env.instrument("NOCANDLES")
        old = await env.instrument("OLD")
        bse = await env.instrument("BSEONLY", seg="BSE_EQ")
        await env.instrument("GONE", active=False)
        await env.candles(hit, up)
        await env.candles(hit2, [c * 2 for c in up])
        await env.candles(flat, [100.0] * 80)
        await env.candles(short, up[-20:])
        await env.candles(old, up, end_open=last_closed_open() - STEP * 10)  # lags: stale
        await env.candles(bse, up)

        r = await env.send("user", "POST", "/api/trading/screener/run", json=body())
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["status"] == "done" and out["cached"] is False
        assert out["universe_size"] == 6  # NSE equities, active only
        assert out["total"] == 2
        assert {i["instrument"]["trading_symbol"] for i in out["items"]} == {"HIT", "HIT2"}
        cov = out["coverage"]
        assert cov == {
            "scanned": 6,
            "matched": 2,
            "no_data": 1,
            "insufficient_data": 1,
            "stale": 1,
        }
        item = out["items"][0]
        assert item["signal"] == "bullish_cross" and item["candle_closed"] is True
        assert item["crossed_at"] == item["data_as_of"]
        assert set(item["indicators"]) == {"ema:8", "ema:32"}
        assert Decimal(item["last"]) > 0 and item["change_pct"] is not None
        assert out["universe_data_as_of"] == item["data_as_of"]

        # Same scan again: served from the stored run (no recompute), paged and re-sorted.
        r2 = await env.send(
            "user",
            "POST",
            "/api/trading/screener/run",
            json=body(sort={"field": "last", "dir": "asc"}, limit=1, offset=1),
        )
        o2 = r2.json()
        assert r2.status_code == 200 and o2["cached"] is True and o2["run_id"] == out["run_id"]
        assert o2["total"] == 2 and len(o2["items"]) == 1
        assert o2["items"][0]["instrument"]["trading_symbol"] == "HIT2"  # higher price, page 2
        async with env.maker() as db:
            assert await db.scalar(select(func.count()).select_from(ScreenerRun)) == 1

        # The run is the owner's only.
        rid = out["run_id"]
        assert (await env.c["other"].get(f"/api/trading/screener/runs/{rid}")).status_code == 404
        page = await env.c["user"].get(
            f"/api/trading/screener/runs/{rid}", params={"sort": "symbol", "dir": "asc"}
        )
        assert [i["instrument"]["trading_symbol"] for i in page.json()["items"]] == [
            "HIT",
            "HIT2",
        ]
        runs = (await env.c["user"].get("/api/trading/screener/runs")).json()
        assert runs["total"] == 1 and runs["items"][0]["skipped"]["stale"] == 1
        assert (await env.c["other"].get("/api/trading/screener/runs")).json()["total"] == 0

        # BSE universe: only the BSE stock.
        r3 = await env.send(
            "user",
            "POST",
            "/api/trading/screener/run",
            json=body(universe={"exchange": ["BSE"], "kind": ["equity"]}),
        )
        assert [i["instrument"]["id"] for i in r3.json()["items"]] == [bse]

        status = (await env.c["user"].get("/api/trading/screener/status")).json()
        assert status["worker"] == "down"  # no research worker heartbeat in this test
        assert status["candles_as_of"]["15m"] is not None and status["candles_as_of"]["1d"] is None
        assert status["universe_size"] == 1 and status["last_run_at"] is not None


async def test_screener_confirmed_vs_live_candle(make_env: EnvFactory) -> None:
    async with make_env() as env:
        iid = await env.instrument("LIVE")
        # The crossing bar is the one still forming now: confirmed must not see it.
        await env.candles(iid, cross_up_close(), end_open=last_closed_open() + STEP)
        flt = [{**EMA_8_32, "within_bars": 1}]
        conf = (
            await env.send("user", "POST", "/api/trading/screener/run", json=body(filters=flt))
        ).json()
        assert conf["total"] == 0 and conf["coverage"]["scanned"] == 1
        live = (
            await env.send(
                "user", "POST", "/api/trading/screener/run", json=body(filters=flt, candle="live")
            )
        ).json()
        assert live["total"] == 1 and live["items"][0]["candle_closed"] is False
        candles = (
            await env.c["user"].get(
                "/api/trading/candles", params={"instrument_id": iid, "timeframe": "15m"}
            )
        ).json()
        assert candles[-1]["is_closed"] is False and candles[-2]["is_closed"] is True


async def test_screener_indicators_agree_with_compute_endpoint(make_env: EnvFactory) -> None:
    async with make_env() as env:
        iid = await env.instrument("SAME")
        await env.candles(iid, [100.0 + (i % 7) * 0.5 for i in range(200)] + cross_up_close())
        out = (await env.send("user", "POST", "/api/trading/screener/run", json=body())).json()
        assert out["total"] == 1
        screened = out["items"][0]["indicators"]
        r = await env.c["user"].get(
            "/api/trading/indicators/compute",
            params={
                "instrument_id": iid,
                "timeframe": "15m",
                "specs": "ema:8,ema:32,ema:21,ema:50,ema:200,rsi:14,adx:14,macd:12:26:9,vwap",
                "limit": 5,
            },
        )
        assert r.status_code == 200, r.text
        rows = r.json()
        last = {x["name"]: x for x in rows if x["ts"] == out["items"][0]["data_as_of"]}
        assert last["ema:8"]["values"]["value"] == screened["ema:8"]
        assert last["ema:32"]["values"]["value"] == screened["ema:32"]
        assert set(last) >= {"ema:200", "rsi:14", "adx:14", "macd:12:26:9", "vwap"}
        assert set(last["adx:14"]["values"]) == {"adx", "plus_di", "minus_di"}
        assert len([x for x in rows if x["name"] == "ema:8"]) == 5
        bad = await env.c["user"].get(
            "/api/trading/indicators/compute",
            params={"instrument_id": iid, "timeframe": "15m", "specs": "ema:0"},
        )
        assert bad.status_code == 422
        bad = await env.c["user"].get(
            "/api/trading/indicators/compute",
            params={"instrument_id": iid, "timeframe": "1d", "specs": "vwap"},
        )
        assert bad.status_code == 422


async def test_screener_refusals(make_env: EnvFactory) -> None:
    async with make_env(SCREENER_MAX_UNIVERSE=10, SCREENER_USER_ACTIVE_MAX=1) as env:
        for i in range(11):
            await env.instrument(f"S{i}")
        r = await env.send("user", "POST", "/api/trading/screener/run", json=body())
        assert r.status_code == 422 and r.json()["detail"]["code"] == "universe_too_large"
        r = await env.send("user", "POST", "/api/trading/screener/run", json=body(timeframe="30m"))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "timeframe_unsupported"
        r = await env.send(
            "user",
            "POST",
            "/api/trading/screener/run",
            json=body(universe={"watchlist_id": "device-1"}),
        )
        assert r.status_code == 422
        r = await env.send(
            "user",
            "POST",
            "/api/trading/screener/run",
            json=body(universe={"watchlist_id": str(uuid.uuid4())}),
        )
        assert r.status_code == 404
        r = await env.send(
            "user",
            "POST",
            "/api/trading/screener/run",
            json=body(filters=[{**EMA_8_32, "fast": 32, "slow": 8}]),
        )
        assert r.status_code == 422


async def test_large_universe_is_queued_and_worked_in_chunks(make_env: EnvFactory) -> None:
    async with make_env(
        SCREENER_INLINE_MAX=5, SCREENER_CHUNK=10, SCREENER_USER_ACTIVE_MAX=2
    ) as env:
        up = cross_up_close()
        hits = []
        for i in range(25):
            iid = await env.instrument(f"U{i:02d}")
            if i % 5 == 0:
                await env.candles(iid, up)
                hits.append(iid)
        r = await env.send("user", "POST", "/api/trading/screener/run", json=body())
        assert r.status_code == 202, r.text
        q = r.json()
        assert q["status"] == "queued" and q["items"] == [] and q["total"] == 0
        assert "poll" in q["detail"]
        # The same request while queued returns the same run (no duplicate scan).
        again = await env.send("user", "POST", "/api/trading/screener/run", json=body())
        assert again.status_code == 202 and again.json()["run_id"] == q["run_id"]

        # A second, different scan queues; a third exceeds SCREENER_USER_ACTIVE_MAX.
        other = body(filters=[{**EMA_8_32, "direction": "bearish"}])
        r2 = await env.send("user", "POST", "/api/trading/screener/run", json=other)
        assert r2.status_code == 202
        r3 = await env.send("user", "POST", "/api/trading/screener/run", json=body(timeframe="1h"))
        assert r3.status_code == 429
        # Cancel the second while queued.
        rid2 = r2.json()["run_id"]
        assert (await env.send("user", "DELETE", f"/api/trading/screener/runs/{rid2}")).status_code
        st = (await env.c["user"].get(f"/api/trading/screener/runs/{rid2}")).json()
        assert st["status"] == "cancelled"

        # The research worker's screener lane claims and runs it.
        worker = ResearchWorker(env.settings, env.maker, {})
        run_id = await svc.claim_run(env.maker, env.settings)
        assert str(run_id) == q["run_id"]
        assert await svc.claim_run(env.maker, env.settings) is None  # nothing else queued
        await worker.do_screener(run_id)
        assert worker.lane(SCREENER).extra["done"] == 1
        done = (
            await env.c["user"].get(
                f"/api/trading/screener/runs/{run_id}", params={"limit": 2, "offset": 2}
            )
        ).json()
        assert done["status"] == "done" and done["total"] == 5 and len(done["items"]) == 2
        assert done["coverage"] == {
            "scanned": 25,
            "matched": 5,
            "no_data": 20,
            "insufficient_data": 0,
            "stale": 0,
        }
        async with env.maker() as db:
            stored = set(
                (
                    await db.scalars(
                        select(ScreenerResult.instrument_id).where(ScreenerResult.run_id == run_id)
                    )
                ).all()
            )
        assert stored == set(hits)


async def test_run_without_candles_says_so(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.instrument("EMPTY")
        out = (await env.send("user", "POST", "/api/trading/screener/run", json=body())).json()
        assert out["status"] == "done" and out["total"] == 0 and out["items"] == []
        assert out["coverage"]["no_data"] == 1 and out["universe_data_as_of"] is None
        assert "backfill" in out["detail"]


async def test_screener_watchlist_universe(make_env: EnvFactory) -> None:
    async with make_env() as env:
        idx = await env.instrument("NIFTY 50", seg="IDX_I", itype="INDEX")
        eq = await env.instrument("RELIANCE")
        up = cross_up_close()
        await env.candles(idx, up, volume=0)
        await env.candles(eq, up)
        wid = (
            await env.send("user", "POST", "/api/trading/watchlists", json={"name": "mix"})
        ).json()["id"]
        for iid in (idx, eq):
            await env.send(
                "user", "POST", f"/api/trading/watchlists/{wid}/items", json={"instrument_id": iid}
            )
        out = (
            await env.send(
                "user",
                "POST",
                "/api/trading/screener/run",
                json=body(universe={"watchlist_id": wid}),
            )
        ).json()
        assert out["universe_size"] == 2 and out["total"] == 2
        # The volume filter cannot judge an index (no volume): insufficient, not a match.
        vol = {"type": "volume", "op": ">", "multiple_of_avg": 0.5, "avg_period": 20}
        out = (
            await env.send(
                "user",
                "POST",
                "/api/trading/screener/run",
                json=body(universe={"watchlist_id": wid}, filters=[vol]),
            )
        ).json()
        assert [i["instrument"]["id"] for i in out["items"]] == [eq]
        assert out["coverage"]["insufficient_data"] == 1


# --- instruments --------------------------------------------------------------------------


async def test_instrument_filters_and_status(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.instrument("INFY", display_name="Infosys", name="INFOSYS LIMITED")
        await env.instrument("INFY", seg="BSE_EQ")
        await env.instrument("NIFTY 50", seg="IDX_I", itype="INDEX")
        exp = datetime.now(UTC).date() + timedelta(days=10)
        fut = await env.instrument(
            "NIFTY-FUT", seg="NSE_FNO", itype="FUTIDX", underlying="NIFTY", expiry=exp
        )
        ce = await env.instrument(
            "NIFTY-24000-CE",
            seg="NSE_FNO",
            itype="OPTIDX",
            underlying="NIFTY",
            expiry=exp,
            strike=Decimal(24000),
            option_type="CE",
        )
        await env.instrument(
            "NIFTY-25000-PE",
            seg="NSE_FNO",
            itype="OPTIDX",
            underlying="NIFTY",
            expiry=exp,
            strike=Decimal(25000),
            option_type="PE",
        )
        await env.instrument("EXPIRED-FUT", seg="NSE_FNO", itype="FUTSTK", active=False)

        async def get(**params: Any) -> dict[str, Any]:
            r = await env.c["user"].get("/api/trading/instruments", params=params)
            assert r.status_code == 200, r.text
            out: dict[str, Any] = r.json()
            return out

        assert (await get(q="infosys"))["total"] == 1
        assert (await get(q="infy"))["total"] == 2
        assert (await get(q="infy", exchange="BSE"))["items"][0]["segment"] == "BSE_EQ"
        assert (await get(segment=["IDX_I"]))["items"][0]["instrument_type"] == "INDEX"
        futs = await get(instrument_type=["FUTIDX", "FUTSTK"])
        assert [i["id"] for i in futs["items"]] == [fut]  # the expired one is inactive
        assert (await get(instrument_type=["FUTSTK"], include_inactive=True))["total"] == 1
        opts = await get(underlying="nifty", option_type="CE", strike_max="24500")
        assert [i["id"] for i in opts["items"]] == [ce]
        assert (await get(expiry=exp.isoformat(), segment="NSE_FNO"))["total"] == 3

        st = (await env.c["user"].get("/api/trading/instruments/status")).json()
        assert st["last_synced_at"] is None and st["stale"] is True
        assert st["by_segment"] == {"NSE_EQ": 1, "BSE_EQ": 1, "IDX_I": 1, "NSE_FNO": 3}
        async with env.maker() as db, db.begin():
            db.add(
                DhanInstrumentSyncRun(
                    trigger="scheduled", status="succeeded", finished_at=datetime.now(UTC)
                )
            )
            db.add(DhanInstrumentSyncRun(trigger="manual", status="failed", error="HTTP 503"))
        st = (await env.c["user"].get("/api/trading/instruments/status")).json()
        assert st["stale"] is False and st["count"] == 6
        assert st["last_error"] == "HTTP 503" and st["last_run_status"] == "failed"


# --- candle history -----------------------------------------------------------------------


class FakeDhan(BrokerAdapter):
    broker = "dhan"

    def __init__(self, rows: list[Candle], fail: Exception | None = None) -> None:
        self.rows, self.fail = rows, fail
        self.calls: list[tuple[datetime, datetime]] = []

    async def health(self) -> AdapterHealth:
        return AdapterHealth(broker="dhan", ok=True, checked_at=utc_now())

    async def candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        self.calls.append((start, end))
        if self.fail:
            raise self.fail
        return [c for c in self.rows if start <= c.ts < end]


def daily(n: int, end: datetime) -> list[Candle]:
    day = timedelta(days=1)
    first = end.replace(hour=0, minute=0, second=0, microsecond=0) - day * n
    return [
        Candle(
            ts=first + day * i,
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal(100 + i),
            volume=Decimal(10),
        )
        for i in range(n)
    ]


async def test_backfill_queue_validation_and_dedupe(make_env: EnvFactory) -> None:
    async with make_env(CANDLE_BACKFILL_USER_OPEN_MAX=3) as env:
        ok = await env.instrument("OK")
        ok2 = await env.instrument("OK2")
        gone = await env.instrument("GONE", active=False)
        fx = await env.instrument("USDINR", seg="NSE_CURRENCY", itype="FUTCUR")
        url = "/api/trading/candles/backfill"
        r = await env.send(
            "user",
            "POST",
            url,
            json={"instrument_ids": [ok, gone, fx, 424242], "timeframe": "1d", "days": 400},
        )
        assert r.status_code == 202, r.text
        out = r.json()
        assert out["queued"] == 1 and out["already_open"] == 0
        reasons = {x["instrument_id"]: x["reason"] for x in out["rejected"]}
        assert reasons[424242] == "unknown_instrument"
        assert reasons[gone].startswith("inactive")
        assert "NSE_CURRENCY" in reasons[fx]
        again = await env.send(
            "user", "POST", url, json={"instrument_ids": [ok], "timeframe": "1d", "days": 400}
        )
        assert again.json() == {"queued": 0, "already_open": 1, "rejected": []}
        for tf, days in (("30m", 5), ("4h", 5), ("1m", 91)):
            r = await env.send(
                "user", "POST", url, json={"instrument_ids": [ok], "timeframe": tf, "days": days}
            )
            assert r.status_code == 422, (tf, r.text)
        r = await env.send(
            "user",
            "POST",
            url,
            json={"instrument_ids": [ok, ok2], "timeframe": "15m", "days": 5},
        )
        assert r.status_code == 202
        r = await env.send(
            "user", "POST", url, json={"instrument_ids": [ok2], "timeframe": "1h", "days": 5}
        )
        assert r.status_code == 429  # 3 open already (CANDLE_BACKFILL_USER_OPEN_MAX)
        mine = (await env.c["user"].get(url)).json()
        assert mine["total"] == 3
        assert (await env.c["other"].get(url)).json()["total"] == 0
        assert (await env.c["admin"].get(url, params={"status": "queued"})).json()["total"] == 3


async def test_backfill_worker_fetches_only_missing_bars(make_env: EnvFactory) -> None:
    async with make_env() as env:
        iid = await env.instrument("HIST")
        now = datetime.now(UTC)
        rows = daily(30, now)
        dhan = FakeDhan(rows)
        async with env.maker() as db, db.begin():
            await market_data.enqueue_backfill(db, [iid], Timeframe.D1, 20, env.settings)
        rid = await market_data.claim_backfill(env.maker)
        assert rid is not None
        assert await market_data.run_backfill(env.maker, dhan, rid, env.settings, now) == "done"
        start, _ = dhan.calls[0]
        assert abs((start - (now - timedelta(days=20))).total_seconds()) < 1  # full window
        async with env.maker() as db:
            req = await db.get(CandleBackfillRequest, rid)
            assert (
                req is not None and req.candles == 19 and req.status == "done"
            )  # midnight bars inside now-20d..now
        # The next request for the same instrument only asks for bars after the newest one.
        async with env.maker() as db, db.begin():
            await market_data.enqueue_backfill(db, [iid], Timeframe.D1, 20, env.settings)
        rid2 = await market_data.claim_backfill(env.maker)
        assert rid2 is not None
        await market_data.run_backfill(env.maker, dhan, rid2, env.settings, now)
        start2, _ = dhan.calls[1]
        assert start2 == rows[-1].ts


async def test_backfill_dhan_failures(make_env: EnvFactory) -> None:
    async with make_env(CANDLE_BACKFILL_MAX_ATTEMPTS=2) as env:
        iid = await env.instrument("FAIL")
        broken = FakeDhan([], fail=BrokerError("DH-904 rate limited"))
        async with env.maker() as db, db.begin():
            await market_data.enqueue_backfill(db, [iid], Timeframe.H1, 5, env.settings)
        rid = await market_data.claim_backfill(env.maker)
        assert rid is not None
        assert await market_data.run_backfill(env.maker, broken, rid, env.settings) == "queued"
        assert await market_data.claim_backfill(env.maker) == rid
        assert await market_data.run_backfill(env.maker, broken, rid, env.settings) == "failed"
        async with env.maker() as db:
            req = await db.get(CandleBackfillRequest, rid)
            assert req is not None and req.attempts == 2 and "DH-904" in (req.error or "")
            n = await db.scalar(select(func.count()).select_from(MarketCandle))
        assert n == 0  # nothing invented on failure
        # Not supported: failed at once, no retry.
        async with env.maker() as db, db.begin():
            await market_data.enqueue_backfill(db, [iid], Timeframe.H1, 5, env.settings)
        rid = await market_data.claim_backfill(env.maker)
        assert rid is not None
        ns = FakeDhan([], fail=NotSupported("dhan", "candles", "no 1h for this segment"))
        assert await market_data.run_backfill(env.maker, ns, rid, env.settings) == "failed"
        # No Dhan adapter at all.
        async with env.maker() as db, db.begin():
            await market_data.enqueue_backfill(db, [iid], Timeframe.M5, 2, env.settings)
        rid = await market_data.claim_backfill(env.maker)
        assert rid is not None
        assert await market_data.run_backfill(env.maker, None, rid, env.settings) == "failed"


async def test_universe_enqueue(make_env: EnvFactory) -> None:
    async with make_env(
        DHAN_UNIVERSE_BACKFILL=["nse_equities", "indices", "fno_stocks", "watchlists"],
        DHAN_UNIVERSE_BACKFILL_TIMEFRAMES=["1d", "15m"],
    ) as env:
        infy = await env.instrument("INFY")
        await env.instrument("TCS")
        await env.instrument("NIFTY 50", seg="IDX_I", itype="INDEX")
        await env.instrument("INFY-FUT", seg="NSE_FNO", itype="FUTSTK", underlying="INFY")
        out = await market_data.enqueue_universes(env.maker, env.settings)
        assert out["nse_equities:1d"]["queued"] == 2
        assert out["indices:1d"]["queued"] == 1
        assert out["fno_stocks:1d"] == {
            "instruments": 1,
            "queued": 0,
            "already_open": 1,
            "rejected": 0,
        }
        assert out["watchlists:1d"]["instruments"] == 0
        async with env.maker() as db:
            rows = (await db.scalars(select(CandleBackfillRequest))).all()
        days = {(r.instrument_id, r.timeframe): r.days for r in rows}
        assert days[(infy, "15m")] == env.settings.CANDLE_BACKFILL_MAX_DAYS_INTRADAY
        assert days[(infy, "1d")] == env.settings.DHAN_UNIVERSE_BACKFILL_DAYS
        assert {r.source for r in rows} == {"universe"}
