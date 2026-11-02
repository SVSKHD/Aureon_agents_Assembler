"""Trading Phase F against the DB: paper intents → risk → admin approval → simulated fill.

Covers the Prompt 12 list: an intent cannot bypass risk, AI output cannot create or approve
intents, the LIVE path stays unreachable, and fills are accounted correctly (fees, P&L,
positions, ledger, reconciliation drift).
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.registry import SCHEDULER, AgentRegistry
from app.agents.trading.base import BrokerAdapter
from app.agents.trading.paper import accounting as acc
from app.auth.bootstrap import bootstrap_admin
from app.auth.security import hash_password
from app.core.config import Settings
from app.main import create_app
from app.models.agents import AgentLog
from app.models.auth import AuditEvent, User
from app.models.brokers import Mt5Symbol
from app.models.trading import (
    BrokerAccount,
    Execution,
    Instrument,
    OrderEvent,
    OrderIntent,
    PaperLedger,
    PositionSnapshot,
    Reconciliation,
    RiskDecision,
    RiskPolicy,
)
from app.services import paper
from app.workers.scheduler import TaskContext, paper_reconcile
from tests.conftest import TEST_REDIS_URL

pytestmark = pytest.mark.db
PASSWORD = "correct horse battery staple"
D = Decimal
REF = "NSE_EQ:1333"
ROOT = Path(__file__).resolve().parents[1]


class Env:
    def __init__(
        self,
        app: FastAPI,
        clients: dict[str, AsyncClient],
        csrf: dict[str, str],
        ids: dict[str, Any],
    ) -> None:
        self.app, self.clients, self.csrf, self.ids = app, clients, csrf, ids
        self.account_id: Any = None
        self.equity: int = 0
        self.option: int = 0
        self.gold: int = 0

    @property
    def maker(self) -> async_sessionmaker[AsyncSession]:
        m: async_sessionmaker[AsyncSession] = self.app.state.sessionmaker
        return m

    @property
    def settings(self) -> Settings:
        s: Settings = self.app.state.settings
        return s

    def send(self, who: str, method: str, url: str, **kw: Any) -> Any:
        return self.clients[who].request(
            method, url, headers={"x-csrf-token": self.csrf[who]}, **kw
        )

    async def tick(self, ltp: str, ref: str = REF, age: float = 0, stale: str = "0") -> None:
        redis = Redis.from_url(TEST_REDIS_URL)
        await redis.hset(
            f"dhan:tick:{ref}",
            mapping={
                "ltp": ltp,
                "received_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
                "stale": stale,
            },
        )
        await redis.aclose()

    async def policy(self, **kw: Any) -> None:
        async with self.maker() as db, db.begin():
            values: dict[str, Any] = {
                "max_daily_loss": D(1000),
                "max_exposure": D(100000),
                "max_position_size": D(100),
                "max_drawdown_pct": D(10),
                "allowed_sessions": ["Asia", "London", "NY"],
                "kill_switch": False,
            }
            values.update(kw)
            existing = await db.scalar(
                select(RiskPolicy).where(RiskPolicy.broker_account_id == self.account_id)
            )
            if existing is None:
                db.add(RiskPolicy(broker_account_id=self.account_id, **values))
            else:
                for k, v in values.items():
                    setattr(existing, k, v)

    async def mode(self, mode: str) -> None:
        async with self.maker() as db, db.begin():
            await db.execute(
                update(BrokerAccount).where(BrokerAccount.id == self.account_id).values(mode=mode)
            )

    async def intent(self, who: str = "user", **kw: Any) -> Any:
        body = {
            "account_id": str(self.account_id),
            "instrument_id": self.equity,
            "side": "BUY",
            "quantity": "10",
            **kw,
        }
        return await self.send(who, "POST", "/api/trading/order-intents", json=body)

    async def approve(self, intent_id: str, who: str = "admin") -> Any:
        return await self.send(who, "POST", f"/api/trading/order-intents/{intent_id}/approve")

    async def count(self, model: Any, *where: Any) -> int:
        async with self.maker() as db:
            q = select(func.count()).select_from(model)
            for w in where:
                q = q.where(w)
            return int(await db.scalar(q) or 0)


EnvFactory = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


async def _clean(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as db, db.begin():
        await db.execute(
            text("TRUNCATE trading.broker_accounts, mt5.symbols, trading.instruments CASCADE")
        )


@pytest.fixture
def make_env(db_clean: str, settings: Settings) -> EnvFactory:
    @contextlib.asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[Env]:
        engine = create_async_engine(db_clean)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        await _clean(maker)
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
            equity = Instrument(
                broker="dhan",
                broker_instrument_id=REF,
                symbol="HDFCBANK",
                segment="NSE_EQ",
                instrument_type="EQUITY",
                currency="INR",
                lot_size=D(1),
                tick_size=D("0.05"),
            )
            option = Instrument(
                broker="dhan",
                broker_instrument_id="NSE_FNO:43210",
                symbol="NIFTY-CE",
                segment="NSE_FNO",
                instrument_type="OPTIDX",
                currency="INR",
                lot_size=D(75),
                tick_size=D("0.05"),
            )
            gold = Instrument(
                broker="mt5",
                broker_instrument_id="XAUUSD",
                symbol="XAUUSD",
                instrument_type="CFD",
                currency="USD",
                lot_size=D("0.01"),
                tick_size=D("0.01"),
            )
            account = BrokerAccount(
                broker="dhan",
                account_id="1100012345",
                base_currency="INR",
                owner_user_id=ids["user"],
                mode="PAPER",
            )
            db.add_all([equity, option, gold, account])
            await db.flush()
            inst_ids = (equity.id, option.id, gold.id)
            account_id = account.id
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
            env = Env(app, clients, csrf, ids)
            env.account_id = account_id
            env.equity, env.option, env.gold = inst_ids
            try:
                yield env
            finally:
                for c in clients.values():
                    await c.aclose()
                await _clean(env.maker)

    return factory


def _events(detail: dict[str, Any]) -> list[str]:
    return [e["event_type"] for e in detail["events"]]


# --- the happy path + accounting ----------------------------------------------------------


async def test_paper_round_trip_fills_books_fees_pnl_and_reconciles(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy()
        await env.tick("1000")
        r = await env.intent(correlation_id="ui-test-0001")
        assert r.status_code == 201, r.text
        intent = r.json()
        assert intent["status"] == "PENDING_RISK" and intent["mode"] == "PAPER"
        assert intent["source"] == "manual"
        assert _events(intent) == ["CREATED", "RISK_PASSED"]
        assert intent["risk_decisions"][0]["approved"] is True
        assert intent["executions"] == []

        # The owner cannot approve their own intent: approval is admin-only.
        assert (await env.approve(intent["id"], who="user")).status_code == 403
        assert await env.count(Execution) == 0

        r = await env.approve(intent["id"])
        assert r.status_code == 200, r.text
        filled = r.json()
        assert filled["status"] == "FILLED"
        assert _events(filled) == ["CREATED", "RISK_PASSED", "APPROVED", "SUBMITTED", "FILLED"]
        assert len(filled["risk_decisions"]) == 2  # re-checked at approval
        (ex,) = filled["executions"]
        # 2 bps spread (half = 1) + 1 bp slippage against a BUY: 1000 → 1000.20.
        assert D(ex["price"]) == D("1000.20")
        buy_fees = acc.dhan_charges(
            "equity", None, "BUY", D("10002"), acc.DEFAULT_DHAN_CHARGES
        ).total
        assert buy_fees == D("4.21")
        assert D(ex["fees"]) == buy_fees
        assert ex["source"] == "simulated" and ex["is_simulated"] is True
        assert ex["broker_trade_id"] == f"paper-{intent['id'].replace('-', '')}"
        fill_event = filled["events"][-1]["payload"]
        assert fill_event["charges"]["total"] == "4.21"
        assert fill_event["price_source"].startswith("dhan feed")

        listed = (
            await env.clients["user"].get(
                "/api/trading/executions", params={"account_id": str(env.account_id)}
            )
        ).json()
        assert [i["id"] for i in listed["items"]] == [ex["id"]]

        # Sell it back 10 rupees higher: 1010 → 1009.798 → floor to tick 1009.75.
        await env.tick("1010")
        r = await env.intent(side="SELL")
        assert r.status_code == 201, r.text
        r = await env.approve(r.json()["id"])
        assert r.json()["status"] == "FILLED", r.text
        sell = r.json()["executions"][0]
        assert D(sell["price"]) == D("1009.75")
        sell_fees = acc.dhan_charges(
            "equity", None, "SELL", D("10097.5"), acc.DEFAULT_DHAN_CHARGES
        ).total
        assert D(sell["fees"]) == sell_fees

        async with env.maker() as db:
            ledger = (
                await db.execute(
                    select(PaperLedger.kind, func.sum(PaperLedger.amount)).group_by(
                        PaperLedger.kind
                    )
                )
            ).all()
            last = await db.scalar(select(func.max(PositionSnapshot.as_of)))
            snaps = (
                await db.scalars(
                    select(PositionSnapshot).where(
                        PositionSnapshot.as_of == last, PositionSnapshot.source == "simulated"
                    )
                )
            ).all()
        assert dict(ledger) == {"realized": D("95.50"), "fees": -(buy_fees + sell_fees)}
        assert [(s.quantity, s.realized_pnl) for s in snaps] == [(D(0), D("95.50"))]

        summary = await env.clients["user"].get(f"/api/trading/accounts/{env.account_id}/paper")
        body = summary.json()
        assert body["realized_pnl"] == "95.5"
        assert D(body["fees"]) == buy_fees + sell_fees
        assert D(body["equity"]) == D(1000000) + D("95.50") - buy_fees - sell_fees
        assert body["positions"] == [] and body["source"] == "simulated"

        r = await env.send(
            "admin", "POST", f"/api/trading/accounts/{env.account_id}/paper/reconcile"
        )
        assert r.status_code == 200 and r.json()["ok"] is True, r.text
        assert "order_intent_approved" in [e async for e in _audit(env)] and "paper_reconciled" in [
            e async for e in _audit(env)
        ]


async def _audit(env: Env) -> AsyncIterator[str]:
    async with env.maker() as db:
        for e in await db.scalars(select(AuditEvent.event_type).order_by(AuditEvent.id)):
            yield e


async def test_correlation_id_is_idempotent(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy()
        await env.tick("1000")
        first = await env.intent(correlation_id="strategy-abc-1")
        again = await env.intent(correlation_id="strategy-abc-1")
        assert (first.status_code, again.status_code) == (201, 200)
        assert first.json()["id"] == again.json()["id"]
        assert await env.count(OrderIntent) == 1
        assert await env.count(OrderEvent) == 2  # no duplicate lifecycle events
        clash = await env.intent(correlation_id="strategy-abc-1", quantity="5")
        assert clash.status_code == 409 and clash.json()["detail"]["code"] == "correlation_conflict"


async def test_lot_sizes_and_order_shape_are_enforced(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy()
        r = await env.intent(instrument_id=env.option, quantity="100")
        assert r.status_code == 422 and "multiple of 75 units" in r.json()["detail"]["detail"]
        r = await env.intent(order_type="LIMIT")
        assert r.json()["detail"]["code"] == "limit_price_required"
        r = await env.intent(instrument_id=env.gold)
        assert r.json()["detail"]["code"] == "broker_mismatch"
        r = await env.intent(quantity="-1")
        assert r.status_code == 422
        assert await env.count(OrderIntent) == 0


# --- risk cannot be bypassed --------------------------------------------------------------


async def test_intent_cannot_bypass_risk(make_env: EnvFactory) -> None:
    async with make_env() as env:
        # No policy → rejected at creation; approving it afterwards is refused.
        await env.tick("1000")
        r = await env.intent()
        rejected = r.json()
        assert rejected["status"] == "REJECTED"
        assert "policy_present" in rejected["risk_decisions"][0]["reason"]
        r = await env.approve(rejected["id"])
        assert r.status_code == 409 and r.json()["detail"]["code"] == "not_pending"

        # Kill switch, too large, stale price: all rejected, nothing filled.
        await env.policy(kill_switch=True)
        assert (await env.intent()).json()["status"] == "REJECTED"
        await env.policy(max_position_size=D(5))
        bad = (await env.intent()).json()
        assert bad["status"] == "REJECTED" and "position_size" in bad["risk_decisions"][0]["reason"]
        await env.policy()
        await env.tick("1000", age=3600)
        stale = (await env.intent()).json()
        assert "price_fresh" in stale["risk_decisions"][0]["reason"]
        await env.tick("1000", stale="1")
        assert (await env.intent()).json()["status"] == "REJECTED"

        # Passed at creation, but the kill switch is on by approval time → re-check rejects.
        await env.tick("1000")
        ok = (await env.intent()).json()
        assert ok["status"] == "PENDING_RISK"
        await env.policy(kill_switch=True)
        r = await env.approve(ok["id"])
        assert r.json()["status"] == "REJECTED"
        assert _events(r.json())[-1] == "RISK_REJECTED"
        assert await env.count(Execution) == 0

        # The Execution Agent itself refuses unapproved intents, and an APPROVED status
        # forged straight into the DB without an approving risk decision.
        await env.policy()
        pending = (await env.intent()).json()
        async with env.maker() as db, db.begin():
            intent = await db.get(OrderIntent, pending["id"])
            assert intent is not None
            with pytest.raises(paper.IntentRefused, match="not approved"):
                await paper.execute(db, None, {}, env.settings, intent)
            await db.execute(
                update(RiskDecision)
                .where(RiskDecision.order_intent_id == intent.id)
                .values(approved=False)
            )
            intent.status = "APPROVED"
            with pytest.raises(paper.IntentRefused) as exc:
                await paper.execute(db, None, {}, env.settings, intent)
            assert exc.value.code == "risk_not_approved"
        assert await env.count(Execution) == 0


async def test_daily_loss_limit_blocks_new_intents(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy(max_daily_loss=D(50))
        async with env.maker() as db, db.begin():
            db.add(
                PaperLedger(
                    broker_account_id=env.account_id,
                    instrument_id=env.equity,
                    kind="realized",
                    amount=D("-60"),
                    currency="INR",
                )
            )
        await env.tick("1000")
        r = (await env.intent()).json()
        assert r["status"] == "REJECTED" and "daily_loss" in r["risk_decisions"][0]["reason"]


# --- LIVE stays unreachable --------------------------------------------------------------


async def test_live_path_is_unreachable(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy()
        await env.tick("1000")
        await env.mode("OBSERVE")
        r = await env.intent()
        assert r.status_code == 409 and r.json()["detail"]["code"] == "account_not_paper"
        await env.mode("LIVE")
        r = await env.intent()
        assert r.status_code == 409 and r.json()["detail"]["code"] == "live_unreachable"

        # A PAPER intent whose account was later flipped to LIVE is never executed.
        await env.mode("PAPER")
        made = (await env.intent()).json()
        await env.mode("LIVE")
        r = await env.approve(made["id"])
        assert r.status_code == 409 and r.json()["detail"]["code"] == "live_unreachable"
        async with env.maker() as db, db.begin():
            intent = await db.get(OrderIntent, made["id"])
            assert intent is not None
            intent.mode, intent.status = "LIVE", "APPROVED"
            with pytest.raises(paper.LiveUnreachable):
                await paper.execute(db, None, {}, env.settings, intent)
            with pytest.raises(paper.LiveUnreachable):
                await paper.approve_and_execute(
                    db, None, {}, env.settings, intent, env.ids["admin"]
                )
        assert await env.count(Execution) == 0
        assert "order_intent_approve_denied" in [e async for e in _audit(env)]


def test_broker_interface_has_no_order_methods() -> None:
    names = {n for n, _ in inspect.getmembers(BrokerAdapter, inspect.isfunction)}
    forbidden = {"place_order", "modify_order", "cancel_order", "submit_order", "send_order"}
    assert not names & forbidden
    src = (ROOT / "app/services/paper.py").read_text()
    assert ".place_order" not in src and "order_send" not in src


# --- AI cannot create or approve -----------------------------------------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out |= {f"{node.module}.{a.name}" for a in node.names}
        elif isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
    return out


def test_ai_code_cannot_create_or_approve_intents() -> None:
    """Only the paper service constructs OrderIntent rows, and only the paper API and the
    scheduler import it. Nothing under the AI / research agents reaches either."""
    builders = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "app").rglob("*.py")
        if re.search(r"(?<!class )\bOrderIntent\(", p.read_text())
    ]
    assert builders == ["app/services/paper.py"]
    users = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "app").rglob("*.py")
        if "app.services.paper" in _imports(p)
    )
    assert users == ["app/api/v1/paper.py", "app/workers/scheduler.py"]
    ai_modules = [
        *(ROOT / "app/agents/research").rglob("*.py"),
        ROOT / "app/api/v1/ai.py",
        ROOT / "app/workers/research.py",
        ROOT / "app/services/research.py",
    ]
    for ai_mod in ai_modules:
        text = ai_mod.read_text()
        assert "OrderIntent" not in text and "order_intents" not in text, ai_mod


async def test_ai_routes_cannot_reach_intents_and_approve_is_admin_only(
    make_env: EnvFactory,
) -> None:
    async with make_env() as env:
        routes = [
            (sorted(getattr(r, "methods", set())), getattr(r, "path", ""))
            for r in env.app.routes
            if "order-intents" in getattr(r, "path", "")
        ]
        assert all(p.startswith("/api/trading/order-intents") for _, p in routes)
        await env.policy()
        await env.tick("1000")
        made = (await env.intent()).json()
        # No CSRF token (e.g. a background caller) → refused before anything runs.
        r = await env.clients["admin"].post(f"/api/trading/order-intents/{made['id']}/approve")
        assert r.status_code == 403
        # Another non-admin cannot even see it.
        assert (await env.approve(made["id"], who="other")).status_code == 403
        assert (
            await env.clients["other"].get(f"/api/trading/order-intents/{made['id']}")
        ).status_code == 404
        assert await env.count(Execution) == 0


# --- resting LIMITs, expiry, cancel -------------------------------------------------------


async def test_limit_rests_until_marketable_and_day_orders_expire(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy()
        await env.tick("1000")
        made = (await env.intent(order_type="LIMIT", limit_price="990")).json()
        r = (await env.approve(made["id"])).json()
        assert r["status"] == "SUBMITTED" and r["executions"] == []

        async with env.maker() as db, db.begin():
            out = await paper.process_open(db, env.app.state.redis, {}, env.settings)
        assert out == {"filled": 0, "expired": 0, "waiting": 1}

        await env.tick("985")
        async with env.maker() as db, db.begin():
            out = await paper.process_open(db, env.app.state.redis, {}, env.settings)
        assert out["filled"] == 1
        detail = (await env.clients["user"].get(f"/api/trading/order-intents/{made['id']}")).json()
        assert detail["status"] == "FILLED"
        assert D(detail["executions"][0]["price"]) == D("985.20")

        # A DAY intent from a previous market day expires; it is never filled.
        old = (await env.intent(order_type="LIMIT", limit_price="900")).json()
        async with env.maker() as db, db.begin():
            await db.execute(
                update(OrderIntent)
                .where(OrderIntent.id == old["id"])
                .values(created_at=datetime.now(UTC) - timedelta(days=2))
            )
        async with env.maker() as db, db.begin():
            out = await paper.process_open(db, env.app.state.redis, {}, env.settings)
        assert out["expired"] == 1
        detail = (await env.clients["user"].get(f"/api/trading/order-intents/{old['id']}")).json()
        assert detail["status"] == "EXPIRED" and _events(detail)[-1] == "EXPIRED"


async def test_cancel_is_creator_or_admin_while_open(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy()
        await env.tick("1000")
        made = (await env.intent()).json()
        url = f"/api/trading/order-intents/{made['id']}/cancel"
        assert (await env.send("other", "POST", url)).status_code == 404
        r = await env.send("user", "POST", url)
        assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
        assert (await env.send("user", "POST", url)).json()["detail"]["code"] == "not_open"
        assert (await env.approve(made["id"])).json()["detail"]["code"] == "not_pending"


# --- MT5 accounting + reconciliation drift ------------------------------------------------


async def test_mt5_requires_contract_size_and_uses_it(make_env: EnvFactory) -> None:
    async with make_env() as env:
        async with env.maker() as db, db.begin():
            gold = await db.get(Instrument, env.gold)
            assert gold is not None
            with pytest.raises(acc.AccountingError, match="contract size unknown"):
                await paper.instrument_spec(db, gold)
            db.add(
                Mt5Symbol(
                    server="Demo-1",
                    symbol="XAUUSD",
                    digits=2,
                    point=D("0.01"),
                    contract_size=D(100),
                    volume_step=D("0.01"),
                    currency_profit="USD",
                    trading_instrument_id=env.gold,
                )
            )
            await db.flush()
            spec = await paper.instrument_spec(db, gold)
        assert (spec.multiplier, spec.lot_step, spec.currency) == (D(100), D("0.01"), "USD")
        assert acc.pip_value(spec, D("0.1")) == D("0.100")


async def test_reconciliation_flags_drift(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy()
        await env.tick("1000")
        made = (await env.intent()).json()
        assert (await env.approve(made["id"])).json()["status"] == "FILLED"
        await AgentRegistry(env.maker).register(SCHEDULER)
        ctx = TaskContext(env.settings, env.maker, None, redis=env.app.state.redis)
        assert (await paper_reconcile(ctx))["drift"] == []

        # Someone edits the paper position and the money ledger behind the engine's back.
        async with env.maker() as db, db.begin():
            await db.execute(update(PositionSnapshot).values(quantity=D(7)))
            await db.execute(
                update(PaperLedger).where(PaperLedger.kind == "fees").values(amount=D("-1"))
            )
        out = await paper_reconcile(ctx)
        (acct,) = out["drift"]
        fields = sorted(d["field"] for d in acct["items"])
        assert fields == ["fees", "quantity"]
        async with env.maker() as db:
            last = await db.scalar(select(Reconciliation).order_by(Reconciliation.id.desc()))
            log = await db.scalar(select(AgentLog).order_by(AgentLog.id.desc()))
        assert last is not None and last.ok is False
        assert log is not None and "drift" in log.message and log.level == "error"
        summary = (
            await env.clients["user"].get(f"/api/trading/accounts/{env.account_id}/paper")
        ).json()
        assert summary["reconciliation"]["ok"] is False
