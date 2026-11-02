"""Finance (Prompt 3) against the DB: CRUD, exact decimals, and ownership on every endpoint."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.bootstrap import bootstrap_admin
from app.auth.security import hash_password
from app.core.config import Settings
from app.main import create_app
from app.models.auth import AuditEvent, User

pytestmark = pytest.mark.db
PASSWORD = "correct horse battery staple"


class Env:
    def __init__(self, app: FastAPI, clients: dict[str, AsyncClient], csrf: dict[str, str],
                 ids: dict[str, Any]) -> None:  # fmt: skip
        self.app, self.clients, self.csrf, self.ids = app, clients, csrf, ids

    @property
    def maker(self) -> async_sessionmaker[AsyncSession]:
        m: async_sessionmaker[AsyncSession] = self.app.state.sessionmaker
        return m

    def send(self, who: str, method: str, url: str, **kw: Any) -> Any:
        return self.clients[who].request(
            method, url, headers={"x-csrf-token": self.csrf[who]}, **kw
        )

    def get(self, who: str, url: str, **kw: Any) -> Any:
        return self.clients[who].get(url, **kw)

    async def sql(self, stmt: str, **params: Any) -> Any:
        async with self.maker() as db, db.begin():
            r = await db.execute(text(stmt), params)
            return r.all() if r.returns_rows else []


EnvFactory = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


@pytest.fixture
def make_env(db_clean: str, settings: Settings) -> EnvFactory:
    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[Env]:
        engine = create_async_engine(db_clean)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db, db.begin():
            for t in ("trades", "expenses", "secured", "astra_documents", "portfolio_profiles"):
                await db.execute(text(f"DELETE FROM public.{t}"))
        admin = await bootstrap_admin(maker, "admin@spasta.online", PASSWORD)
        ids: dict[str, Any] = {"admin": admin.id}
        async with maker() as db, db.begin():
            for who in ("a", "b"):
                u = User(
                    email=f"{who}@spasta.online", password_hash=hash_password(PASSWORD), role_id=2
                )
                db.add(u)
                await db.flush()
                ids[who] = u.id
        await engine.dispose()
        app = create_app(settings)
        async with LifespanManager(app) as mgr:
            t = ASGITransport(app=mgr.app)
            clients = {
                w: AsyncClient(transport=t, base_url="https://test") for w in ("admin", "a", "b")
            }
            csrf = {}
            for who, c in clients.items():
                email = "admin@spasta.online" if who == "admin" else f"{who}@spasta.online"
                r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
                csrf[who] = r.json()["csrf_token"]
            try:
                yield Env(app, clients, csrf, ids)
            finally:
                for c in clients.values():
                    await c.aclose()

    return factory


TRADE = {
    "id": "t-0001",
    "ist_date": "2026-10-15",
    "ist_time": "13:05",
    "symbol": "XAUUSD",
    "session": "London",
    "side": "buy",
    "lot": "0.1",
    "entry": "2650.10",
    "exit": "2655.35",
    "move": "5.25",
    "pl": "52.5",
    "note": "breakout",
}


async def test_trade_crud_keeps_decimals_exact(make_env: EnvFactory) -> None:
    async with make_env() as env:
        r = await env.send("a", "POST", "/api/finance/trades", json=TRADE)
        assert r.status_code == 201, r.text
        body = r.json()
        assert (body["id"], body["lot"], body["entry"], body["pl"]) == (
            "t-0001", "0.1", "2650.1", "52.5"
        )  # fmt: skip
        # Client-minted id is kept; a replay is a 409, not a duplicate.
        again = await env.send("a", "POST", "/api/finance/trades", json=TRADE)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "exists"
        # A server id when none is given.
        second = {**TRADE, "id": None, "ist_date": "2026-09-01", "pl": "-10.05"}
        r2 = await env.send("a", "POST", "/api/finance/trades", json=second)
        assert r2.status_code == 201 and len(r2.json()["id"]) == 32

        page = (await env.get("a", "/api/finance/trades?limit=1")).json()
        assert page["total"] == 2 and [i["id"] for i in page["items"]] == ["t-0001"]
        oct_only = (
            await env.get("a", "/api/finance/trades?date_from=2026-10-01&date_to=2026-10-31")
        ).json()
        assert [i["id"] for i in oct_only["items"]] == ["t-0001"]
        assert (await env.get("a", "/api/finance/trades?symbol=EURUSD")).json()["total"] == 0

        r = await env.send("a", "PATCH", "/api/finance/trades/t-0001", json={"pl": "60.1"})
        assert r.status_code == 200 and r.json()["pl"] == "60.1"
        assert r.json()["symbol"] == "XAUUSD"
        totals = (await env.get("a", "/api/finance/totals")).json()
        assert totals["trades"] == 2 and totals["trade_pl"] == "50.05"

        assert (await env.send("a", "DELETE", "/api/finance/trades/t-0001")).status_code == 204
        assert (await env.get("a", "/api/finance/trades/t-0001")).status_code == 404

        # Stored owner columns: legacy text owner + the Aureon owner.
        rows = await env.sql("select user_id, aureon_user_id from public.trades")
        assert rows == [(str(env.ids["a"]), env.ids["a"])]


async def test_every_endpoint_is_scoped_to_its_owner(make_env: EnvFactory) -> None:
    async with make_env() as env:
        made = {
            "trades": (await env.send("a", "POST", "/api/finance/trades", json=TRADE)).json(),
            "expenses": (
                await env.send(
                    "a", "POST", "/api/finance/expenses",
                    json={"id": "e1", "date": "2026-10-02", "amount": "120.50", "category": "Food"},
                )
            ).json(),
            "secured": (
                await env.send("a", "POST", "/api/finance/secured",
                               json={"id": "s1", "date": "2026-10-03", "amt": "1000"})
            ).json(),
        }  # fmt: skip
        assert all("id" in v for v in made.values()), made
        for name, row in made.items():
            url = f"/api/finance/{name}/{row['id']}"
            assert (await env.get("b", url)).status_code == 404
            assert (await env.send("b", "PATCH", url, json={"note": "mine now"})).status_code == 404
            assert (await env.send("b", "DELETE", url)).status_code == 404
            assert (await env.get("b", f"/api/finance/{name}")).json()["total"] == 0
            # Admins get no extra visibility: finance is personal.
            assert (await env.get("admin", url)).status_code == 404
            assert (await env.get("a", url)).status_code == 200
        b_totals = (await env.get("b", "/api/finance/totals")).json()
        assert (b_totals["trades"], b_totals["expenses"], b_totals["secured"]) == (0, "0", "0")
        a_totals = (await env.get("a", "/api/finance/totals")).json()
        assert (a_totals["expenses"], a_totals["secured"]) == ("120.5", "1000")
        # B can reuse the same client id: ids are per owner.
        r = await env.send("b", "POST", "/api/finance/trades", json=TRADE)
        assert r.status_code == 201
        # CSRF is required on writes.
        r = await env.clients["a"].post("/api/finance/trades", json={**TRADE, "id": "t-x"})
        assert r.status_code == 403
        # Signed out: 401.
        anon = AsyncClient(transport=ASGITransport(app=env.app), base_url="https://test")
        assert (await anon.get("/api/finance/trades")).status_code == 401
        await anon.aclose()


async def test_validation(make_env: EnvFactory) -> None:
    async with make_env() as env:
        bad = [
            ("expenses", {"date": "2026-10-02", "amount": "-1"}),
            ("expenses", {"date": "2026-10-02", "kind": "recurring"}),
            ("secured", {"date": "2026-10-02", "amt": "abc"}),
            ("trades", {**TRADE, "session": "Tokyo"}),
            ("trades", {**TRADE, "side": "long"}),
            ("trades", {**TRADE, "user_id": "someone-else"}),  # owner is never client-chosen
            ("trades", {**TRADE, "id": "bad id/../x"}),
        ]
        for name, body in bad:
            r = await env.send("a", "POST", f"/api/finance/{name}", json=body)
            assert r.status_code == 422, (name, body, r.text)
        ok = await env.send("a", "POST", "/api/finance/expenses",
                            json={"id": "rent", "date": "2026-10-01", "amount": "15000",
                                  "kind": "recurring", "recur_day": 1})  # fmt: skip
        assert ok.status_code == 201
        r = await env.send("a", "PATCH", "/api/finance/expenses/rent", json={"recur_day": None})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "recur_day_required"


async def test_legacy_rows_need_a_claim(make_env: EnvFactory) -> None:
    async with make_env() as env:
        # Rows the old app wrote: legacy owner id only, no Aureon owner.
        await env.sql(
            "insert into public.trades (user_id, id, ist_date, pl) values "
            "('firebase-uid-1', 'old1', '2026-08-01', 12.5), "
            "('firebase-uid-2', 'other', '2026-08-01', 1)"
        )
        await env.sql(
            "insert into public.astra_documents (namespace, doc_id, user_id, data) values "
            "('aureon-notes', 'firebase-uid-1', 'firebase-uid-1', '{\"n\": 1}')"
        )
        assert (await env.get("a", "/api/finance/trades")).json()["total"] == 0
        body = {"legacy_user_id": "firebase-uid-1", "user_id": str(env.ids["a"])}
        assert (await env.send("a", "POST", "/api/finance/claim", json=body)).status_code == 403

        dry = (await env.send("admin", "POST", "/api/finance/claim", json=body)).json()
        assert dry["applied"] is False and dry["rows"]["trades"] == 1
        assert dry["rows"]["astra_documents"] == 1
        assert (await env.get("a", "/api/finance/trades")).json()["total"] == 0  # dry run

        done = (
            await env.send("admin", "POST", "/api/finance/claim", json={**body, "apply": True})
        ).json()
        assert done["applied"] is True
        listed = (await env.get("a", "/api/finance/trades")).json()
        assert [i["id"] for i in listed["items"]] == ["old1"] and listed["items"][0]["pl"] == "12.5"
        doc = await env.get("a", "/api/finance/documents/aureon-notes/firebase-uid-1")
        assert doc.status_code == 200 and doc.json()["data"] == {"n": 1}

        # New rows now use the legacy owner id, so the old app keeps seeing them.
        await env.send("a", "POST", "/api/finance/trades", json=TRADE)
        rows = await env.sql("select user_id from public.trades where id = 't-0001'")
        assert rows == [("firebase-uid-1",)]

        # The legacy id cannot be handed to another user.
        clash = await env.send("admin", "POST", "/api/finance/claim",
                               json={"legacy_user_id": "firebase-uid-1",
                                     "user_id": str(env.ids["b"]), "apply": True})  # fmt: skip
        assert clash.status_code == 409
        assert (await env.get("b", "/api/finance/trades")).json()["total"] == 0
        async with env.maker() as db:
            events = list(await db.scalars(select(AuditEvent.event_type)))
        assert events.count("finance_rows_claimed") == 1


async def test_documents_set_merge_and_ownership(make_env: EnvFactory) -> None:
    async with make_env() as env:
        url = "/api/finance/documents/astra-goals/g1"
        r = await env.send("a", "PUT", url, json={"data": {"title": "Save", "target": 10}})
        assert r.status_code == 200 and r.json()["data"] == {"title": "Save", "target": 10}
        r = await env.send("a", "PUT", url, json={"data": {"target": 20}, "merge": True})
        assert r.json()["data"] == {"title": "Save", "target": 20}
        r = await env.send("a", "PUT", url, json={"data": {"only": True}})
        assert r.json()["data"] == {"only": True}
        # B can neither read nor overwrite A's document (same answer as missing).
        assert (await env.get("b", url)).status_code == 404
        assert (await env.send("b", "PUT", url, json={"data": {"x": 1}})).status_code == 404
        assert (await env.send("b", "DELETE", url)).status_code == 404
        assert (await env.get("b", "/api/finance/documents/astra-goals")).json()["total"] == 0
        assert (await env.get("a", "/api/finance/documents/astra-goals")).json()["total"] == 1
        assert (await env.send("a", "DELETE", url)).status_code == 204
        assert (await env.get("a", url)).status_code == 404


async def test_portfolio_is_admin_edited(make_env: EnvFactory) -> None:
    async with make_env() as env:
        url = "/api/finance/portfolio-profiles/hithesh"
        assert (await env.get("a", url)).status_code == 404
        r = await env.send("a", "PUT", url, json={"content": {"headline": "x"}})
        assert r.status_code == 403
        r = await env.send("admin", "PUT", url, json={"content": {"headline": "Builder"}})
        assert r.status_code == 200
        assert (await env.get("a", url)).json()["content"] == {"headline": "Builder"}
