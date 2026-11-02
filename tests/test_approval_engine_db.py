"""Phase 20 approval engine against the DB (docs/23-approval-engine.md): policy resolution,
multi-approval, the expiry sweep, drift over several artifacts, the session re-auth stamp,
the event hash chain, Discord notifications, the SSE stream, policies, the audit log API and
voice navigation (which changes nothing)."""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.bootstrap import bootstrap_admin
from app.auth.security import hash_password
from app.core.config import Settings
from app.integrations import discord
from app.main import create_app
from app.models.approvals import ApprovalRequest
from app.models.auth import ROLE_ADMIN, Role, User
from app.services import approval_reauth, approval_stream
from app.services.approvals import REGISTRY, ApprovalGate, ExecContext, ExecutorSpec
from app.services.audit import ClientInfo
from app.workers.scheduler import TaskContext, approval_expire
from tests.conftest import TEST_REDIS_URL
from tests.test_finance_db import PASSWORD, Env

pytestmark = pytest.mark.db

KIND = "test.echo"
WEBHOOK = "https://discord.test/api/webhooks/1/SECRET-WEBHOOK-TOKEN"
SECRET = "ghp_ABCDEFGHIJKLMNOPQRSTUVWX0123456789"

STATE: dict[str, Any] = {"a": {"v": 1}, "b": {"v": 1}, "runs": 0}


async def _exec(ctx: ExecContext, req: ApprovalRequest) -> dict[str, Any]:
    STATE["runs"] += 1
    return {"ok": True}


async def _arts(ctx: ExecContext, req: ApprovalRequest) -> dict[str, Any]:
    return {"a": dict(STATE["a"]), "b": dict(STATE["b"])}


REGISTRY.register(ExecutorSpec(kind=KIND, execute=_exec, current_artifacts=_arts))

EnvFactory = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


@pytest.fixture
def env(db_clean: str, settings: Settings) -> EnvFactory:
    @contextlib.asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[Env]:
        STATE.update(a={"v": 1}, b={"v": 1}, runs=0)
        engine = create_async_engine(db_clean)
        from sqlalchemy.ext.asyncio import async_sessionmaker

        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db, db.begin():
            await db.execute(
                text("DELETE FROM audit.approval_policies WHERE kind_pattern LIKE 'test%'")
            )
        admin = await bootstrap_admin(maker, "admin@spasta.online", PASSWORD)
        ids: dict[str, Any] = {"admin": admin.id}
        async with maker() as db, db.begin():
            admin_role = await db.scalar(select(Role.id).where(Role.name == ROLE_ADMIN))
            for who, role in (("admin2", admin_role), ("a", 2)):
                u = User(
                    email=f"{who}@spasta.online",
                    password_hash=hash_password(PASSWORD),
                    role_id=role,
                )
                db.add(u)
                await db.flush()
                ids[who] = u.id
        await engine.dispose()
        app = create_app(settings.model_copy(update=overrides))
        async with LifespanManager(app) as mgr:
            t = ASGITransport(app=mgr.app)
            names = ("admin", "admin2", "a")
            clients = {w: AsyncClient(transport=t, base_url="https://test") for w in names}
            csrf = {}
            for who, c in clients.items():
                r = await c.post(
                    "/api/auth/login", json={"email": f"{who}@spasta.online", "password": PASSWORD}
                )
                csrf[who] = r.json()["csrf_token"]
            try:
                yield Env(app, clients, csrf, ids)
            finally:
                for c in clients.values():
                    await c.aclose()
                await discord.drain()
                async with app.state.sessionmaker() as db, db.begin():
                    await db.execute(
                        text("DELETE FROM audit.approval_policies WHERE kind_pattern LIKE 'test%'")
                    )

    return factory


async def add_policy(e: Env, pattern: str, **kw: Any) -> None:
    cols = {"kind_pattern": pattern, **kw}
    names = ", ".join(cols)
    vals = ", ".join(f":{k}" for k in cols)
    await e.sql(f"INSERT INTO audit.approval_policies ({names}) VALUES ({vals})", **cols)


async def file(e: Env, payload: dict[str, Any] | None = None, **kw: Any) -> uuid.UUID:
    gate = ApprovalGate(e.maker, e.app.state.settings)
    async with e.maker() as db, db.begin():
        req = await gate.request(
            KIND,
            "thing:1",
            payload or {"x": 1},
            None,
            artifacts={"a": dict(STATE["a"]), "b": dict(STATE["b"])},
            requested_by=e.ids["a"],
            client=ClientInfo(ip="10.0.0.9"),
            db=db,
            **kw,
        )
        return req.id


def approve(e: Env, who: str, aid: uuid.UUID, **body: Any) -> Any:
    return e.send(
        who, "POST", f"/api/v1/approvals/{aid}/approve", json=body or {"password": PASSWORD}
    )


# --- policies -------------------------------------------------------------------------------


async def test_policy_resolution_most_specific_wins(env: EnvFactory) -> None:
    async with env() as e:
        aid = await file(e)
        d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
        assert d["policy"]["kind_pattern"] == "*"  # only the seeded fallback matches
        await add_policy(e, "test.*", required_approvals=2, risk_level="high")
        await add_policy(e, "test.ech?", required_approvals=3)
        await add_policy(e, "test.echo", required_approvals=4, enabled=False)
        aid = await file(e)
        d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
        # exact is disabled; "test.ech?" has more literal characters than "test.*"
        assert d["policy"]["kind_pattern"] == "test.ech?" and d["required_approvals"] == 3
        assert d["policy_subject"] == KIND and d["summary"] == f"{KIND} on thing:1"
        await e.sql(
            "UPDATE audit.approval_policies SET enabled = true WHERE kind_pattern = 'test.echo'"
        )
        d = (await e.get("admin", f"/api/v1/approvals/{await file(e)}")).json()
        assert d["required_approvals"] == 4
        rows = await e.sql("SELECT requester_ip FROM audit.approval_requests WHERE id = :i", i=aid)
        assert rows[0][0] == "10.0.0.9"


async def test_policy_expiry_seconds_and_seeded_defaults(env: EnvFactory) -> None:
    async with env() as e:
        r = await e.get("admin", "/api/v1/approvals/policies")
        pats = {p["kind_pattern"]: p for p in r.json()}
        assert {"*", "github.pr.merge", "repair.*", "trading.paper_intent"} <= set(pats)
        assert (
            pats["repair.*"]["require_reauth"] and pats["repair.*"]["reauth_max_age_seconds"] == 0
        )
        await add_policy(e, "test.*", expiry_seconds=120)
        aid = await file(e)
        d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
        left = datetime.fromisoformat(d["expires_at"]) - datetime.now(UTC)
        assert timedelta(seconds=100) < left <= timedelta(seconds=120)


async def test_put_policies_needs_password_and_is_audited(env: EnvFactory) -> None:
    async with env() as e:
        body = {"password": "wrong-password!", "policies": [{"kind_pattern": "test.put"}]}
        r = await e.send("admin", "PUT", "/api/v1/approvals/policies", json=body)
        assert r.status_code == 403
        assert (
            await e.send("a", "PUT", "/api/v1/approvals/policies", json=body)
        ).status_code == 403
        assert (await e.get("a", "/api/v1/approvals/policies")).status_code == 403
        body = {
            "password": PASSWORD,
            "policies": [
                {"kind_pattern": "test.put", "required_approvals": 2, "risk_level": "low"}
            ],
        }
        r = await e.send("admin", "PUT", "/api/v1/approvals/policies", json=body)
        assert r.status_code == 200, r.text
        got = next(p for p in r.json() if p["kind_pattern"] == "test.put")
        assert got["required_approvals"] == 2 and got["updated_by"] == str(e.ids["admin"])
        rows = await e.sql(
            "SELECT details FROM audit.audit_events WHERE event_type = 'approval_policies_updated'"
        )
        assert rows[0][0]["changes"][0]["after"]["required_approvals"] == 2
        assert PASSWORD not in json.dumps(rows[0][0])
        bad = {"password": PASSWORD, "policies": [{"kind_pattern": "Bad Pattern!"}]}
        r = await e.send("admin", "PUT", "/api/v1/approvals/policies", json=bad)
        assert r.status_code == 422


# --- multi-approval, single use, chain ---------------------------------------------------------


async def test_multi_approval_needs_two_distinct_admins(env: EnvFactory) -> None:
    async with env() as e:
        await add_policy(e, "test.*", required_approvals=2)
        aid = await file(e)
        r = await approve(e, "admin", aid)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending" and r.json()["received_approvals"] == 1
        assert STATE["runs"] == 0
        r = await approve(e, "admin", aid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "already_approved_by_you"
        inbox = (await e.get("admin", "/api/v1/approvals/inbox")).json()
        assert str(aid) not in [i["id"] for i in inbox["items"]]
        inbox2 = (await e.get("admin2", "/api/v1/approvals/inbox")).json()
        assert [i["id"] for i in inbox2["items"]] == [str(aid)]
        assert inbox2["items"][0]["reauth"] == "password"
        r = await approve(e, "admin2", aid)
        assert r.status_code == 200 and r.json()["status"] == "executed"
        assert r.json()["received_approvals"] == 2 and STATE["runs"] == 1
        # single use
        r = await approve(e, "admin2", aid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_already_used"
        ev = (await e.get("admin", f"/api/v1/approvals/{aid}/events")).json()
        assert [x["event"] for x in ev["events"]] == [
            "requested",
            "approval_recorded",
            "approved",
            "executed",
        ]
        assert ev["chain"]["status"] == "ok" and ev["chain"]["verified"] == 4
        d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
        assert d["approvers"] == [str(e.ids["admin"]), str(e.ids["admin2"])]
        assert {a["name"] for a in d["artifacts"]} == {"a", "b"}


async def test_hash_chain_detects_tampering(env: EnvFactory) -> None:
    async with env() as e:
        aid = await file(e)
        r = await e.send("admin", "POST", f"/api/v1/approvals/{aid}/deny", json={"reason": "no"})
        assert r.status_code == 200
        url = f"/api/v1/approvals/{aid}/events"
        assert (await e.get("admin", url)).json()["chain"]["status"] == "ok"
        await e.sql(
            'UPDATE audit.approval_events SET details = \'{"reason": "yes"}\'::jsonb '
            "WHERE request_id = :i AND event = 'denied'",
            i=aid,
        )
        chain = (await e.get("admin", url)).json()["chain"]
        assert chain["status"] == "broken" and chain["reason"] == "hash mismatch"
        # restore, then delete the tail: the head anchor catches it
        await e.sql(
            'UPDATE audit.approval_events SET details = \'{"reason": "no"}\'::jsonb '
            "WHERE request_id = :i AND event = 'denied'",
            i=aid,
        )
        assert (await e.get("admin", url)).json()["chain"]["status"] == "ok"
        await e.sql(
            "DELETE FROM audit.approval_events WHERE request_id = :i AND event = 'denied'", i=aid
        )
        chain = (await e.get("admin", url)).json()["chain"]
        assert chain["status"] == "broken" and chain["reason"] == "head hash mismatch"
        assert (await e.get("a", url)).status_code == 403


# --- drift ------------------------------------------------------------------------------------


async def test_drift_over_multiple_artifacts_invalidates(env: EnvFactory) -> None:
    async with env() as e:
        aid = await file(e)
        d = (await e.get("admin", f"/api/v1/approvals/{aid}", params={"check_drift": True})).json()
        assert d["drift"]["status"] == "ok"
        STATE["b"] = {"v": 2}
        d = (await e.get("admin", f"/api/v1/approvals/{aid}", params={"check_drift": True})).json()
        assert d["drift"] == {"status": "drifted", "drifted": ["b"], "detail": None}
        r = await approve(e, "admin", aid)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_invalidated"
        assert STATE["runs"] == 0
        d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
        assert d["status"] == "invalidated" and d["result"] == {"reason": "artifact_drift"}
        assert d["events"][-1]["details"]["drifted"] == ["b"]


# --- expiry -----------------------------------------------------------------------------------


async def test_expiry_sweep_marks_expired_and_notifies(env: EnvFactory) -> None:
    async with env(DISCORD_WEBHOOK_URL=SecretStr(WEBHOOK), DISCORD_RETRY_BASE_SECONDS=0.0) as e:
        with respx.mock(assert_all_called=False) as router:
            hook = router.post(WEBHOOK).mock(return_value=httpx.Response(204))
            a1, a2 = await file(e), await file(e)
            await e.sql(
                "UPDATE audit.approval_requests SET expires_at = now() - interval '1 minute' "
                "WHERE id = :i",
                i=a1,
            )
            ctx = TaskContext(e.app.state.settings, e.maker, None)
            assert await approval_expire(ctx) == {"expired": 1}
            assert await approval_expire(ctx) == {"expired": 0}
            await discord.drain()
            rows = await e.sql("SELECT id, status FROM audit.approval_requests ORDER BY created_at")
            assert {str(i): s for i, s in rows} == {str(a1): "expired", str(a2): "pending"}
            ev = (await e.get("admin", f"/api/v1/approvals/{a1}/events")).json()
            assert [x["event"] for x in ev["events"]] == ["requested", "expired"]
            assert ev["chain"]["status"] == "ok"
            titles = [json.loads(c.request.content)["embeds"][0]["title"] for c in hook.calls]
            assert titles.count(f"Approval requested: {KIND}") == 2
            assert titles.count(f"Expired: {KIND}") == 1
            r = await approve(e, "admin", a1)
            assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_not_pending"


# --- re-auth stamp ----------------------------------------------------------------------------


async def test_reauth_stamp_ttl_and_max_age(env: EnvFactory) -> None:
    async with env() as e:
        await add_policy(e, "test.*", reauth_max_age_seconds=60)
        aid = await file(e)
        # no password and no stamp: refused, and {} is still a validation error
        r = await approve(e, "admin", aid, use_reauth=True)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "reauth_required"
        assert (
            await e.send("admin", "POST", f"/api/v1/approvals/{aid}/approve", json={})
        ).status_code == 422
        r = await e.send(
            "admin", "POST", "/api/v1/approvals/reauth", json={"password": "nope-nope"}
        )
        assert r.status_code == 403
        r = await e.send("admin", "POST", "/api/v1/approvals/reauth", json={"password": PASSWORD})
        assert r.status_code == 200, r.text
        # TTL = the largest enabled policy max age (the seeded 300 s), capped by the setting
        assert r.json()["ttl_seconds"] == 300
        redis = Redis.from_url(TEST_REDIS_URL)
        try:
            keys = [k async for k in redis.scan_iter("approvals:reauth:*")]
            assert len(keys) == 1 and 290 <= await redis.ttl(keys[0]) <= 300
            assert PASSWORD.encode() not in (await redis.get(keys[0]) or b"")
            sid = uuid.UUID(keys[0].decode().rsplit(":", 1)[1])
            inbox = (await e.get("admin", "/api/v1/approvals/inbox")).json()
            assert inbox["items"][0]["reauth"] == "session_stamp"
            assert inbox["reauth_stamp_expires_at"] is not None
            # a stamp older than this policy's 60 s does not count
            old = datetime.now(UTC) - timedelta(seconds=120)
            await approval_reauth.set_stamp(redis, sid, e.ids["admin"], 300, at=old)
            r = await approve(e, "admin", aid, use_reauth=True)
            assert r.status_code == 403 and r.json()["detail"]["code"] == "reauth_required"
            # the other admin's stamp is per session: admin2 has none
            r = await approve(e, "admin2", aid, use_reauth=True)
            assert r.status_code == 403
            await approval_reauth.set_stamp(redis, sid, e.ids["admin"], 300)
            r = await approve(e, "admin", aid, use_reauth=True)
            assert r.status_code == 200 and r.json()["status"] == "executed"
            d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
            approved = next(x for x in d["events"] if x["event"] == "approved")
            assert approved["details"]["reauth"] == "session_stamp"
            # a policy with max age 0 never accepts the stamp
            await e.sql(
                "UPDATE audit.approval_policies SET reauth_max_age_seconds = 0 "
                "WHERE kind_pattern = 'test.*'"
            )
            aid2 = await file(e)
            r = await approve(e, "admin", aid2, use_reauth=True)
            assert r.status_code == 403
            assert (await e.send("admin", "DELETE", "/api/v1/approvals/reauth")).status_code == 204
            assert [k async for k in redis.scan_iter("approvals:reauth:*")] == []
        finally:
            await redis.aclose()


# --- Discord ----------------------------------------------------------------------------------


async def test_discord_payload_has_no_secrets_and_failure_never_blocks(env: EnvFactory) -> None:
    over = {
        "DISCORD_WEBHOOK_URL": SecretStr(WEBHOOK),
        "DISCORD_RETRY_BASE_SECONDS": 0.0,
        "DISCORD_MAX_ATTEMPTS": 3,
        "PUBLIC_APP_URL": "https://app.example",
    }
    async with env(**over) as e:
        with respx.mock(assert_all_called=False) as router:
            hook = router.post(WEBHOOK).mock(return_value=httpx.Response(500))
            aid = await file(e, {"token": SECRET, "password": "hunter2-secret"})
            r = await approve(e, "admin", aid)
            assert r.status_code == 200 and r.json()["status"] == "executed"
            await discord.drain()
            # requested + approved, each tried 3 times, and the flow was never blocked
            assert hook.call_count == 6
            bodies = [c.request.content.decode() for c in hook.calls]
            for b in bodies:
                assert SECRET not in b and "hunter2" not in b and "SECRET-WEBHOOK" not in b
                assert f"https://app.example/dev/approvals/{aid}" in b
                assert '"payload"' not in b
            data = json.loads(bodies[0])
            assert data["allowed_mentions"] == {"parse": []}
            assert "components" not in data  # no buttons: nothing can be approved from Discord
        # a network error is swallowed too
        with respx.mock(assert_all_called=False) as router:
            router.post(WEBHOOK).mock(side_effect=httpx.ConnectError("down"))
            aid = await file(e)
            r = await e.send("admin", "POST", f"/api/v1/approvals/{aid}/deny", json={"reason": "x"})
            assert r.status_code == 200
            await discord.drain()


# --- stream -----------------------------------------------------------------------------------


def _parse(raw: str) -> list[dict[str, Any]]:
    out = []
    for block in raw.split("\n\n"):
        ev: dict[str, Any] = {}
        for line in block.splitlines():
            k, _, v = line.partition(": ")
            if k in ("id", "event"):
                ev[k] = v
            elif k == "data":
                ev["data"] = json.loads(v)
        if "event" in ev:
            out.append(ev)
    return out


async def test_stream_emits_approval_events_and_resumes(
    env: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setattr(approval_stream, "LAG", timedelta(milliseconds=50))
    over = {
        "APPROVALS_STREAM_POLL_SECONDS": 0.05,
        "APPROVALS_STREAM_HEARTBEAT_SECONDS": 0.2,
        "APPROVALS_STREAM_MAX_SECONDS": 1.0,
    }
    async with env(**over) as e:
        await file(e)  # before the stream: not replayed

        async def later() -> None:
            await asyncio.sleep(0.2)
            await file(e, {"x": 2})

        task = asyncio.create_task(later())
        r = await e.get("admin", "/api/v1/approvals/stream")
        await task
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = _parse(r.text)
        assert events[0]["event"] == "hello" and events[-1]["event"] == "bye"
        got = [x for x in events if x["event"] == "approval"]
        assert len(got) == 1 and got[0]["data"]["event"] == "requested"
        assert "payload" not in got[0]["data"]
        last = got[0]["id"]
        await file(e, {"x": 3})
        r = await e.get("admin", "/api/v1/approvals/stream", params={"last_event_id": last})
        got = [x for x in _parse(r.text) if x["event"] == "approval"]
        assert len(got) == 1 and int(got[0]["id"]) > int(last)
        assert (await e.get("a", "/api/v1/approvals/stream")).status_code == 403


# --- list + counts ----------------------------------------------------------------------------


async def test_list_has_counts_and_filters(env: EnvFactory) -> None:
    async with env() as e:
        a1 = await file(e)
        await file(e)
        await e.send("admin", "POST", f"/api/v1/approvals/{a1}/deny", json={"reason": "r"})
        r = (await e.get("admin", "/api/v1/approvals", params={"kind": KIND})).json()
        assert r["total"] == 2 and r["counts"] == {"pending": 1, "denied": 1}
        r = (await e.get("admin", "/api/v1/approvals", params={"status": "denied"})).json()
        assert [x["id"] for x in r["items"]] == [str(a1)]
        assert r["counts"]["pending"] == 1


# --- audit log API ----------------------------------------------------------------------------


async def test_audit_events_api_filters_and_is_admin_only(env: EnvFactory) -> None:
    async with env() as e:
        aid = await file(e)
        await e.send("admin2", "POST", f"/api/v1/approvals/{aid}/deny", json={"reason": "r"})
        assert (await e.get("a", "/api/v1/audit/events")).status_code == 403
        r = (
            await e.get("admin", "/api/v1/audit/events", params={"event_type": "approval_"})
        ).json()
        types = [x["event_type"] for x in r["items"]]
        assert types == ["approval_denied", "approval_requested"] and r["total"] == 2
        r = (
            await e.get(
                "admin",
                "/api/v1/audit/events",
                params={"actor_user_id": str(e.ids["admin2"]), "event_type": "approval"},
            )
        ).json()
        assert [x["event_type"] for x in r["items"]] == ["approval_denied"]
        assert r["items"][0]["actor_email"] == "admin2@spasta.online"
        assert r["items"][0]["details"]["approval_id"] == str(aid)
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        r = (await e.get("admin", "/api/v1/audit/events", params={"since": future})).json()
        assert r["total"] == 0
        r = (
            await e.get("admin", "/api/v1/audit/events", params={"until": future, "limit": 1})
        ).json()
        assert len(r["items"]) == 1 and r["total"] > 1
        # "_" in the prefix is literal, not a LIKE wildcard
        r = (
            await e.get("admin", "/api/v1/audit/events", params={"event_type": "approval_d"})
        ).json()
        assert r["total"] == 1
        assert (
            await e.get("admin", "/api/v1/audit/events", params={"limit": 201})
        ).status_code == 422


# --- voice ------------------------------------------------------------------------------------


async def test_voice_approve_utterance_only_navigates(env: EnvFactory) -> None:
    async with env() as e:
        aid = await file(e)

        async def snapshot() -> Any:
            return (
                await e.sql("SELECT id, status, received_approvals FROM audit.approval_requests"),
                await e.sql("SELECT count(*) FROM audit.approval_events"),
                await e.sql("SELECT count(*) FROM audit.audit_events"),
            )

        before = await snapshot()
        for words in (f"approve request {aid}", "approve request 123", "merge PR 42 now"):
            r = await e.send("admin", "POST", "/api/v1/voice/intent", json={"transcript": words})
            assert r.status_code == 200, r.text
        assert r.json() == {
            "intent": "open_pr",
            "route": "/dev/prs/42",
            "params": {"number": "42"},
            "source": "grammar",
            "note": "navigation only: review and act in the app",
        }
        r = await e.send(
            "admin", "POST", "/api/v1/voice/intent", json={"transcript": "approve request 123"}
        )
        assert r.json()["intent"] == "open_approval" and r.json()["route"] == "/dev/approvals/123"
        assert await snapshot() == before
        assert STATE["runs"] == 0
        r = await e.send("a", "POST", "/api/v1/voice/intent", json={"transcript": "approvals"})
        assert r.status_code == 403
