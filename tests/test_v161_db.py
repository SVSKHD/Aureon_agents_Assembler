"""V1.6.1 regression tests against PostgreSQL (and the test Redis):

A1  the persistent kill switch (trading.risk_policies.kill_switch) cannot be released by an
    omitted field, a stale form or a concurrent write, and survives a restart;
A4  `migrate --check` reads the real version table: at head / pending / diverged;
A5  intent status changes are atomic: the scheduler never fills or expires an intent a cancel
    holds or has cancelled, a stale writer is refused (compare-and-set on status), concurrent
    cancels and cancel-vs-fill races always end in one consistent terminal state, and the
    optional expected_status answers 409 on a mismatch;
A6  alert delivery leases on Redis: crash recovery, fencing of stale workers.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

from app.main import create_app
from app.models.trading import Execution, OrderEvent, OrderIntent
from app.services import alerts, paper
from app.workers import migrate
from tests.conftest import TEST_DATABASE_URL, TEST_REDIS_URL
from tests.test_paper_db import PASSWORD, EnvFactory, make_env

pytestmark = pytest.mark.db
FIXTURES = (make_env,)  # re-exported pytest fixture
ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"REJECTED", "FILLED", "CANCELLED", "EXPIRED"}


# --- helpers --------------------------------------------------------------------------------


async def _resting(env: Any) -> str:
    """An approved LIMIT intent resting as SUBMITTED (not marketable at 1000)."""
    await env.tick("1000")
    made = (await env.intent(order_type="LIMIT", limit_price="990")).json()
    r = (await env.approve(made["id"])).json()
    assert r["status"] == "SUBMITTED", r
    return str(made["id"])


async def _status(env: Any, iid: str) -> str:
    async with env.maker() as db:
        s = await db.scalar(select(OrderIntent.status).where(OrderIntent.id == uuid.UUID(iid)))
    return str(s)


async def _fills(env: Any, iid: str) -> int:
    return int(await env.count(Execution, Execution.order_intent_id == uuid.UUID(iid)))


async def _terminal_events(env: Any, iid: str) -> list[str]:
    async with env.maker() as db:
        kinds = (
            await db.scalars(
                select(OrderEvent.event_type).where(OrderEvent.order_intent_id == uuid.UUID(iid))
            )
        ).all()
    return [k for k in kinds if k in {"FILLED", "CANCELLED", "EXPIRED", "RISK_REJECTED"}]


async def _process(env: Any) -> dict[str, int]:
    async with env.maker() as db, db.begin():
        return await paper.process_open(db, env.app.state.redis, {}, env.settings)


# --- A5: atomic intent transitions ----------------------------------------------------------


async def test_scheduler_skips_an_intent_a_cancel_holds_and_never_refills_it(
    make_env: EnvFactory,
) -> None:
    async with make_env() as env:
        await env.policy()
        iid = await _resting(env)
        await env.tick("985")  # marketable now: the scheduler would fill it
        async with env.maker() as hold, hold.begin():
            locked = await hold.get(OrderIntent, uuid.UUID(iid), with_for_update=True)
            assert locked is not None
            # A scheduler run while the cancel holds the row skips it (no wait, no fill).
            out = await asyncio.wait_for(_process(env), 10)
            assert out == {"filled": 0, "expired": 0, "waiting": 0}
            await paper.cancel(hold, locked, env.ids["user"])
        out = await _process(env)  # after the cancel committed: nothing open
        assert out == {"filled": 0, "expired": 0, "waiting": 0}
        assert await _status(env, iid) == "CANCELLED"
        assert await _fills(env, iid) == 0


async def test_a_stale_writer_cannot_overwrite_a_committed_cancel(make_env: EnvFactory) -> None:
    """Compare-and-set: a session that read SUBMITTED before the cancel committed cannot
    write FILLED over CANCELLED; its flush fails and the row keeps CANCELLED."""
    async with make_env() as env:
        await env.policy()
        iid = await _resting(env)
        async with env.maker() as stale:
            intent = await stale.get(OrderIntent, uuid.UUID(iid))
            assert intent is not None and intent.status == "SUBMITTED"
            r = await env.send("user", "POST", f"/api/trading/order-intents/{iid}/cancel")
            assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
            paper.transition(intent, "FILLED")  # legal from what this session saw...
            with pytest.raises(StaleDataError):
                await stale.commit()  # ...but the row moved on
        assert await _status(env, iid) == "CANCELLED"


async def test_cancel_racing_the_scheduler_always_ends_consistent(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy()
        outcomes = set()
        for _ in range(6):
            iid = await _resting(env)
            await env.tick("985")
            cancel, _out = await asyncio.gather(
                env.send("user", "POST", f"/api/trading/order-intents/{iid}/cancel"),
                _process(env),
            )
            final, fills = await _status(env, iid), await _fills(env, iid)
            if final == "CANCELLED":
                assert cancel.status_code == 200 and fills == 0
            else:
                assert final == "FILLED" and fills == 1, (final, fills)
                assert cancel.status_code == 409
                assert cancel.json()["detail"]["code"] in ("not_open", "conflict")
            assert len(await _terminal_events(env, iid)) == 1
            outcomes.add(final)
        assert outcomes <= {"CANCELLED", "FILLED"}


async def test_concurrent_cancels_cancel_once(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy()
        await env.tick("1000")
        iid = (await env.intent()).json()["id"]
        url = f"/api/trading/order-intents/{iid}/cancel"
        rs = await asyncio.gather(
            env.send("user", "POST", url),
            env.send("admin", "POST", url),
            env.send("user", "POST", url),
        )
        assert sorted(r.status_code for r in rs) == [200, 409, 409]
        assert {r.json()["detail"]["code"] for r in rs if r.status_code == 409} == {"not_open"}
        assert await _terminal_events(env, iid) == ["CANCELLED"]


async def test_expected_status_mismatch_is_409_and_changes_nothing(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy()
        await env.tick("1000")
        iid = (await env.intent()).json()["id"]
        cancel = f"/api/trading/order-intents/{iid}/cancel"
        r = await env.send("user", "POST", cancel, json={"expected_status": "SUBMITTED"})
        assert r.status_code == 409
        assert r.json()["detail"] == {
            "code": "status_mismatch",
            "detail": "intent is PENDING_RISK, not SUBMITTED",
            "status": "PENDING_RISK",
        }
        approve = f"/api/trading/order-intents/{iid}/approve"
        r = await env.send(
            "admin", "POST", approve, json={"password": PASSWORD, "expected_status": "APPROVED"}
        )
        assert r.status_code == 409 and r.json()["detail"]["code"] == "status_mismatch"
        assert await _status(env, iid) == "PENDING_RISK" and await _fills(env, iid) == 0
        r = await env.send("user", "POST", cancel, json={"expected_status": "NOPE"})
        assert r.status_code == 422
        # Matching expectation (and the legacy body-less call) work as before.
        r = await env.send("user", "POST", cancel, json={"expected_status": "PENDING_RISK"})
        assert r.status_code == 200 and r.json()["status"] == "CANCELLED"


async def test_terminal_intents_cannot_be_moved_by_the_service(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.policy()
        await env.tick("1000")
        iid = (await env.intent()).json()["id"]
        assert (
            await env.send("user", "POST", f"/api/trading/order-intents/{iid}/cancel")
        ).is_success
        async with env.maker() as db, db.begin():
            intent = await db.get(OrderIntent, uuid.UUID(iid), with_for_update=True)
            assert intent is not None
            for new in ("APPROVED", "SUBMITTED", "FILLED", "EXPIRED", "PENDING_RISK"):
                with pytest.raises(paper.IntentRefused):
                    paper.transition(intent, new)
            with pytest.raises(paper.IntentRefused):
                await paper.execute(db, None, {}, env.settings, intent)
        assert await _status(env, iid) == "CANCELLED"


# --- A1: the persistent kill switch ---------------------------------------------------------

POLICY = {
    "max_daily_loss": "1000",
    "max_exposure": "100000",
    "max_position_size": "100",
    "max_drawdown_pct": "10",
    "allowed_sessions": ["Asia", "London", "NY"],
}


async def test_kill_switch_survives_omission_stale_forms_and_needs_a_version_to_release(
    make_env: EnvFactory,
) -> None:
    async with make_env() as env:
        url = f"/api/trading/accounts/{env.account_id}/risk-policy"
        r = await env.send("admin", "PUT", url, json={**POLICY, "kill_switch": True})
        assert r.status_code == 200 and r.json()["kill_switch"] is True
        v1 = r.json()["updated_at"]

        # A limits-only PUT (no kill_switch field) keeps the lock. Before 1.6.1 it released it.
        r = await env.send("admin", "PUT", url, json={**POLICY, "max_daily_loss": "900"})
        assert r.status_code == 200 and r.json()["kill_switch"] is True
        v2 = r.json()["updated_at"]
        assert v2 != v1

        # Releasing needs the version the caller read...
        r = await env.send("admin", "PUT", url, json={**POLICY, "kill_switch": False})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "kill_switch_release_needs_version"
        # ...and a stale one (from before the limits change) is refused.
        r = await env.send(
            "admin", "PUT", url, json={**POLICY, "kill_switch": False, "expected_updated_at": v1}
        )
        assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_policy"
        assert (await env.clients["user"].get(url)).json()["kill_switch"] is True

        fresh = (await env.clients["user"].get(url)).json()["updated_at"]
        r = await env.send(
            "admin", "PUT", url, json={**POLICY, "kill_switch": False, "expected_updated_at": fresh}
        )
        assert r.status_code == 200 and r.json()["kill_switch"] is False
        # Engaging never needs a version (fail-safe direction).
        r = await env.send("admin", "PUT", url, json={**POLICY, "kill_switch": True})
        assert r.status_code == 200 and r.json()["kill_switch"] is True
        # A naive timestamp is a 422, not a 500.
        r = await env.send(
            "admin", "PUT", url, json={**POLICY, "expected_updated_at": "2026-01-01T00:00:00"}
        )
        assert r.status_code == 422


async def test_concurrent_policy_writes_from_one_read_let_exactly_one_win(
    make_env: EnvFactory,
) -> None:
    async with make_env() as env:
        url = f"/api/trading/accounts/{env.account_id}/risk-policy"
        r = await env.send("admin", "PUT", url, json={**POLICY, "kill_switch": True})
        v = r.json()["updated_at"]
        bodies = [
            {**POLICY, "kill_switch": i % 2 == 0, "max_daily_loss": str(100 + i),
             "expected_updated_at": v}
            for i in range(8)
        ]  # fmt: skip
        rs = await asyncio.gather(*(env.send("admin", "PUT", url, json=b) for b in bodies))
        codes = sorted(r.status_code for r in rs)
        assert codes == [200] + [409] * 7, codes
        (winner,) = [b for b, r in zip(bodies, rs, strict=True) if r.status_code == 200]
        got = (await env.clients["user"].get(url)).json()
        assert got["kill_switch"] is winner["kill_switch"]
        assert Decimal(got["max_daily_loss"]) == Decimal(winner["max_daily_loss"])


async def test_concurrent_first_puts_do_not_500(make_env: EnvFactory) -> None:
    async with make_env() as env:
        url = f"/api/trading/accounts/{env.account_id}/risk-policy"
        rs = await asyncio.gather(
            *(env.send("admin", "PUT", url, json={**POLICY, "kill_switch": True}) for _ in range(4))
        )
        assert all(r.status_code in (200, 409) for r in rs), [r.status_code for r in rs]
        assert any(r.status_code == 200 for r in rs)
        assert (await env.clients["user"].get(url)).json()["kill_switch"] is True


async def test_kill_switch_persists_across_a_restart(make_env: EnvFactory) -> None:
    async with make_env() as env:
        url = f"/api/trading/accounts/{env.account_id}/risk-policy"
        r = await env.send("admin", "PUT", url, json={**POLICY, "kill_switch": True})
        assert r.is_success
        await env.tick("1000")
        # A brand-new app instance (a restart) on the same database and Redis.
        app = create_app(env.settings)
        async with LifespanManager(app) as mgr:
            async with AsyncClient(
                transport=ASGITransport(app=mgr.app), base_url="https://test"
            ) as c:
                login = await c.post(
                    "/api/auth/login", json={"email": "user@spasta.online", "password": PASSWORD}
                )
                csrf = login.json()["csrf_token"]
                assert (await c.get(url)).json()["kill_switch"] is True
                r = await c.post(
                    "/api/trading/order-intents",
                    headers={"x-csrf-token": csrf},
                    json={"account_id": str(env.account_id), "instrument_id": env.equity,
                          "side": "BUY", "quantity": "10"},
                )  # fmt: skip
                assert r.status_code == 201 and r.json()["status"] == "REJECTED"
                assert "kill_switch" in r.json()["events"][-1]["payload"]["reason"]


# --- A4: migration state of a real database -------------------------------------------------


def _psql(url: str, sql: str) -> str:
    r = subprocess.run(["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", url, "-c", sql],
                       capture_output=True, text=True, check=True)  # fmt: skip
    return r.stdout.strip()


def _check(url: str) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, "-m", "app.workers.migrate", "--check"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
        env={**os.environ, "DATABASE_URL": url, "MIGRATIONS_DATABASE_URL": ""},
    )  # fmt: skip
    assert migrate.CHECK_MARKER in r.stdout, r.stdout + r.stderr
    return r.returncode, r.stdout.split(migrate.CHECK_MARKER, 1)[1]


def test_migrate_check_against_real_databases(migrated: str) -> None:
    code, out = _check(migrated)
    assert code == 0 and '"state": "at_head"' in out, out
    assert TEST_DATABASE_URL is not None
    server = TEST_DATABASE_URL.replace("+asyncpg", "").rsplit("/", 1)[0]
    scratch = f"aureon_chk_t{os.getpid()}_{uuid.uuid4().hex[:6]}"
    _psql(f"{server}/postgres", f'CREATE DATABASE "{scratch}"')
    url = TEST_DATABASE_URL.rsplit("/", 1)[0] + f"/{scratch}"
    try:
        code, out = _check(url)  # never migrated
        assert code == 3 and '"state": "pending"' in out and '"applied": []' in out
        env = {**os.environ, "DATABASE_URL": url, "MIGRATIONS_DATABASE_URL": ""}
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0022"], cwd=ROOT,
                       env=env, check=True, capture_output=True)  # fmt: skip
        code, out = _check(url)  # behind head
        assert code == 3 and '"applied": ["0022"]' in out and '"0023"' in out
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                       env=env, check=True, capture_output=True)  # fmt: skip
        assert _check(url)[0] == 0
        _psql(f"{server}/{scratch}", "UPDATE public.aureon_alembic_version SET version_num='9f9f'")
        code, out = _check(url)  # a revision this build does not have
        assert code == 4 and '"state": "diverged"' in out and '"9f9f"' in out
    finally:
        _psql(f"{server}/postgres", f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')


# --- A6: alert leases on Redis --------------------------------------------------------------


@pytest.fixture
async def redis() -> Any:
    r = Redis.from_url(TEST_REDIS_URL)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


async def test_crashed_worker_lease_expires_and_the_alert_is_retried(
    redis: Redis, migrated: str
) -> None:
    d = alerts.Dedup(redis, window_s=600, lease_s=1)
    crashed = await d.claim("fp")  # worker A claims, then dies: no commit, no release
    assert crashed is not None and await d.claim("fp") is None
    ttl = await redis.ttl(alerts.DEDUP_PREFIX + "fp")
    assert 0 < ttl <= 1  # a lease, not the 600 s window
    await asyncio.sleep(1.2)
    retry = await d.claim("fp")
    assert retry is not None  # recovered without a human
    assert await d.commit("fp", retry) is True
    assert await redis.ttl(alerts.DEDUP_PREFIX + "fp") > 500
    assert await d.claim("fp") is None  # delivered once; idempotent afterwards


async def test_stale_worker_cannot_overwrite_a_newer_attempt(redis: Redis, migrated: str) -> None:
    d = alerts.Dedup(redis, window_s=600, lease_s=1)
    a = await d.claim("fp")
    await asyncio.sleep(1.2)  # A hung past its lease
    b = await d.claim("fp")
    assert a and b and a != b
    await d.release("fp", a)  # A's late failure handler
    assert await redis.get(alerts.DEDUP_PREFIX + "fp") == f"sending:{b}".encode()
    assert await d.commit("fp", a) is False  # A's late success
    assert await redis.get(alerts.DEDUP_PREFIX + "fp") == f"sending:{b}".encode()
    await d.release("fp", b)  # B fails: released, the next run retries
    assert await redis.get(alerts.DEDUP_PREFIX + "fp") is None


async def test_dispatch_cancelled_mid_send_releases_its_lease_on_redis(
    redis: Redis, migrated: str, monkeypatch: pytest.MonkeyPatch, settings: Any
) -> None:
    from app.core.config import Settings

    alert = alerts.Alert("incident", "x-2", "critical", "DB down", "detail", "/dev")

    async def collect(maker: Any, s: Any, now: Any = None) -> Any:
        return [alert], {}

    monkeypatch.setattr(alerts, "collect", collect)
    s = Settings.model_validate(
        {**settings.model_dump(), "DISCORD_WEBHOOK_URL": "https://discord.test/api/webhooks/1/x"}
    )

    async def hang(settings: Any, a: Any) -> bool:
        await asyncio.sleep(3600)
        return True

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.3):
            await alerts.dispatch(None, s, redis, sender=hang)  # type: ignore[arg-type]
    assert await redis.get(alerts.DEDUP_PREFIX + "x-2") is None  # released, not held 60 min
    sent: list[Any] = []

    async def ok(settings: Any, a: Any) -> bool:
        sent.append(a)
        return True

    assert (await alerts.dispatch(None, s, redis, sender=ok))["sent"] == 1  # type: ignore[arg-type]
    assert (await alerts.dispatch(None, s, redis, sender=ok))["deduplicated"] == 1  # type: ignore[arg-type]
    assert len(sent) == 1
