"""Phase 22 against the DB: the MT5 read-only feed end to end (bot export fixture -> mt5_sync
job -> /api/trading/mt5/status, positions, the trading stream, the integrations card),
GET /api/v1/system/integrations, and the alerts_dispatch task."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.trading.mt5.adapter import MT5Adapter
from app.agents.trading.mt5.status import StatusSource
from app.auth.bootstrap import bootstrap_admin
from app.auth.security import hash_password
from app.core.config import Settings
from app.main import create_app
from app.models.agents import Agent
from app.models.approvals import ApprovalRequest
from app.models.auth import User
from app.models.repairs import Incident
from app.models.trading import Instrument, PositionSnapshot
from app.services import alerts, trading_stream
from app.workers.scheduler import Scheduler, default_jobs
from tests.conftest import TEST_REDIS_URL
from tests.test_trading_stream import parse

pytestmark = pytest.mark.db
FIX = Path(__file__).parent / "fixtures" / "mt5" / "status.json"
LOGIN = "51234567"
PASSWORD = "correct horse battery staple"
OLLAMA_TAGS = "http://ollama.test:11434/api/tags"


@dataclass
class Env:
    maker: async_sessionmaker[AsyncSession]
    settings: Settings
    tmp: Path

    def with_(self, **kw: Any) -> Settings:
        return Settings.model_validate({**self.settings.model_dump(), **kw})


@pytest.fixture
async def env(
    db_clean: str, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Env]:
    monkeypatch.setattr(trading_stream, "LAG", timedelta(milliseconds=150))
    engine = create_async_engine(db_clean)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def clean() -> None:
        async with engine.begin() as conn:
            for t in (
                "trading.positions_snapshots",
                "trading.trading_signals",
                "trading.strategy_versions",
                "trading.strategies",
                "mt5.symbols",
                "mt5.terminals",
                "trading.broker_accounts",
                "trading.instruments",
            ):
                await conn.exec_driver_sql(f"DELETE FROM {t}")

    await clean()
    s = Settings.model_validate(
        {
            **settings.model_dump(),
            "DATABASE_URL": db_clean,
            "BACKUP_STATUS_FILE": str(tmp_path / "ops-status" / "backup.json"),
            "TRADING_STREAM_POLL_SECONDS": 0.1,
            "TRADING_STREAM_MAX_SECONDS": 1.5,
            "TRADING_STREAM_HEARTBEAT_SECONDS": 0.3,
        }
    )
    await bootstrap_admin(maker, "admin@spasta.online", PASSWORD)
    async with maker() as db, db.begin():
        db.add(User(email="user@spasta.online", password_hash=hash_password(PASSWORD), role_id=2))
    yield Env(maker, s, tmp_path)
    await clean()
    await engine.dispose()


async def _login(app: Any, email: str = "admin@spasta.online") -> AsyncClient:
    c = AsyncClient(transport=ASGITransport(app=app), base_url="https://test")
    r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    c.headers["x-csrf-token"] = r.json()["csrf_token"]
    return c


def _export(path: Path, **changes: Any) -> None:
    d: dict[str, Any] = json.loads(FIX.read_text())
    d["generated_at"] = datetime.now(UTC).isoformat()
    d.update(changes)
    path.write_text(json.dumps(d))


# --- MT5 read-only feed, end to end -------------------------------------------------------


async def test_mt5_export_to_api_positions_and_stream(env: Env) -> None:
    export = env.tmp / "status.json"
    _export(export)
    s = env.with_(MT5_STATUS_FILE=str(export), MT5_ACCOUNT_ID=LOGIN, MT5_SERVER="Broker-Demo")
    async with env.maker() as db, db.begin():
        db.add(
            Instrument(
                broker="mt5", broker_instrument_id="XAUUSD", symbol="XAUUSD",
                instrument_type="CFD", currency="USD", lot_size=Decimal("0.01"),
                tick_size=Decimal("0.01"),
            )
        )  # fmt: skip
    app = create_app(s)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(OLLAMA_TAGS).mock(return_value=httpx.Response(200, json={"models": []}))
        async with LifespanManager(app) as mgr:
            c = await _login(mgr.app)
            try:
                acct = await c.post(
                    "/api/trading/accounts",
                    json={"broker": "mt5", "account_id": LOGIN, "base_currency": "USD"},
                )
                assert acct.status_code == 201, acct.text
                acct_id = acct.json()["id"]
                # Before the first sync the feed is not fresh on the card.
                card = (await c.get("/api/v1/system/integrations")).json()
                assert card["integrations"]["mt5"]["fresh"] is False

                # The stream is open while the scheduler job ingests the export.
                adapter = MT5Adapter(
                    StatusSource(path=str(export)), account_id=LOGIN, currency="USD",
                    stale_after=90,
                )  # fmt: skip
                sched = Scheduler(s, env.maker, tasks=None, mt5=adapter)

                async def sync_later() -> dict[str, Any]:
                    await asyncio.sleep(0.4)
                    return await sched.tasks["mt5_sync_status"](sched.ctx)

                job = asyncio.create_task(sync_later())
                stream = await c.get("/api/trading/stream")
                out = await job
                await adapter.aclose()
                assert out["signals"]["inserted"] == 1 and out["positions"] == 2, out

                events = parse(stream.text)
                sigs = [e for e in events if e["event"] == "signal"]
                assert len(sigs) == 1, [e["event"] for e in events]
                assert sigs[0]["data"]["side"] == "BUY"

                st = (await c.get("/api/trading/mt5/status")).json()
                assert st["ok"] and st["source"] == "file"
                assert {p["ticket"] for a in st["agents"].values() for p in a["positions"]} == {
                    101,
                    202,
                }
                pos = (await c.get(f"/api/trading/accounts/{acct_id}/positions")).json()
                assert pos["total"] == 2
                agents = {a["id"]: a for a in (await c.get("/api/trading/agents")).json()}
                assert agents["mt5-bot"]["state"] not in ("stale", "error")

                card = (await c.get("/api/v1/system/integrations")).json()
                mt5 = card["integrations"]["mt5"]
                assert mt5["fresh"] and mt5["status"] == "ok" and mt5["read_only"]
                # No write path: funds/orders are not supported, and there is no order route.
                f = await c.get(f"/api/trading/accounts/{acct_id}/orders")
                assert f.status_code == 501
            finally:
                await c.aclose()
    async with env.maker() as db:
        snaps = await db.scalar(select(func.count()).select_from(PositionSnapshot))
    assert snaps == 2

    # The export stops: the feed goes stale on the card.
    async with env.maker() as db, db.begin():
        agent = await db.get(Agent, "mt5-bot")
        assert agent is not None
        agent.last_heartbeat_at = datetime.now(UTC) - timedelta(minutes=10)
    app = create_app(s)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(OLLAMA_TAGS).mock(return_value=httpx.Response(200, json={"models": []}))
        async with LifespanManager(app) as mgr:
            c = await _login(mgr.app)
            card = (await c.get("/api/v1/system/integrations")).json()
            await c.aclose()
    assert card["integrations"]["mt5"]["status"] == "down"
    assert card["integrations"]["mt5"]["agent"]["state"] == "stale"
    assert "mt5" in card["problems"]


# --- the integrations card ----------------------------------------------------------------


async def test_integrations_card_sections_and_auth(env: Env) -> None:
    status_file = env.tmp / "ops-status" / "backup.json"
    status_file.parent.mkdir(parents=True)
    status_file.write_text(json.dumps({
        "status": "ok", "last_success_at": datetime.now(UTC).isoformat(),
        "last_success_backup": "aureon-20261102T020000Z",
    }))  # fmt: skip
    s = env.with_(
        DISCORD_WEBHOOK_URL="https://discord.test/api/webhooks/1/secret-token",
        CODE_MODEL="qwen2.5-coder:7b",
        REPAIR_MODEL="qwen2.5-coder:14b",
        GITHUB_WEBHOOK_SECRET="w" * 32,
    )
    async with env.maker() as db, db.begin():
        db.add(Agent(id="github-webhooks", name="w", kind="integration", state="idle",
                     last_heartbeat_at=datetime.now(UTC)))  # fmt: skip
    app = create_app(s)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(OLLAMA_TAGS).mock(
            return_value=httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:7b"}]})
        )
        async with LifespanManager(app) as mgr:
            anon = AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test")
            assert (await anon.get("/api/v1/system/integrations")).status_code == 401
            await anon.aclose()
            user = await _login(mgr.app, "user@spasta.online")
            assert (await user.get("/api/v1/system/integrations")).status_code == 403
            await user.aclose()
            c = await _login(mgr.app)
            r = await c.get("/api/v1/system/integrations")
            await c.aclose()
    assert r.status_code == 200
    body = r.json()
    sec = body["integrations"]
    assert set(sec) == {"mt5", "dhan", "github", "webhook_worker", "claude_cli", "ollama",
                        "discord", "database", "backups"}  # fmt: skip
    assert sec["mt5"]["status"] == "not_configured"
    assert sec["dhan"]["status"] == "not_configured"
    assert sec["github"]["status"] == "not_configured"
    assert sec["claude_cli"] == {**sec["claude_cli"], "status": "disabled", "advisory_only": True}
    assert sec["webhook_worker"]["status"] == "ok" and sec["webhook_worker"]["pending"] == 0
    assert sec["discord"]["configured"] is True
    assert "secret-token" not in r.text  # the webhook URL is a secret
    assert sec["ollama"]["models"] == ["qwen2.5-coder:7b"]
    assert sec["ollama"]["missing"] == {"REPAIR_MODEL": "qwen2.5-coder:14b"}
    assert sec["ollama"]["status"] == "degraded"
    # The test DB runs as a superuser: the card says the scoped roles are not in use.
    assert sec["database"]["superuser"] is True and sec["database"]["status"] == "degraded"
    assert sec["backups"]["status"] == "ok" and sec["backups"]["age_hours"] < 1
    assert set(body["problems"]) == {"ollama", "database"}


# --- alerts -------------------------------------------------------------------------------


class Capture:
    def __init__(self, ok: bool = True) -> None:
        self.sent: list[dict[str, Any]] = []
        self.ok = ok

    async def __call__(self, settings: Settings, alert: Mapping[str, Any]) -> bool:
        self.sent.append(dict(alert))
        return self.ok


async def _seed_alert_sources(env: Env) -> None:
    now = datetime.now(UTC)
    async with env.maker() as db, db.begin():
        admin = await db.scalar(select(User).where(User.email == "admin@spasta.online"))
        assert admin is not None
        db.add_all([
            Incident(fingerprint="f" * 64, source="db", severity="error", title="DB slow",
                     details={}),
            Incident(fingerprint="g" * 64, source="api", severity="info", title="minor",
                     details={}),  # below warning: no alert
            Incident(fingerprint="h" * 64, source="heartbeat", severity="warning",
                     title="hb", details={}),  # covered by the heartbeat check
            Incident(fingerprint="i" * 64, source="docker", severity="critical",
                     title="old", status="resolved", details={}),  # resolved: no alert
            Agent(id="research-x", name="r", kind="trading", state="idle",
                  last_heartbeat_at=now - timedelta(hours=1)),
            Agent(id="paused-x", name="p", kind="trading", state="idle", paused=True,
                  last_heartbeat_at=now - timedelta(hours=1)),
            Agent(id="stopped-x", name="s", kind="trading", state="stopped",
                  last_heartbeat_at=now - timedelta(hours=1)),
            Agent(id="fresh-x", name="f", kind="trading", state="idle", last_heartbeat_at=now),
            ApprovalRequest(kind="github.pr.merge", target="o/r#1", summary="merge 1",
                            requested_by=admin.id, expires_at=now + timedelta(minutes=5)),
            ApprovalRequest(kind="github.pr.merge", target="o/r#2", summary="later",
                            requested_by=admin.id, expires_at=now + timedelta(hours=2)),
        ])  # fmt: skip
    status_file = env.tmp / "ops-status" / "backup.json"
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text(json.dumps({
        "status": "failed", "last_success_at": (now - timedelta(hours=50)).isoformat(),
        "last_success_backup": "aureon-20261031T020000Z",
    }))  # fmt: skip


async def test_alerts_dispatch_collects_dedups_and_retries(env: Env) -> None:
    await _seed_alert_sources(env)
    s = env.with_(DISCORD_WEBHOOK_URL="https://discord.test/api/webhooks/1/x")
    redis = Redis.from_url(TEST_REDIS_URL)
    try:
        cap = Capture()
        out = await alerts.dispatch(env.maker, s, redis, sender=cap)
        kinds = sorted(a["kind"] for a in cap.sent)
        assert kinds == ["approval", "backup", "heartbeat", "incident"], cap.sent
        assert out["sent"] == 4 and out["failed"] == 0
        hb = next(a for a in cap.sent if a["kind"] == "heartbeat")
        assert "research-x" in hb["title"]
        backup = next(a for a in cap.sent if a["kind"] == "backup")
        assert "last backup failed" in backup["detail"] and "50" in backup["detail"]
        appr = next(a for a in cap.sent if a["kind"] == "approval")
        assert appr["path"].startswith("/dev/approvals/") and "o/r#1" in appr["detail"]

        # Same state, next run: everything is deduplicated.
        again = Capture()
        out = await alerts.dispatch(env.maker, s, redis, sender=again)
        assert again.sent == [] and out["deduplicated"] == 4

        # An escalation (new severity) alerts again inside the window.
        async with env.maker() as db, db.begin():
            inc = await db.scalar(select(Incident).where(Incident.title == "DB slow"))
            assert inc is not None
            inc.severity = "critical"
        esc = Capture()
        await alerts.dispatch(env.maker, s, redis, sender=esc)
        assert [a["severity"] for a in esc.sent] == ["critical"]

        # A failed send releases the key: the next run retries it.
        await redis.flushdb()
        failing = Capture(ok=False)
        out = await alerts.dispatch(env.maker, s, redis, sender=failing)
        assert out["failed"] == 4 and out["sent"] == 0
        retry = Capture()
        out = await alerts.dispatch(env.maker, s, redis, sender=retry)
        assert out["sent"] == 4

        # The per-run cap.
        await redis.flushdb()
        capped = Capture()
        out = await alerts.dispatch(
            env.maker, s.model_copy(update={"ALERTS_MAX_PER_RUN": 2}), redis, sender=capped
        )
        assert len(capped.sent) == 2 and out["capped"] == 2
        assert capped.sent[0]["severity"] == "critical"  # most severe first
    finally:
        await redis.flushdb()
        await redis.aclose()


async def test_alerts_noop_without_discord_and_job_wiring(env: Env) -> None:
    await _seed_alert_sources(env)
    cap = Capture()
    out = await alerts.dispatch(env.maker, env.settings, None, sender=cap)
    assert out == {"skipped": "discord not configured", "sent": 0} and cap.sent == []
    assert "alerts-dispatch" not in {j[0] for j in default_jobs(env.settings)}
    on = env.with_(DISCORD_WEBHOOK_URL="https://discord.test/api/webhooks/1/x")
    assert ("alerts-dispatch", "alerts_dispatch", "*/5 * * * *") in default_jobs(on)
    assert "alerts-dispatch" not in {j[0] for j in default_jobs(on.model_copy(
        update={"ALERTS_CRON": ""}))}  # fmt: skip

    # The scheduler task posts through the Discord adapter (mocked webhook), with the
    # in-process dedup when Redis is absent.
    with respx.mock() as mock:
        hook = mock.post("https://discord.test/api/webhooks/1/x").mock(
            return_value=httpx.Response(204)
        )
        sched = Scheduler(on, env.maker, tasks=None)
        sched.ctx.redis = None
        out = await sched.tasks["alerts_dispatch"](sched.ctx)
        assert out["sent"] == 4 and hook.call_count == 4
        body = json.loads(hook.calls[0].request.content)
        assert body["allowed_mentions"] == {"parse": []}
        assert body["embeds"][0]["url"].startswith("https://spasta.online/dev")
        out = await sched.tasks["alerts_dispatch"](sched.ctx)
        assert out["sent"] == 0 and hook.call_count == 4


async def test_backup_status_unreadable_is_not_an_alert(env: Env) -> None:
    s = env.with_(DISCORD_WEBHOOK_URL="https://discord.test/api/webhooks/1/x",
                  BACKUP_STATUS_FILE=str(env.tmp / "missing.json"))  # fmt: skip
    cap = Capture()
    out = await alerts.dispatch(env.maker, s, None, sender=cap, dedup=alerts.Dedup(None, 60))
    assert out["backup"] == "unknown" and cap.sent == []
