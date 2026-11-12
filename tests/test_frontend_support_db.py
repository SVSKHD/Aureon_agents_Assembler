"""Frontend support (D-backend) against the DB: change sync + tombstones, sessions,
receipt attachments, public shares / forex, and the news pull job."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.services import news
from app.workers.scheduler import TaskContext, purge_agent_history
from tests import test_finance_db as fin_db
from tests.test_finance_db import PASSWORD, TRADE, EnvFactory

make_env = fin_db.make_env  # the shared fixture (two users + admin, signed in)

pytestmark = pytest.mark.db

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"x" * 64
MAX_BYTES = 4096


@pytest.fixture
def settings(data_root: Path) -> Settings:
    from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL

    return Settings(
        ENV="test",
        DATABASE_URL=TEST_DATABASE_URL or "postgresql+asyncpg://x:x@127.0.0.1:1/none",
        REDIS_URL=TEST_REDIS_URL,
        DATA_ROOT=data_root,
        SECRET_KEY="t" * 40,
        HEALTH_CHECK_TIMEOUT=1.0,
        FINANCE_ATTACHMENT_MAX_BYTES=MAX_BYTES,
        PUBLIC_RATE_LIMIT_PER_MINUTE=5,
    )


def _since(dt: datetime) -> str:
    return quote(dt.isoformat())


# --- change sync ----------------------------------------------------------------------------


async def test_updated_since_changes_and_tombstones(make_env: EnvFactory) -> None:
    async with make_env() as env:
        assert (await env.send("a", "POST", "/api/finance/trades", json=TRADE)).status_code == 201
        exp = {"id": "e1", "date": "2026-10-02", "amount": "12.5", "category": "food"}
        assert (await env.send("a", "POST", "/api/finance/expenses", json=exp)).status_code == 201
        r = await env.send(
            "a", "PUT", "/api/finance/documents/astra-goals/g1", json={"data": {"x": 1}}
        )
        assert r.status_code == 200
        assert (await env.send("b", "POST", "/api/finance/trades", json=TRADE)).status_code == 201

        first = (
            await env.get(
                "a", f"/api/finance/changes?since={_since(datetime(2000, 1, 1, tzinfo=UTC))}"
            )
        ).json()
        assert [t["id"] for t in first["trades"]] == ["t-0001"]
        assert [e["id"] for e in first["expenses"]] == ["e1"]
        assert [(d["namespace"], d["doc_id"]) for d in first["documents"]] == [
            ("astra-goals", "g1")
        ]
        assert first["deleted"] == [] and first["truncated"] is False
        cursor = first["cursor"]

        await asyncio.sleep(0.01)
        r = await env.send("a", "PATCH", "/api/finance/expenses/e1", json={"amount": "13"})
        assert r.status_code == 200
        assert (await env.send("a", "DELETE", "/api/finance/trades/t-0001")).status_code == 204
        r = await env.send("a", "DELETE", "/api/finance/documents/astra-goals/g1")
        assert r.status_code == 204

        nxt = (await env.get("a", f"/api/finance/changes?since={quote(cursor)}")).json()
        assert nxt["trades"] == [] and nxt["documents"] == []
        assert [e["id"] for e in nxt["expenses"]] == ["e1"]
        assert sorted((d["kind"], d["ref"]) for d in nxt["deleted"]) == [
            ("document", "astra-goals/g1"),
            ("trades", "t-0001"),
        ]
        # The other user's delete feed is untouched.
        b = (await env.get("b", f"/api/finance/changes?since={quote(cursor)}")).json()
        assert b["deleted"] == [] and b["trades"] == []

        # updated_since on the list endpoints.
        lst = (await env.get("a", f"/api/finance/expenses?updated_since={quote(cursor)}")).json()
        assert lst["total"] == 1
        later = datetime.now(UTC) + timedelta(seconds=5)
        lst = (await env.get("a", f"/api/finance/expenses?updated_since={_since(later)}")).json()
        assert lst["total"] == 0
        await env.send("a", "PUT", "/api/finance/documents/astra-goals/g2", json={"data": {}})
        docs = await env.get(
            "a", f"/api/finance/documents/astra-goals?updated_since={quote(cursor)}"
        )
        assert [d["doc_id"] for d in docs.json()["items"]] == ["g2"]

        # A naive timestamp is refused (ambiguous zone).
        assert (
            await env.get("a", "/api/finance/changes?since=2026-01-01T00:00:00")
        ).status_code == 422

        # Tombstones older than the retention are pruned by purge-agent-history.
        await env.sql("update core.finance_tombstones set deleted_at = now() - interval '31 days' "
                      "where kind = 'trades'")  # fmt: skip
        out = await purge_agent_history(TaskContext(env.app.state.settings, env.maker, None))
        assert out["tombstones_deleted"] == 1
        left = await env.sql("select kind from core.finance_tombstones")
        assert [r[0] for r in left] == ["document"]


# --- sessions -------------------------------------------------------------------------------


async def test_sessions_list_and_single_revoke(make_env: EnvFactory) -> None:
    async with make_env() as env:
        other = AsyncClient(transport=ASGITransport(app=env.app), base_url="https://test")
        r = await other.post(
            "/api/auth/login", json={"email": "a@spasta.online", "password": PASSWORD}
        )
        assert r.status_code == 200
        other_csrf = r.json()["csrf_token"]

        rows = (await env.get("a", "/api/auth/sessions")).json()
        assert len(rows) == 2 and sum(r["current"] for r in rows) == 1
        assert set(rows[0]) == {"id", "created_at", "expires_at", "ip", "user_agent", "current"}
        mine = next(r["id"] for r in rows if r["current"])
        theirs = next(r["id"] for r in rows if not r["current"])

        b_rows = (await env.get("b", "/api/auth/sessions")).json()
        b_id = b_rows[0]["id"]
        # Another user's session, or an unknown id, is 404; CSRF is required.
        assert (await env.send("a", "DELETE", f"/api/auth/sessions/{b_id}")).status_code == 404
        unknown = "00000000-0000-0000-0000-000000000000"
        assert (await env.send("a", "DELETE", f"/api/auth/sessions/{unknown}")).status_code == 404
        assert (await env.clients["a"].delete(f"/api/auth/sessions/{theirs}")).status_code == 403
        assert (await env.get("b", "/api/auth/me")).status_code == 200

        assert (await env.send("a", "DELETE", f"/api/auth/sessions/{theirs}")).status_code == 204
        assert (await other.get("/api/auth/me")).status_code == 401
        assert (await env.send("a", "DELETE", f"/api/auth/sessions/{theirs}")).status_code == 404
        rows = (await env.get("a", "/api/auth/sessions")).json()
        assert [r["id"] for r in rows] == [mine]
        audit = await env.sql(
            "select details->>'session_id' from audit.audit_events "
            "where event_type = 'session_revoked'"
        )
        assert [a[0] for a in audit] == [theirs]
        del other_csrf
        await other.aclose()


# --- attachments ----------------------------------------------------------------------------


def _upload(env, who: str, ref: str = "t-0001", body: bytes = PNG, ctype: str = "image/png",
            kind: str = "trades", name: str = "receipt.png"):  # type: ignore[no-untyped-def]  # fmt: skip
    return env.send(
        who, "POST", "/api/finance/attachments",
        data={"kind": kind, "ref": ref}, files={"file": (name, body, ctype)},
    )  # fmt: skip


async def test_attachments_owner_scoped_sniffed_and_limited(
    make_env: EnvFactory, data_root: Path
) -> None:
    async with make_env() as env:
        assert (await env.send("a", "POST", "/api/finance/trades", json=TRADE)).status_code == 201
        r = await _upload(env, "a", name="../../etc/passwd.png")
        assert r.status_code == 201, r.text
        meta = r.json()
        att_id = meta["id"]
        assert meta["content_type"] == "image/png" and meta["size_bytes"] == len(PNG)
        assert meta["filename"] == "passwd.png" and "storage_path" not in meta
        stored = data_root / "finance-attachments" / str(env.ids["a"]) / att_id
        assert stored.read_bytes() == PNG

        f = await env.get("a", f"/api/finance/attachments/{att_id}/file")
        assert f.status_code == 200 and f.content == PNG
        assert f.headers["content-type"] == "image/png"
        assert f.headers["x-content-type-options"] == "nosniff"
        assert f.headers["content-disposition"].startswith("attachment")
        lst = (await env.get("a", "/api/finance/attachments?kind=trades&ref=t-0001")).json()
        assert [x["id"] for x in lst] == [att_id]

        # Another user: 404 on every route, nothing listed, and no upload against a's row.
        assert (await env.get("b", f"/api/finance/attachments/{att_id}")).status_code == 404
        assert (await env.get("b", f"/api/finance/attachments/{att_id}/file")).status_code == 404
        r = await env.send("b", "DELETE", f"/api/finance/attachments/{att_id}")
        assert r.status_code == 404
        assert (await env.get("b", "/api/finance/attachments?kind=trades&ref=t-0001")).json() == []
        assert (await _upload(env, "b")).status_code == 404
        assert (await _upload(env, "a", ref="nope")).status_code == 404
        assert (await _upload(env, "a", kind="bogus")).status_code == 422

        # Sniffing: a "PNG" that is really HTML is refused; a real PDF is accepted.
        fake = await _upload(env, "a", body=b"<html><script>alert(1)</script></html>")
        assert fake.status_code == 415
        pdf = await _upload(env, "a", body=PDF, ctype="image/png", name="x.png")
        assert pdf.status_code == 201 and pdf.json()["content_type"] == "application/pdf"
        # Size limit.
        big = await _upload(env, "a", body=PNG + b"\x00" * MAX_BYTES)
        assert big.status_code == 413
        # CSRF.
        no_csrf = await env.clients["a"].post(
            "/api/finance/attachments", data={"kind": "trades", "ref": "t-0001"},
            files={"file": ("r.png", PNG, "image/png")},
        )  # fmt: skip
        assert no_csrf.status_code == 403

        # Delete one; then deleting the row removes the rest (records and files).
        assert (
            await env.send("a", "DELETE", f"/api/finance/attachments/{att_id}")
        ).status_code == 204
        assert not stored.exists()
        assert (await env.get("a", f"/api/finance/attachments/{att_id}")).status_code == 404
        pdf_file = data_root / "finance-attachments" / str(env.ids["a"]) / pdf.json()["id"]
        assert pdf_file.exists()
        assert (await env.send("a", "DELETE", "/api/finance/trades/t-0001")).status_code == 204
        assert not pdf_file.exists()
        assert (await env.get("a", "/api/finance/attachments")).json() == []


async def test_attachment_per_user_cap(make_env: EnvFactory, settings: Settings) -> None:
    settings.FINANCE_ATTACHMENTS_MAX_PER_USER = 1
    async with make_env() as env:
        assert (await env.send("a", "POST", "/api/finance/trades", json=TRADE)).status_code == 201
        assert (await _upload(env, "a")).status_code == 201
        r = await _upload(env, "a")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "attachment_limit"


# --- public shares and forex ----------------------------------------------------------------


async def test_public_shares_and_forex(make_env: EnvFactory) -> None:
    async with make_env() as env:
        anon = AsyncClient(transport=ASGITransport(app=env.app), base_url="https://test")
        share = {
            "isPublic": True,
            "title": "Q3",
            "ownerId": "uid-1",
            "nested": {"userId": "uid-1", "keep": 1},
            "rows": [{"ownerId": "x", "v": 2}],
        }
        r = await env.send(
            "a", "PUT", "/api/finance/documents/aureon-shares/s1", json={"data": share}
        )
        assert r.status_code == 200
        r = await env.send(
            "a",
            "PUT",
            "/api/finance/documents/aureon-shares/s2",
            json={"data": {"isPublic": False}},
        )
        assert r.status_code == 200

        got = await anon.get("/api/public/shares/s1")
        assert got.status_code == 200
        body = got.json()
        assert set(body) == {"doc_id", "data", "updated_at"}
        assert body["data"] == {
            "isPublic": True,
            "title": "Q3",
            "nested": {"keep": 1},
            "rows": [{"v": 2}],
        }
        assert (await anon.get("/api/public/shares/s2")).status_code == 404
        assert (await anon.get("/api/public/shares/missing")).status_code == 404

        # Unpublish through the documents API (merge).
        r = await env.send("a", "PUT", "/api/finance/documents/aureon-shares/s1",
                           json={"data": {"isPublic": False}, "merge": True})  # fmt: skip
        assert r.status_code == 200
        assert (await anon.get("/api/public/shares/s1")).status_code == 404

        # Forex: only server-written (ownerless) items, newest first.
        for doc_id, day in (("old", "2026-10-01"), ("new", "2026-11-01")):
            await env.sql(
                "insert into public.astra_documents (namespace, doc_id, data, updated_at) "
                "values ('forex', :d, cast(:data as jsonb), now())",
                d=doc_id,
                data=f'{{"title": "{doc_id}", "published": "{day}T00:00:00+00:00"}}',
            )
        r = await env.send(
            "a", "PUT", "/api/finance/documents/forex/private", json={"data": {"t": 1}}
        )
        assert r.status_code == 200
        fx = await anon.get("/api/public/forex?limit=10")
        assert fx.status_code == 200
        assert [d["doc_id"] for d in fx.json()] == ["new", "old"]
        await anon.aclose()


async def test_public_rate_limit(make_env: EnvFactory) -> None:
    async with make_env() as env:
        anon = AsyncClient(transport=ASGITransport(app=env.app), base_url="https://test")
        # Limit 5/min; 11 calls fill at least one fixed window even across a minute boundary.
        resp = [await anon.get("/api/public/forex") for _ in range(11)]
        assert resp[0].status_code == 200
        limited = [r for r in resp if r.status_code == 429]
        assert limited and int(limited[0].headers["retry-after"]) >= 1
        await anon.aclose()


# --- news pull --------------------------------------------------------------------------------

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>FX Desk</title>
<item><title>EUR/USD climbs</title><link>https://news.test/a</link>
<pubDate>Mon, 02 Nov 2026 09:00:00 GMT</pubDate></item>
<item><title>No link</title></item>
</channel></rss>"""


@respx.mock
async def test_news_pull_upserts_forex_documents(make_env: EnvFactory, settings: Settings) -> None:
    respx.get("https://feeds.test/rss").mock(return_value=httpx.Response(200, content=RSS))
    settings.NEWS_FEED_URLS = ["https://feeds.test/rss"]
    async with make_env() as env:
        async with env.maker() as db, db.begin():
            out = await news.pull(db, settings)
        assert out["items"] == 1 and out["written"] == 1 and out["errors"] == {}
        async with env.maker() as db, db.begin():
            again = await news.pull(db, settings)
        assert again["written"] == 0  # unchanged items are not rewritten
        rows = await env.sql(
            "select doc_id, data->>'source', aureon_user_id from public.astra_documents "
            "where namespace = 'forex'"
        )
        import hashlib

        assert rows == [(hashlib.sha1(b"https://news.test/a").hexdigest(), "FX Desk", None)]
        anon = AsyncClient(transport=ASGITransport(app=env.app), base_url="https://test")
        fx = (await anon.get("/api/public/forex")).json()
        assert fx[0]["data"]["title"] == "EUR/USD climbs"
        assert fx[0]["data"]["published"] == "2026-11-02T09:00:00+00:00"
        await anon.aclose()
