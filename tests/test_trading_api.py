"""Trading Phase A API: accounts, execution-mode gate, risk policy, adapter reads, data."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.trading.fake import FakeBrokerAdapter
from app.auth.bootstrap import bootstrap_admin
from app.auth.security import hash_password
from app.core.config import Settings
from app.main import create_app
from app.models.agents import Agent
from app.models.auth import AuditEvent, User
from app.models.trading import (
    BrokerAccount,
    Execution,
    Instrument,
    MarketCandle,
    Strategy,
    StrategyVersion,
    TradingSignal,
)

pytestmark = pytest.mark.db
PASSWORD = "correct horse battery staple"
T = datetime(2026, 11, 2, 9, 15, tzinfo=UTC)


class Env:
    def __init__(
        self,
        app: FastAPI,
        admin: AsyncClient,
        user: AsyncClient,
        other: AsyncClient,
        csrf: dict[str, str],
        ids: dict[str, Any],
    ) -> None:
        self.app, self.admin, self.user, self.other = app, admin, user, other
        self.csrf, self.ids = csrf, ids

    @property
    def maker(self) -> async_sessionmaker[AsyncSession]:
        m: async_sessionmaker[AsyncSession] = self.app.state.sessionmaker
        return m

    def send(self, who: str, method: str, url: str, **kw: Any) -> Any:
        c = {"admin": self.admin, "user": self.user, "other": self.other}[who]
        return c.request(method, url, headers={"x-csrf-token": self.csrf[who]}, **kw)

    async def account(
        self, owner: str = "user", broker: str = "dhan", account_id: str = "1100012345"
    ) -> str:
        r = await self.send(
            "admin",
            "POST",
            "/api/trading/accounts",
            json={
                "broker": broker,
                "account_id": account_id,
                "label": "main",
                "base_currency": "INR",
                "owner_user_id": str(self.ids[owner]),
            },
        )
        assert r.status_code == 201, r.text
        return str(r.json()["id"])

    async def events(self) -> list[str]:
        async with self.maker() as db:
            return list(await db.scalars(select(AuditEvent.event_type).order_by(AuditEvent.id)))


EnvFactory = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


@pytest.fixture
def make_env(db_clean: str, settings: Settings) -> EnvFactory:
    @contextlib.asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[Env]:
        engine = create_async_engine(db_clean)
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
                for w in ("admin", "user", "other")
            }
            csrf = {}
            for who, c in clients.items():
                r = await c.post(
                    "/api/auth/login", json={"email": f"{who}@spasta.online", "password": PASSWORD}
                )
                csrf[who] = r.json()["csrf_token"]
            try:
                yield Env(app, clients["admin"], clients["user"], clients["other"], csrf, ids)
            finally:
                for c in clients.values():
                    await c.aclose()

    return factory


# --- accounts + isolation -----------------------------------------------------------------


async def test_account_create_is_admin_only_audited_and_defaults_to_observe(
    make_env: EnvFactory,
) -> None:
    async with make_env() as env:
        body = {"broker": "dhan", "account_id": "1", "base_currency": "INR"}
        assert (
            await env.send("user", "POST", "/api/trading/accounts", json=body)
        ).status_code == 403
        assert (await env.admin.post("/api/trading/accounts", json=body)).status_code == 403
        acct = await env.account()
        got = (await env.user.get(f"/api/trading/accounts/{acct}")).json()
        assert got["mode"] == "OBSERVE" and got["broker"] == "dhan"
        dup = await env.send(
            "admin",
            "POST",
            "/api/trading/accounts",
            json={"broker": "dhan", "account_id": "1100012345", "base_currency": "INR"},
        )
        assert dup.status_code == 409
        bad = await env.send(
            "admin",
            "POST",
            "/api/trading/accounts",
            json={"broker": "binance", "account_id": "x", "base_currency": "USD"},
        )
        assert bad.status_code == 422
        assert "trading_account_created" in await env.events()


async def test_accounts_are_scoped_to_owner_admin_sees_all(make_env: EnvFactory) -> None:
    async with make_env() as env:
        mine = await env.account("user")
        theirs = await env.account("other", account_id="2200")
        assert [a["id"] for a in (await env.user.get("/api/trading/accounts")).json()["items"]] == [
            mine
        ]
        assert (await env.admin.get("/api/trading/accounts")).json()["total"] == 2
        for path in ("", "/health", "/funds", "/positions", "/orders", "/risk-policy"):
            assert (
                await env.user.get(f"/api/trading/accounts/{theirs}{path}")
            ).status_code == 404, path
        r = await env.user.get(f"/api/trading/executions?account_id={theirs}")
        assert r.status_code == 404


# --- execution mode gate ------------------------------------------------------------------


async def test_mode_change_authorization_and_audit(make_env: EnvFactory) -> None:
    async with make_env() as env:
        acct = await env.account()
        url = f"/api/trading/accounts/{acct}/mode"
        assert (await env.send("user", "POST", url, json={"mode": "PAPER"})).status_code == 403
        assert (await env.admin.post(url, json={"mode": "PAPER"})).status_code == 403  # CSRF
        r = await env.send("admin", "POST", url, json={"mode": "PAPER"})
        assert r.status_code == 200 and r.json()["mode"] == "PAPER"
        assert r.json()["mode_changed_at"]
        assert (await env.send("admin", "POST", url, json={"mode": "OBSERVE"})).json()[
            "mode"
        ] == "OBSERVE"
        assert (await env.send("admin", "POST", url, json={"mode": "YOLO"})).status_code == 422
        async with env.maker() as db:
            details = list(
                await db.scalars(
                    select(AuditEvent.details)
                    .where(AuditEvent.event_type == "trading_mode_changed")
                    .order_by(AuditEvent.id)
                )
            )
        assert [(d["from"], d["to"]) for d in details] == [
            ("OBSERVE", "PAPER"),
            ("PAPER", "OBSERVE"),
        ]


async def test_live_unreachable_with_feature_flag_off(make_env: EnvFactory) -> None:
    async with make_env() as env:
        acct = await env.account()
        r = await env.send(
            "admin",
            "POST",
            f"/api/trading/accounts/{acct}/mode",
            json={"mode": "LIVE", "confirmation_token": "anything"},
        )
        assert r.status_code == 403 and r.json()["detail"]["code"] == "live_disabled"
        t = await env.send("admin", "POST", f"/api/trading/accounts/{acct}/mode/live-token")
        assert t.status_code == 403 and t.json()["detail"]["code"] == "live_disabled"
        assert (await env.user.get(f"/api/trading/accounts/{acct}")).json()["mode"] == "OBSERVE"
        assert (await env.events()).count("trading_mode_change_denied") == 2


async def test_live_gate_with_flag_on_still_blocked_until_phase_g(make_env: EnvFactory) -> None:
    async with make_env(TRADING_LIVE_ENABLED=True, PHASE_G_APPROVED="yes") as env:
        acct = await env.account()
        url = f"/api/trading/accounts/{acct}/mode"
        no_token = await env.send("admin", "POST", url, json={"mode": "LIVE"})
        assert no_token.json()["detail"]["code"] == "confirmation_required"
        wrong = await env.send(
            "admin", "POST", url, json={"mode": "LIVE", "confirmation_token": "guess"}
        )
        assert wrong.json()["detail"]["code"] == "confirmation_required"
        # Non-admins can't get a token.
        assert (await env.send("user", "POST", f"{url}/live-token")).status_code == 403
        token = (await env.send("admin", "POST", f"{url}/live-token")).json()
        assert token["expires_in"] == 300
        r = await env.send(
            "admin", "POST", url, json={"mode": "LIVE", "confirmation_token": token["token"]}
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "preconditions_unmet"
        assert any("Phase G" in u for u in r.json()["detail"]["unmet"])
        # Tokens are single-use.
        again = await env.send(
            "admin", "POST", url, json={"mode": "LIVE", "confirmation_token": token["token"]}
        )
        assert again.json()["detail"]["code"] == "confirmation_required"
        assert (await env.user.get(f"/api/trading/accounts/{acct}")).json()["mode"] == "OBSERVE"


# --- risk policy --------------------------------------------------------------------------


async def test_risk_policy_put_get_decimal_strings(make_env: EnvFactory) -> None:
    async with make_env() as env:
        acct = await env.account()
        url = f"/api/trading/accounts/{acct}/risk-policy"
        assert (await env.user.get(url)).status_code == 404
        body = {
            "max_daily_loss": "5000.1234",
            "max_exposure": "250000",
            "max_position_size": "75",
            "max_drawdown_pct": "12.5",
            "allowed_sessions": ["London", "NY"],
            "kill_switch": False,
        }
        assert (await env.send("user", "PUT", url, json=body)).status_code == 403
        r = await env.send("admin", "PUT", url, json=body)
        assert r.status_code == 200
        got = (await env.user.get(url)).json()
        assert got["max_daily_loss"] == "5000.1234"  # Decimal as a JSON string, not a float
        assert Decimal(got["max_drawdown_pct"]) == Decimal("12.5")
        assert got["allowed_sessions"] == ["London", "NY"]
        r = await env.send("admin", "PUT", url, json={**body, "kill_switch": True})
        assert r.json()["kill_switch"] is True
        for bad in (
            {"max_daily_loss": "-1"},
            {"allowed_sessions": []},
            {"allowed_sessions": ["NY", "NY"]},
            {"allowed_sessions": ["Sydney"]},
            {"max_drawdown_pct": "101"},
        ):
            assert (await env.send("admin", "PUT", url, json={**body, **bad})).status_code == 422, (
                bad
            )
        async with env.maker() as db:
            last = await db.scalar(
                select(AuditEvent.details)
                .where(AuditEvent.event_type == "risk_policy_updated")
                .order_by(AuditEvent.id.desc())
            )
        assert last["before"]["kill_switch"] is False and last["after"]["kill_switch"] is True


# --- broker reads -------------------------------------------------------------------------


async def test_without_adapters_reads_are_cleanly_not_supported(make_env: EnvFactory) -> None:
    async with make_env() as env:
        acct = await env.account()
        h = (await env.user.get(f"/api/trading/accounts/{acct}/health")).json()
        assert (h["supported"], h["ok"]) == (False, False)
        for path in ("funds", "positions", "orders"):
            r = await env.user.get(f"/api/trading/accounts/{acct}/{path}")
            assert r.status_code == 501, path
            assert r.json()["detail"]["code"] == "not_supported"
            assert r.json()["detail"]["operation"] == path
        q = await env.user.get("/api/trading/quotes?broker=mt5&symbols=XAUUSD")
        assert q.status_code == 501 and q.json()["detail"]["broker"] == "mt5"


async def test_with_fake_adapter_reads_return_broker_labelled_decimals(
    make_env: EnvFactory,
) -> None:
    async with make_env() as env:
        acct = await env.account()
        env.app.state.trading_adapters["dhan"] = FakeBrokerAdapter()
        h = (await env.user.get(f"/api/trading/accounts/{acct}/health")).json()
        assert h["supported"] and h["ok"] and h["credentials_expire_at"]
        f = (await env.user.get(f"/api/trading/accounts/{acct}/funds")).json()
        assert f["available"] == "100000.0000" and f["source"] == "broker"
        assert f["account_id"] == "1100012345"  # the broker's id, namespaced by the account
        pos = (await env.user.get(f"/api/trading/accounts/{acct}/positions?limit=1")).json()
        assert pos["total"] == 2 and len(pos["items"]) == 1
        assert pos["items"][0]["quantity"] == "75"
        orders = (await env.user.get(f"/api/trading/accounts/{acct}/orders")).json()
        assert orders["items"][0]["status"] == "FILLED"
        q = (await env.user.get("/api/trading/quotes?broker=dhan&symbols=RELIANCE,TCS")).json()
        assert [x["symbol"] for x in q] == ["RELIANCE", "TCS"] and q[0]["bid"] == "2890.05"
        assert (await env.user.get("/api/trading/quotes?broker=dhan&symbols=,")).status_code == 422


async def test_broker_failure_and_timeout_map_to_502_504(make_env: EnvFactory) -> None:
    class Slow(FakeBrokerAdapter):
        async def funds(self, account_id: str) -> Any:
            import asyncio

            await asyncio.sleep(5)

    async with make_env(TRADING_ADAPTER_TIMEOUT=0.2) as env:
        acct = await env.account()
        env.app.state.trading_adapters["dhan"] = FakeBrokerAdapter(fail=True)
        r = await env.user.get(f"/api/trading/accounts/{acct}/positions")
        assert r.status_code == 502 and r.json()["detail"]["code"] == "broker_error"
        env.app.state.trading_adapters["dhan"] = Slow()
        r = await env.user.get(f"/api/trading/accounts/{acct}/funds")
        assert r.status_code == 504


# --- stored data --------------------------------------------------------------------------


async def test_stored_data_endpoints_empty_then_filled(make_env: EnvFactory) -> None:
    async with make_env() as env:
        for path in ("instruments", "signals", "executions"):
            assert (await env.user.get(f"/api/trading/{path}")).json()["total"] == 0, path
        assert (await env.user.get("/api/trading/strategies")).json() == []
        assert (await env.user.get("/api/trading/agents")).json() == []

        acct = await env.account()
        async with env.maker() as db, db.begin():
            inst = Instrument(
                broker="dhan",
                broker_instrument_id="2885",
                symbol="RELIANCE",
                instrument_type="EQUITY",
                currency="INR",
                lot_size=Decimal(1),
                tick_size=Decimal("0.05"),
            )
            db.add(inst)
            await db.flush()
            for i in range(5):
                ts = T + timedelta(minutes=15 * i)
                db.add(
                    MarketCandle(
                        instrument_id=inst.id,
                        timeframe="15m",
                        ts=ts,
                        open=Decimal("2890.05"),
                        high=Decimal("2891"),
                        low=Decimal("2889.5"),
                        close=Decimal("2890.55"),
                        volume=Decimal(100),
                        session="London",
                    )
                )
            s = Strategy(name="ema-cross")
            db.add(s)
            await db.flush()
            for v in (1, 2):
                db.add(
                    StrategyVersion(
                        strategy_id=s.id,
                        version=v,
                        code_ref="ema_cross",
                        params={"fast": 9, "slow": 21 + v},
                    )
                )
            await db.flush()
            sv = await db.scalar(select(StrategyVersion).where(StrategyVersion.version == 2))
            assert sv is not None
            db.add(
                TradingSignal(
                    strategy_version_id=sv.id,
                    instrument_id=inst.id,
                    timeframe="15m",
                    candle_time=T,
                    session="London",
                    side="BUY",
                )
            )
            db.add(
                Execution(
                    broker_account_id=acct,
                    instrument_id=inst.id,
                    side="BUY",
                    quantity=Decimal(1),
                    price=Decimal("2890.05"),
                    currency="INR",
                    executed_at=T,
                    session="London",
                    is_simulated=True,
                )
            )
            db.add(
                Agent(
                    id="dhan-feed",
                    name="Dhan feed",
                    kind="trading",
                    state="idle",
                    last_heartbeat_at=func.now(),
                )
            )
            inst_id = inst.id

        found = (await env.user.get("/api/trading/instruments?q=relia&broker=dhan")).json()
        assert found["total"] == 1 and found["items"][0]["tick_size"] == "0.05000000"
        assert (await env.user.get("/api/trading/instruments?q=%25")).json()["total"] == 1

        url = f"/api/trading/candles?instrument_id={inst_id}&timeframe=15m"
        all_c = (await env.user.get(url)).json()
        assert len(all_c) == 5 and all_c[0]["ts"] < all_c[-1]["ts"]
        assert all_c[0]["session"] == "London" and all_c[0]["open"] == "2890.05000000"
        start = (T + timedelta(minutes=15)).isoformat()
        end = (T + timedelta(minutes=45)).isoformat()
        base = {"instrument_id": inst_id, "timeframe": "15m"}
        part = (
            await env.user.get("/api/trading/candles", params={**base, "start": start, "end": end})
        ).json()
        assert len(part) == 2
        last2 = (await env.user.get(f"{url}&limit=2")).json()
        assert [c["ts"] for c in last2] == [c["ts"] for c in all_c[-2:]]
        naive = await env.user.get(
            "/api/trading/candles", params={**base, "start": "2026-11-02T09:00:00"}
        )
        assert naive.status_code == 422

        strats = (await env.user.get("/api/trading/strategies")).json()
        assert [v["version"] for v in strats[0]["versions"]] == [2, 1]
        sig = (await env.user.get("/api/trading/signals?side=BUY")).json()
        assert sig["total"] == 1
        assert (await env.user.get("/api/trading/signals?side=SELL")).json()["total"] == 0

        ex = (await env.user.get("/api/trading/executions")).json()
        assert ex["total"] == 1 and ex["items"][0]["source"] == "simulated"
        assert (await env.other.get("/api/trading/executions")).json()["total"] == 0

        agents = (await env.user.get("/api/trading/agents")).json()
        assert [a["id"] for a in agents] == ["dhan-feed"] and agents[0]["state"] == "idle"
        assert (await env.user.get("/api/trading/agents/dhan-feed/status")).status_code == 200
        assert (await env.user.get("/api/trading/agents/ollama/status")).status_code == 404
        assert (await env.user.get("/api/trading/agents/dhan-feed/logs")).status_code == 403
        assert (await env.admin.get("/api/trading/agents/dhan-feed/logs")).json()["total"] == 0


async def test_every_trading_endpoint_requires_auth(make_env: EnvFactory) -> None:
    async with make_env() as env:
        anon = AsyncClient(transport=ASGITransport(app=env.app), base_url="https://test")
        for path in (
            "accounts",
            "instruments",
            "candles?instrument_id=1&timeframe=1m",
            "quotes?broker=dhan&symbols=X",
            "strategies",
            "signals",
            "executions",
            "agents",
        ):
            assert (await anon.get(f"/api/trading/{path}")).status_code == 401, path
        await anon.aclose()
        assert BrokerAccount.__table__.c.mode.server_default is not None


async def test_brokers_lists_instrument_counts_per_segment(make_env: EnvFactory) -> None:
    async with make_env() as env:
        async with env.maker() as db, db.begin():
            for n, (seg, active) in enumerate(
                [("NSE_EQ", True), ("NSE_EQ", True), ("NSE_FNO", True), ("BSE_EQ", False)]
            ):
                db.add(
                    Instrument(
                        broker="dhan",
                        broker_instrument_id=f"{seg}:{n}",
                        symbol=f"S{n}",
                        segment=seg,
                        instrument_type="EQUITY",
                        currency="INR",
                        lot_size=Decimal(1),
                        tick_size=Decimal("0.05"),
                        is_active=active,
                    )
                )
        res = await env.user.get("/api/trading/brokers")
        assert res.status_code == 200
        by = {b["broker"]: b for b in res.json()}
        assert set(by) == {"dhan", "mt5"}
        # Inactive (expired, delisted) instruments are not counted.
        assert by["dhan"]["instruments"] == 3
        assert by["dhan"]["segments"] == [
            {"segment": "NSE_EQ", "instruments": 2},
            {"segment": "NSE_FNO", "instruments": 1},
        ]
        assert by["mt5"]["instruments"] == 0 and by["mt5"]["segments"] == []
        # The server-side filters the browser now uses.
        page = (await env.user.get("/api/trading/instruments?broker=dhan&segment=NSE_FNO")).json()
        assert page["total"] == 1
        anon = AsyncClient(transport=ASGITransport(app=env.app), base_url="https://test")
        async with anon:
            assert (await anon.get("/api/trading/brokers")).status_code == 401
