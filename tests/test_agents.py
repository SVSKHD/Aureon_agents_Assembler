"""Phase 5: agent registry, pause/resume, runs/logs, scheduler, system status."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.imaging.generator import StubGenerator
from app.agents.registry import SCHEDULER, AgentRegistry
from app.auth.bootstrap import bootstrap_admin
from app.auth.security import hash_password
from app.core.config import Settings
from app.main import create_app
from app.models.agents import Agent, AgentLog, ScheduledJob
from app.models.auth import AuditEvent, User, UserSession
from app.workers.imaging import ImagingWorker
from app.workers.scheduler import Scheduler, TaskContext, backoff, next_fire

pytestmark = pytest.mark.db
PASSWORD = "correct horse battery staple"
OLLAMA = "http://ollama.test:11434"
DOCKER = "http://docker.test:2375"
TAGS = {"models": [{"name": "qwen2.5:7b", "size": 1, "details": {}}]}


class Env:
    def __init__(
        self,
        app: FastAPI,
        settings: Settings,
        admin: AsyncClient,
        user: AsyncClient,
        csrf: dict[str, str],
    ) -> None:
        self.app, self.settings, self.admin, self.user, self.csrf = (
            app,
            settings,
            admin,
            user,
            csrf,
        )

    @property
    def maker(self) -> async_sessionmaker[AsyncSession]:
        m: async_sessionmaker[AsyncSession] = self.app.state.sessionmaker
        return m

    def post(self, who: str, url: str, **kw: Any) -> Any:
        c = self.admin if who == "admin" else self.user
        return c.post(url, headers={"x-csrf-token": self.csrf[who]}, **kw)

    async def wait_agent(self, agent_id: str) -> dict[str, Any]:
        for _ in range(100):
            r = await self.admin.get(f"/api/agents/{agent_id}")
            if r.status_code == 200:
                body: dict[str, Any] = r.json()
                return body
            await asyncio.sleep(0.05)
        raise AssertionError(f"agent {agent_id} never registered")

    async def scalar(self, stmt: Any) -> Any:
        async with self.maker() as db:
            return await db.scalar(stmt)

    async def exec(self, stmt: Any) -> None:
        async with self.maker() as db, db.begin():
            await db.execute(stmt)


EnvFactory = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


@pytest.fixture
def make_env(db_clean: str, settings: Settings, tmp_path: Path) -> EnvFactory:
    @contextlib.asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[Env]:
        engine = create_async_engine(db_clean)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        await bootstrap_admin(maker, "admin@spasta.online", PASSWORD)
        async with engine.begin() as conn:
            await conn.execute(
                User.__table__.insert().values(
                    email="user@spasta.online", password_hash=hash_password(PASSWORD), role_id=2
                )
            )
        await engine.dispose()
        s = Settings.model_validate(
            {
                **settings.model_dump(),
                "DOCKER_API_URL": DOCKER,
                "HOST_STATUS_FILE": tmp_path / "host-services.json",
                **overrides,
            }
        )
        app = create_app(s)
        async with LifespanManager(app) as mgr:
            t = ASGITransport(app=mgr.app)
            async with (
                AsyncClient(transport=t, base_url="https://test") as a,
                AsyncClient(transport=t, base_url="https://test") as u,
            ):
                csrf = {}
                for who, c in (("admin", a), ("user", u)):
                    r = await c.post(
                        "/api/auth/login",
                        json={"email": f"{who}@spasta.online", "password": PASSWORD},
                    )
                    csrf[who] = r.json()["csrf_token"]
                yield Env(app, s, a, u, csrf)

    return factory


# --- registry -----------------------------------------------------------------------------


async def test_api_registers_ollama_agent_and_heartbeats(make_env: EnvFactory) -> None:
    async with make_env(AGENT_HEARTBEAT_SECONDS=0.1) as env:
        agent = await env.wait_agent("ollama")
        assert agent["state"] == "idle" and agent["kind"] == "llm"
        assert (
            agent["details"]["capacity"]
            == env.settings.OLLAMA_MAX_CONCURRENT + env.settings.OLLAMA_MAX_QUEUE
        )
        first = agent["last_heartbeat_at"]
        await asyncio.sleep(0.3)
        assert (await env.wait_agent("ollama"))["last_heartbeat_at"] > first
        listing = (await env.user.get("/api/agents")).json()["agents"]
        assert [a["id"] for a in listing] == ["ollama"]
    # Shutdown marks it stopped.
    engine = create_async_engine(env.settings.DATABASE_URL.get_secret_value())
    async with engine.connect() as conn:
        state = (await conn.execute(select(Agent.state).where(Agent.id == "ollama"))).scalar()
    await engine.dispose()
    assert state == "stopped"


async def test_stale_agent_is_reported(make_env: EnvFactory) -> None:
    async with make_env(AGENT_HEARTBEAT_SECONDS=3600, AGENT_STALE_SECONDS=30) as env:
        await env.wait_agent("ollama")
        await env.exec(update(Agent).values(last_heartbeat_at=func.now() - timedelta(minutes=5)))
        agent = (await env.user.get("/api/agents/ollama")).json()
        assert agent["state"] == "stale" and agent["reported_state"] == "idle"
        assert agent["seconds_since_heartbeat"] >= 299


async def test_unknown_agent_404(make_env: EnvFactory) -> None:
    async with make_env() as env:
        assert (await env.user.get("/api/agents/nope")).status_code == 404
        assert (await env.post("admin", "/api/agents/nope/pause")).status_code == 404


@respx.mock
async def test_pause_resume_ollama_is_admin_only_audited_and_blocks_chat(
    make_env: EnvFactory,
) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    done = {"model": "qwen2.5:7b", "message": {"content": ""}, "done": True, "eval_count": 1}
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            200,
            content=json.dumps({"message": {"content": "hi"}}).encode()
            + b"\n"
            + json.dumps(done).encode()
            + b"\n",
        )
    )
    chat = {"model": "qwen2.5:7b", "messages": [{"role": "user", "content": "secret prompt"}]}

    async with make_env() as env:
        await env.wait_agent("ollama")
        assert (await env.post("user", "/api/agents/ollama/pause")).status_code == 403
        assert (await env.admin.post("/api/agents/ollama/pause")).status_code == 403  # CSRF

        r = await env.post("admin", "/api/agents/ollama/pause")
        assert r.status_code == 200 and r.json()["state"] == "paused"
        blocked = await env.post("user", "/api/chat", json=chat)
        assert blocked.status_code == 503 and "paused" in blocked.json()["detail"]

        assert (await env.post("admin", "/api/agents/ollama/resume")).json()["paused"] is False
        ok = await env.post("user", "/api/chat", json=chat)
        assert ok.status_code == 200 and "event: done" in ok.text

        events = await env.scalar(select(func.array_agg(AuditEvent.event_type)))
        assert "agent_paused" in events and "agent_resumed" in events

        # The chat turn is recorded as an ollama run, without the prompt text.
        runs = (await env.user.get("/api/agents/ollama/runs")).json()
        assert runs["total"] == 1
        run = runs["items"][0]
        assert (run["task"], run["status"]) == ("chat", "succeeded")
        assert "secret prompt" not in json.dumps(run)
        assert run["duration_ms"] is not None


async def test_pause_reaches_agent_via_heartbeat(make_env: EnvFactory) -> None:
    async with make_env(AGENT_HEARTBEAT_SECONDS=0.05) as env:
        await env.wait_agent("ollama")
        # Paused directly in the DB (e.g. from another API process): the heartbeat picks it up.
        await env.exec(update(Agent).where(Agent.id == "ollama").values(paused=True))
        for _ in range(50):
            if env.app.state.agent_paused.get("ollama"):
                break
            await asyncio.sleep(0.05)
        assert env.app.state.agent_paused["ollama"] is True


async def test_logs_are_admin_only_and_filterable(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await env.wait_agent("ollama")
        reg = AgentRegistry(env.maker)
        run_id = await reg.start_run("ollama", "probe")
        await reg.log("ollama", "info", "hello", run_id=run_id, n=1)
        await reg.log("ollama", "error", "boom")
        await reg.finish_run(run_id, "failed", error="boom", details={"x": 1})

        assert (await env.user.get("/api/agents/ollama/logs")).status_code == 403
        logs = (await env.admin.get("/api/agents/ollama/logs")).json()
        assert logs["total"] == 2 and logs["items"][0]["message"] == "boom"
        errors = (await env.admin.get("/api/agents/ollama/logs?level=error")).json()
        assert errors["total"] == 1
        by_run = (await env.admin.get(f"/api/agents/ollama/logs?run_id={run_id}")).json()
        assert by_run["items"][0]["context"] == {"n": 1}

        failed = (await env.user.get("/api/agents/ollama/runs?status=failed")).json()
        assert failed["items"][0]["details"] == {"x": 1}
        assert failed["items"][0]["duration_ms"] >= 0
        detail = (await env.user.get("/api/agents/ollama")).json()
        assert detail["runs_last_24h"] == {"failed": 1}


async def test_imaging_worker_records_runs(make_env: EnvFactory) -> None:
    async with make_env(IMAGE_GENERATOR="stub") as env:
        reg = AgentRegistry(env.maker)
        from app.agents.registry import IMAGING

        await reg.register(IMAGING)
        r = await env.post("user", "/api/images/generate", json={"prompt": "a fox", "steps": 2})
        assert r.status_code == 202
        worker = ImagingWorker(env.settings, env.maker, StubGenerator(), reg)
        assert await worker.run_once()
        runs = (await env.user.get("/api/agents/imaging/runs")).json()["items"]
        assert (runs[0]["task"], runs[0]["status"]) == ("image_job", "succeeded")
        assert runs[0]["details"]["job_id"] == r.json()["id"]
        assert "a fox" not in json.dumps(runs)


# --- scheduler ----------------------------------------------------------------------------


def test_next_fire_uses_configured_timezone() -> None:
    # 00:00 UTC = 05:30 IST, so the next 03:17 IST is the following night, 21:47 UTC.
    nxt = next_fire("17 3 * * *", datetime(2026, 11, 2, tzinfo=UTC), "Asia/Kolkata")
    assert nxt == datetime(2026, 11, 2, 21, 47, tzinfo=UTC)


def test_backoff_is_exponential_and_capped() -> None:
    assert [backoff(60, a).total_seconds() for a in (1, 2, 3)] == [60, 120, 240]
    assert backoff(60, 20).total_seconds() == 3600


async def _scheduler(env: Env, tasks: dict[str, Any]) -> Scheduler:
    sched = Scheduler(env.settings, env.maker, tasks=tasks)
    await sched.registry.register(SCHEDULER)
    return sched


async def _add_job(env: Env, job_id: str, task: str, **kw: Any) -> None:
    async with env.maker() as db, db.begin():
        db.add(
            ScheduledJob(
                id=job_id,
                agent_id="scheduler",
                task=task,
                cron="0 4 * * *",
                next_run_at=datetime.now(UTC) - timedelta(seconds=1),
                **kw,
            )
        )


async def _job(env: Env, job_id: str) -> ScheduledJob:
    async with env.maker() as db:
        job = await db.get(ScheduledJob, job_id)
        assert job is not None
        return job


async def _make_due(env: Env, job_id: str) -> None:
    await env.exec(
        update(ScheduledJob)
        .where(ScheduledJob.id == job_id)
        .values(next_run_at=func.now() - timedelta(seconds=1))
    )


async def test_retry_with_backoff_then_success(make_env: EnvFactory) -> None:
    calls = 0

    async def flaky(ctx: TaskContext) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise RuntimeError(f"transient {calls}")
        return {"ok": calls}

    async with make_env() as env:
        sched = await _scheduler(env, {"flaky": flaky})
        await _add_job(env, "j", "flaky", max_retries=3, retry_backoff_seconds=60)

        before = datetime.now(UTC)
        assert await sched.tick()
        j = await _job(env, "j")
        assert (j.attempt, j.last_status, j.locked_until) == (1, "retrying", None)
        assert "transient 1" in (j.last_error or "")
        assert timedelta(seconds=55) < (j.next_run_at - before) < timedelta(seconds=65)  # type: ignore[operator]
        assert not await sched.tick()  # not due yet

        await _make_due(env, "j")
        await sched.tick()
        j = await _job(env, "j")
        assert j.attempt == 2
        assert (j.next_run_at - datetime.now(UTC)) > timedelta(seconds=110)  # type: ignore[operator]

        await _make_due(env, "j")
        await sched.tick()
        j = await _job(env, "j")
        assert (j.attempt, j.last_status, j.consecutive_failures) == (0, "succeeded", 0)
        assert j.last_error is None
        assert j.next_run_at == next_fire("0 4 * * *", j.last_run_at, "Asia/Kolkata")  # type: ignore[arg-type]

        runs = (await env.user.get("/api/agents/scheduler/runs")).json()["items"]
        assert [(r["status"], r["attempt"]) for r in runs][::-1] == [
            ("failed", 1),
            ("failed", 2),
            ("succeeded", 3),
        ]
        assert runs[0]["details"] == {"ok": 3} and runs[0]["scheduled_job_id"] == "j"
        levels = (await env.admin.get("/api/agents/scheduler/logs")).json()["items"]
        assert [x["level"] for x in levels][::-1] == ["warning", "warning", "info"]


async def test_retries_exhausted_waits_for_next_cron(make_env: EnvFactory) -> None:
    async def broken(ctx: TaskContext) -> dict[str, Any]:
        raise ValueError("always")

    async with make_env() as env:
        sched = await _scheduler(env, {"broken": broken})
        await _add_job(env, "b", "broken", max_retries=1, retry_backoff_seconds=1)
        await sched.tick()
        await _make_due(env, "b")
        await sched.tick()
        j = await _job(env, "b")
        assert (j.attempt, j.last_status, j.consecutive_failures) == (0, "failed", 1)
        assert j.next_run_at == next_fire("0 4 * * *", j.last_run_at, "Asia/Kolkata")  # type: ignore[arg-type]
        logs = (await env.admin.get("/api/agents/scheduler/logs?level=error")).json()
        assert "retries exhausted" in logs["items"][0]["message"]


async def test_timeout_and_unknown_task(make_env: EnvFactory) -> None:
    async def slow(ctx: TaskContext) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {}

    async with make_env() as env:
        sched = await _scheduler(env, {"slow": slow})
        await _add_job(env, "s", "slow", timeout_seconds=1, max_retries=0)
        await _add_job(env, "u", "does_not_exist", max_retries=5)
        t0 = time.monotonic()
        await sched.tick()
        await sched.tick()
        assert time.monotonic() - t0 < 5
        assert "timed out after 1s" in ((await _job(env, "s")).last_error or "")
        unknown = await _job(env, "u")
        # Unknown task: no retries, straight to the next cron slot.
        assert (unknown.last_status, unknown.attempt) == ("failed", 0)
        assert "unknown task" in (unknown.last_error or "")


async def test_paused_job_paused_agent_and_leases(make_env: EnvFactory) -> None:
    ran: list[str] = []

    async def mark(ctx: TaskContext) -> dict[str, Any]:
        ran.append("x")
        return {}

    async with make_env() as env:
        sched = await _scheduler(env, {"mark": mark})
        await _add_job(env, "p", "mark", paused=True)
        await _add_job(env, "l", "mark", locked_until=datetime.now(UTC) + timedelta(minutes=5))
        assert not await sched.tick()  # paused + leased jobs are skipped

        # An expired lease (crashed run) becomes claimable again.
        await env.exec(
            update(ScheduledJob)
            .where(ScheduledJob.id == "l")
            .values(locked_until=func.now() - timedelta(seconds=1))
        )
        await env.exec(update(Agent).where(Agent.id == "scheduler").values(paused=True))
        assert not await sched.tick()  # whole scheduler agent paused
        await env.exec(update(Agent).where(Agent.id == "scheduler").values(paused=False))
        assert await sched.tick() and ran == ["x"]


async def test_concurrent_schedulers_run_a_job_once(make_env: EnvFactory) -> None:
    count = 0

    async def once(ctx: TaskContext) -> dict[str, Any]:
        nonlocal count
        count += 1
        await asyncio.sleep(0.2)
        return {}

    async with make_env() as env:
        a = await _scheduler(env, {"once": once})
        b = Scheduler(env.settings, env.maker, tasks={"once": once})
        await _add_job(env, "o", "once")
        results = await asyncio.gather(a.tick(), b.tick())
        assert sorted(results) == [False, True] and count == 1


async def test_default_jobs_and_builtin_tasks(make_env: EnvFactory) -> None:
    async with make_env() as env:
        sched = await _scheduler(env, {})
        sched.tasks = __import__("app.workers.scheduler", fromlist=["TASKS"]).TASKS
        await sched.ensure_default_jobs()
        await sched.ensure_default_jobs()  # idempotent
        jobs = (await env.admin.get("/api/scheduler/jobs")).json()
        assert {j["id"] for j in jobs} == {
            "purge-agent-history",
            "purge-expired-sessions",
            "refresh-model-inventory",
        }
        assert all(j["next_run_at"] for j in jobs)

        # Old logs + sessions get purged.
        reg = AgentRegistry(env.maker)
        await reg.log("scheduler", "info", "old")
        await env.exec(update(AgentLog).values(created_at=func.now() - timedelta(days=40)))
        await reg.log("scheduler", "info", "fresh")
        async with env.maker() as db, db.begin():
            uid = await db.scalar(select(User.id).where(User.email == "user@spasta.online"))
            db.add(
                UserSession(
                    user_id=uid,
                    token_hash=b"x" * 32,
                    expires_at=datetime.now(UTC) - timedelta(days=8),
                )
            )
        for job_id in ("purge-agent-history", "purge-expired-sessions"):
            assert (await env.post("admin", f"/api/scheduler/jobs/{job_id}/run")).status_code == 200
            assert await sched.tick()
        assert await env.scalar(select(func.count()).select_from(UserSession)) == 2
        messages = await env.scalar(select(func.array_agg(AgentLog.message)))
        assert "old" not in messages and "fresh" in messages
        runs = (await env.user.get("/api/agents/scheduler/runs?status=succeeded")).json()
        details = {r["task"]: r["details"] for r in runs["items"]}
        assert details["purge_agent_history"]["logs_deleted"] == 1
        assert details["purge_expired_sessions"]["sessions_deleted"] == 1


async def test_scheduler_api_admin_only_and_audited(make_env: EnvFactory) -> None:
    async with make_env() as env:
        await _scheduler(env, {})
        await _add_job(env, "k", "mark")
        assert (await env.user.get("/api/scheduler/jobs")).status_code == 403
        assert (await env.post("user", "/api/scheduler/jobs/k/pause")).status_code == 403
        assert (await env.post("admin", "/api/scheduler/jobs/k/pause")).json()["paused"] is True
        assert (await env.post("admin", "/api/scheduler/jobs/k/resume")).json()["paused"] is False
        await env.exec(
            update(ScheduledJob).values(next_run_at=func.now() + timedelta(days=1), attempt=2)
        )
        run = (await env.post("admin", "/api/scheduler/jobs/k/run")).json()
        assert run["attempt"] == 0
        assert datetime.fromisoformat(run["next_run_at"]) <= datetime.now(UTC)
        assert (await env.post("admin", "/api/scheduler/jobs/missing/run")).status_code == 404
        events = await env.scalar(select(func.array_agg(AuditEvent.event_type)))
        for e in ("scheduled_job_pause", "scheduled_job_resume", "scheduled_job_run"):
            assert e in events


# --- system status ------------------------------------------------------------------------


@respx.mock
async def test_system_status(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    respx.get(f"{OLLAMA}/api/ps").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})
    )
    docker = respx.get(f"{DOCKER}/containers/json").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "Names": ["/aureon-api"],
                    "Image": "aureon-agentic:dev",
                    "State": "running",
                    "Status": "Up 2 hours (healthy)",
                },
                {
                    "Names": ["/open-webui"],
                    "Image": "open-webui",
                    "State": "running",
                    "Status": "Up 3 days",
                },
            ],
        )
    )
    async with make_env() as env:
        env.settings.HOST_STATUS_FILE.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC).isoformat(),
                    "units": {"cloudflared": "active", "docker": "active", "nginx": "active"},
                }
            )
        )
        assert (await env.user.get("/api/system/status")).status_code == 403

        body = (await env.admin.get("/api/system/status")).json()
        host = body["host"]
        assert host["cpu"]["count"] >= 1 and 0 <= host["memory"]["percent"] <= 100
        assert set(host["disks"]) == {"ssd", "hdd"}
        assert host["disks"]["hdd"]["path"] == str(env.settings.DATA_ROOT)
        assert body["postgres"]["ok"] and body["postgres"]["connections"] >= 1
        assert body["redis"]["ok"]
        assert body["ollama"]["loaded_models"] == ["qwen2.5:7b"]
        assert body["imaging"]["worker"] == "absent" and body["imaging"]["ok"] is False
        assert body["docker"]["ok"] and len(body["docker"]["containers"]) == 2
        assert docker.calls[0].request.url.params["all"] == "1"
        assert body["host_services"]["units"]["cloudflared"] == "active"
        assert body["host_services"]["ok"]
        assert body["status"] == "degraded"  # no imaging worker registered


@respx.mock
async def test_system_status_reports_problems(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{OLLAMA}/api/ps").mock(side_effect=httpx.ConnectError("down"))
    respx.get(f"{DOCKER}/containers/json").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"Names": ["/aureon-imaging"], "State": "exited", "Status": "Exited (1)"},
                {"Names": ["/aureon-api"], "State": "running", "Status": "Up (unhealthy)"},
            ],
        )
    )
    async with make_env() as env:
        f = env.settings.HOST_STATUS_FILE
        f.write_text(json.dumps({"units": {"cloudflared": "failed"}}))
        old = time.time() - 3600
        import os

        os.utime(f, (old, old))
        body = (await env.admin.get("/api/system/status")).json()
        assert body["status"] == "degraded"
        assert body["ollama"] == {
            "ok": False,
            "error": "OllamaUnavailable",
            "latency_ms": body["ollama"]["latency_ms"],
        }
        assert sorted(body["docker"]["not_running_or_unhealthy"]) == [
            "aureon-api",
            "aureon-imaging",
        ]
        hs = body["host_services"]
        assert (
            hs["ok"] is False and hs["stale"] is True and hs["units"] == {"cloudflared": "failed"}
        )

        f.unlink()
        missing = (await env.admin.get("/api/system/status")).json()["host_services"]
        assert "not installed" in missing["error"]
