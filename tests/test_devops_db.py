"""DevOps monitoring against PostgreSQL (docs/32-devops-monitoring.md).

GitHub is mocked by respx and webhooks are signed exactly like GitHub signs them; deploys go
through the real deploy_record CLI path; runtime reports through the real runtime_record
parser. Covers: signatures, duplicate / out-of-order deliveries, merged PR vs deployment,
failed CI, green CI without a deployment, rollback, unhealthy and recovered services,
stream reconnect + replay, authorization, rate limits, missing GitHub credentials, a DB
outage during streaming, multiple repositories, notifications and retention.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.main import create_app
from app.services import devops, devops_read
from app.services import github_webhooks as hooks
from app.workers import deploy_record, runtime_record
from app.workers.scheduler import TaskContext, default_jobs, devops_backfill, devops_purge
from tests.test_finance_db import PASSWORD, Env
from tests.test_github_db import API, EnvFactory, gh_env

pytestmark = pytest.mark.db
FIXTURES = (gh_env,)

SECRET = "whsec-" + "d" * 58
HOOK = "/api/v1/github/webhooks"
SHA = "a" * 40
SHA2 = "b" * 40
D = "/api/v1/devops"

DevEnv = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


def ago(minutes: float = 0) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


@pytest.fixture
def env(gh_env: EnvFactory) -> DevEnv:
    @contextlib.asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[Env]:
        overrides.setdefault("GITHUB_ALLOWED_OWNERS", ["me"])
        overrides.setdefault("GITHUB_WEBHOOK_SECRET", SecretStr(SECRET))
        overrides.setdefault("GITHUB_WEBHOOK_RETRY_BASE_SECONDS", 0.0)
        overrides.setdefault("DEVOPS_STREAM_POLL_SECONDS", 0.05)
        async with gh_env(**overrides) as e:
            await e.sql(
                "TRUNCATE github.webhook_events, github.workflow_runs, github.workflow_jobs, "
                "devops.repo_events, devops.deployments, devops.services, "
                "devops.service_snapshots, devops.events, devops.notification_prefs, "
                "devops.notification_reads RESTART IDENTITY CASCADE"
            )
            await e.sql(
                "INSERT INTO github.repositories (id, full_name, owner, name, private, html_url, "
                "default_branch) VALUES (1, 'me/app', 'me', 'app', true, 'x', 'main'), "
                "(2, 'me/web', 'me', 'web', true, 'x', 'dev01')"
            )
            yield e

    return factory


@contextlib.asynccontextmanager
async def public(e: Env) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=e.app), base_url="https://test") as c:
        yield c


async def hook(
    c: AsyncClient, event: str, payload: dict[str, Any], delivery: str, secret: str = SECRET
) -> httpx.Response:
    body = json.dumps(payload).encode()
    return await c.post(HOOK, content=body, headers={
        "content-type": "application/json", "x-github-event": event,
        "x-github-delivery": delivery, "x-hub-signature-256": hooks.signature_for(secret, body),
    })  # fmt: skip


async def drain(e: Env) -> list[hooks.Processed]:
    return await hooks.drain(e.maker, e.app.state.settings)


def run(rid: int, repo: str = "me/app", **kw: Any) -> dict[str, Any]:
    return {
        "action": kw.pop("action", "completed"),
        "repository": {"full_name": repo, "id": 1},
        "workflow_run": {
            "id": rid, "name": "CI", "workflow_id": 10, "path": ".github/workflows/ci.yml",
            "event": "push", "head_branch": "main", "head_sha": SHA, "status": "completed",
            "conclusion": "success", "run_attempt": 1,
            "html_url": f"https://github.com/{repo}/actions/runs/{rid}",
            "created_at": ago(10), "updated_at": ago(5), **kw,
        },
    }  # fmt: skip


def pr(n: int, repo: str = "me/app", *, action: str = "closed", merged: bool = True,
       **kw: Any) -> dict[str, Any]:  # fmt: skip
    body = {
        "number": n, "title": f"Change {n}", "state": "closed" if action == "closed" else "open",
        "html_url": f"https://github.com/{repo}/pull/{n}", "user": {"login": "dev"},
        "base": {"ref": "main"}, "head": {"ref": f"feat/{n}", "sha": SHA2},
        "created_at": ago(60), "updated_at": ago(1),
        "closed_at": ago(1) if action == "closed" else None,
        "merged_at": ago(1) if (action == "closed" and merged) else None,
        "merge_commit_sha": SHA if merged else None, "merged_by": {"login": "lead"}, **kw,
    }  # fmt: skip
    return {"action": action, "number": n, "repository": {"full_name": repo, "id": 1},
            "pull_request": body}  # fmt: skip


async def feed_types(e: Env) -> list[str]:
    rows = await e.sql("SELECT type FROM devops.events ORDER BY id")
    return [r[0] for r in rows]


async def get(e: Env, path: str, who: str = "admin", **params: Any) -> Any:
    r = await e.get(who, D + path, params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def deploy(e: Env, target: str, sha: str, prev: str | None, exit_code: int,
                 phases: dict[str, str]) -> int:  # fmt: skip
    """start + finish exactly as deploy_record.run does (agent run + devops row)."""
    from app.agents.registry import AgentRegistry

    s = e.app.state.settings
    registry = AgentRegistry(e.maker)
    await registry.register(deploy_record.DEPLOYER, {})
    p = deploy_record.build_parser()
    a = p.parse_args(["start", "--target", target, "--sha", sha, "--from", prev or "none",
                      "--log", "/mnt/spasta-data/logs/deploy.log"])  # fmt: skip
    run_id = await registry.start_run("deployer", f"deploy:{target}",
                                      details=deploy_record.start_details(a))  # fmt: skip
    async with e.maker() as db, db.begin():
        await devops.server_deploy_started(db, s, agent_run_id=run_id, target=target, sha=sha,
                                           previous_sha=prev, log_path=a.log)  # fmt: skip
    status = "succeeded" if exit_code == 0 else "failed"
    finish = ["finish", "--run-id", str(run_id), "--status", status, "--exit-code", str(exit_code)]
    f = p.parse_args([*finish, "--phases-json", json.dumps(phases)])
    async with e.maker() as db, db.begin():
        await devops.server_deploy_finished(
            db, s, agent_run_id=run_id, exit_code=deploy_record.exit_code_of(f),
            phases=f.phases, error=deploy_record.finish_error(f),
        )  # fmt: skip
    return run_id


def runtime(host: str = "srv1", observed: datetime | None = None, *, api: str = "healthy",
            worker_state: str = "running", ready: bool = True, expected: tuple[str, ...] = ()
            ) -> devops.RuntimeReport:  # fmt: skip
    doc = {
        "schema": "aureon.runtime/v1", "host": host,
        "observed_at": (observed or datetime.now(UTC)).isoformat(),
        "services": [
            {"service": "api", "state": "running", "health": api, "image": "aureon-agentic:dev",
             "revision": SHA, "container_id": "c1"},
            {"service": "worker", "state": worker_state, "health": "",
             "exit_code": 137 if worker_state == "exited" else 0},
            {"service": "migrate", "state": "exited", "exit_code": 0},
        ],
        "expected": list(expected),
        "readiness": {"service": "api", "http_status": 200 if ready else 503, "ok": ready,
                      "failing": [] if ready else ["postgres"]},
    }  # fmt: skip
    return runtime_record.parse(json.dumps(doc).encode())


async def record_runtime(e: Env, report: devops.RuntimeReport) -> dict[str, Any]:
    async with e.maker() as db, db.begin():
        return await devops.record_runtime(db, e.app.state.settings, report)


# --- webhooks: signature, duplicates, out-of-order ------------------------------------------


async def test_unsigned_or_badly_signed_github_events_store_nothing(env: DevEnv) -> None:
    async with env() as e, public(e) as c:
        r = await hook(c, "pull_request", pr(1), "bad-1", secret="not-the-secret")
        assert r.status_code == 401
        body = json.dumps(pr(1)).encode()
        r = await c.post(HOOK, content=body, headers={
            "content-type": "application/json", "x-github-event": "pull_request",
            "x-github-delivery": "bad-2"})  # fmt: skip
        assert r.status_code == 401
        assert await drain(e) == []
        assert await e.sql("SELECT count(*) FROM devops.repo_events") == [(0,)]
        assert await feed_types(e) == []


async def test_merged_pr_is_recorded_once_across_redelivery_and_backfill(env: DevEnv) -> None:
    async with env() as e, public(e) as c:
        payload = pr(7)
        assert (await hook(c, "pull_request", payload, "p-1")).json()["status"] == "accepted"
        # GitHub redelivers the same delivery id, and a second delivery repeats the fact.
        assert (await hook(c, "pull_request", payload, "p-1")).json()["status"] == "duplicate"
        assert (await hook(c, "pull_request", payload, "p-2")).json()["status"] == "accepted"
        assert [x.status for x in await drain(e)] == ["processed", "processed"]
        kinds = await e.sql("SELECT kind FROM devops.repo_events ORDER BY occurred_at")
        assert kinds == [("pr.opened",), ("pr.merged",)]
        assert await feed_types(e) == ["pr.merged"]
        # The backfill reads the same PR from the API: still one fact, one event.
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/user").mock(return_value=httpx.Response(200, json={"login": "me"}))
            router.get(f"{API}/repos/me/app/pulls").mock(
                return_value=httpx.Response(200, json=[payload["pull_request"]])
            )
            router.get(f"{API}/repos/me/web/pulls").mock(return_value=httpx.Response(200, json=[]))
            router.get(url__regex=rf"{API}/repos/me/\w+/deployments").mock(
                return_value=httpx.Response(200, json=[])
            )
            ctx = TaskContext(e.app.state.settings, e.maker, None, github=e.app.state.github)
            out = await devops_backfill(ctx)
        assert out["pr_events"] == 0 and out["repos"] == 2
        assert await feed_types(e) == ["pr.merged"]


async def test_out_of_order_build_events_never_regress_or_duplicate(env: DevEnv) -> None:
    async with env() as e, public(e) as c:
        done = run(100, updated_at=ago(2))
        late_start = run(100, action="in_progress", status="in_progress", conclusion=None,
                         updated_at=ago(4))  # fmt: skip
        await hook(c, "workflow_run", done, "w-1")
        await hook(c, "workflow_run", late_start, "w-2")  # older, delivered later
        await hook(c, "workflow_run", done, "w-3")  # repeat of the completion
        await drain(e)
        assert await e.sql("SELECT status, conclusion FROM github.workflow_runs") == [
            ("completed", "success")
        ]
        assert await feed_types(e) == ["build.completed"]  # no build.started after completion


async def test_failed_ci_is_reported_with_its_failed_jobs(env: DevEnv) -> None:
    async with env() as e, public(e) as c:
        await hook(c, "workflow_run", run(200, action="in_progress", status="in_progress",
                                          conclusion=None, updated_at=ago(6)), "f-1")  # fmt: skip
        await hook(c, "workflow_run", run(200, conclusion="failure", updated_at=ago(3)), "f-2")
        job = {"action": "completed", "repository": {"full_name": "me/app"},
               "workflow_job": {"id": 9, "run_id": 200, "name": "pytest", "status": "completed",
                                "conclusion": "failure", "run_attempt": 1,
                                "html_url": "https://github.com/me/app/runs/9"}}  # fmt: skip
        await hook(c, "workflow_job", job, "f-3")
        await drain(e)
        assert await feed_types(e) == ["build.started", "build.failed"]
        ov = await get(e, "/overview")
        (fail,) = ov["recent_failures"]
        assert fail["status"] == "FAILED" and fail["conclusion"] == "failure"
        assert fail["failed_jobs"] == [
            {"name": "pytest", "status": "FAILED", "url": "https://github.com/me/app/runs/9"}
        ]
        assert ov["building"] == []
        page = await get(e, "/builds", status="FAILED")
        assert page["total"] == 1 and page["items"][0]["id"] == 200
        assert (await get(e, "/builds", status="SUCCEEDED"))["total"] == 0


async def test_old_history_does_not_flood_the_feed(env: DevEnv) -> None:
    async with env() as e, public(e) as c:
        await hook(c, "workflow_run", run(300, conclusion="failure", updated_at=ago(60 * 72),
                                          created_at=ago(60 * 73)), "o-1")  # fmt: skip
        await drain(e)
        assert await feed_types(e) == []
        assert (await get(e, "/builds"))["items"][0]["status"] == "FAILED"  # still listed


# --- merge vs CI vs deployment --------------------------------------------------------------


async def test_merge_and_green_ci_are_not_a_deployment(env: DevEnv) -> None:
    async with env() as e, public(e) as c:
        await hook(c, "pull_request", pr(5), "m-1")
        await hook(c, "workflow_run", run(400), "m-2")
        await drain(e)
        repos = {r["repo"]: r for r in await get(e, "/repositories")}
        app = repos["me/app"]
        assert app["pr"]["status"] == "MERGED" and app["pr"]["number"] == 5
        assert app["ci"]["status"] == "SUCCEEDED" and app["ci"]["branch"] == "main"
        assert app["deployment"] is None and app["deployed"] is None
        ov = await get(e, "/overview")
        assert ov["deployments"] == []
        assert [p["number"] for p in ov["recently_merged"]] == [5]


async def test_verified_unverified_and_rolled_back_deploys(env: DevEnv) -> None:
    async with env() as e:
        full = {"build_started": ago(5), "build_finished": ago(4), "deploy_started": ago(3),
                "deploy_finished": ago(2), "health_ok": ago(1)}  # fmt: skip
        await deploy(e, "backend", SHA, SHA2, 0, full)
        await deploy(e, "frontend", SHA, None, 0, {})  # its deploy script records no phases
        failed = {
            "build_started": ago(1),
            "failure": "readiness: HTTP 503",
            "rollback_started": ago(1),
        }
        await deploy(e, "backend", SHA2, SHA, 1, failed)
        await deploy(e, "backend", SHA2, SHA, 2, failed)
        items = (await get(e, "/deployments"))["items"]
        by = [(d["target"], d["status"], d["health"], d["rolled_back"], d["rollback_status"])
              for d in reversed(items)]  # fmt: skip
        assert by == [
            ("backend", "SUCCEEDED", "VERIFIED", False, None),
            ("frontend", "UNVERIFIED", "NOT_VERIFIED", False, None),
            ("backend", "ROLLED_BACK", "FAILED", True, "healthy"),
            ("backend", "FAILED", "FAILED", True, "unhealthy"),
        ]
        first = items[-1]
        assert first["repo"] == "SVSKHD/Aureon_agents_Assembler" and first["branch"] == "main"
        assert first["previous_sha"] == SHA2 and first["health_verified_at"] is not None
        assert first["build_started_at"] and first["deploy_finished_at"]
        assert items[1]["failure_reason"] == "readiness: HTTP 503"
        assert await feed_types(e) == [
            "deployment.started", "deployment.completed", "deployment.started",
            "deployment.completed", "deployment.started", "deployment.rolled_back",
            "deployment.started", "deployment.failed",
        ]  # fmt: skip
        sev = await e.sql("SELECT severity FROM devops.events WHERE type='deployment.completed' "
                          "ORDER BY id")  # fmt: skip
        assert sev == [("success",), ("warning",)]  # unverified is never shown as a success
        repos = {r["repo"]: r for r in await get(e, "/repositories")}
        backend = repos["SVSKHD/Aureon_agents_Assembler"]
        assert backend["deployment"]["status"] == "FAILED"
        assert backend["deployed"]["sha"] == SHA  # the last VERIFIED release
        assert (await get(e, "/deployments", status="ROLLED_BACK"))["total"] == 1
        assert (await get(e, "/deployments", environment="staging"))["total"] == 0


async def test_github_deployment_statuses_out_of_order(env: DevEnv) -> None:
    async with env() as e, public(e) as c:
        dep = {"id": 55, "sha": SHA, "ref": "main", "environment": "preview",
               "created_at": ago(10), "creator": {"login": "bot"}}  # fmt: skip
        ok = {"action": "created", "repository": {"full_name": "me/web", "id": 2},
              "deployment": dep, "deployment_status": {"id": 2, "state": "success",
              "created_at": ago(2), "target_url": "https://preview"}}  # fmt: skip
        pending = {**ok, "deployment_status": {"id": 1, "state": "in_progress",
                                               "created_at": ago(8)}}  # fmt: skip
        await hook(c, "deployment_status", ok, "g-1")
        await hook(c, "deployment_status", pending, "g-2")  # older, arrives late
        await drain(e)
        (d,) = (await get(e, "/deployments", source="github"))["items"]
        assert (d["status"], d["environment"], d["health"]) == ("SUCCEEDED", "preview", "UNKNOWN")
        assert await feed_types(e) == ["deployment.started", "deployment.completed"]


# --- runtime ----------------------------------------------------------------------------------


async def test_unhealthy_service_and_recovery_notify_once_each(env: DevEnv) -> None:
    async with env() as e:
        t0 = datetime.now(UTC) - timedelta(minutes=3)
        await record_runtime(e, runtime(observed=t0))
        assert await feed_types(e) == []  # healthy from the first sight: nothing to say
        await record_runtime(e, runtime(observed=t0 + timedelta(minutes=1), worker_state="exited",
                                        ready=False))  # fmt: skip
        await record_runtime(e, runtime(observed=t0 + timedelta(minutes=2), worker_state="exited",
                                        ready=False))  # the same state again  # fmt: skip
        assert await feed_types(e) == ["service.unhealthy", "service.unhealthy"]  # api + worker
        svc = {s["service"]: s for s in await get(e, "/services")}
        assert svc["worker"]["status"] == "STOPPED"
        assert svc["api"]["status"] == "UNHEALTHY"
        assert "readiness failed (HTTP 503): postgres" in svc["api"]["failure_reason"]
        assert svc["migrate"]["status"] == "HEALTHY"  # a one-shot that exited 0
        assert svc["api"]["revision"] == SHA
        await record_runtime(e, runtime(observed=t0 + timedelta(minutes=3)))
        assert (await feed_types(e))[-2:] == ["service.recovered", "service.recovered"]
        assert {s["status"] for s in await get(e, "/services")} == {"HEALTHY"}
        snaps = await e.sql("SELECT count(*) FROM devops.service_snapshots")
        assert snaps == [(12,)]
        # A late, older report never moves a service back.
        await record_runtime(e, runtime(observed=t0, worker_state="exited"))
        assert {s["status"] for s in await get(e, "/services")} == {"HEALTHY"}


async def test_missing_expected_service_and_stale_collector(env: DevEnv) -> None:
    async with env() as e:
        old = datetime.now(UTC) - timedelta(minutes=30)
        await record_runtime(e, runtime(observed=old, expected=("scheduler",)))
        svc = {s["service"]: s for s in await get(e, "/services")}
        # The collector has not reported for 30 min: nothing can be verified.
        assert {s["status"] for s in svc.values()} == {"UNKNOWN"}
        assert all(s["stale"] for s in svc.values())
        assert svc["scheduler"]["state"] == "missing"
        ov = await get(e, "/overview")
        assert ov["collector"]["stale"] is True
        await record_runtime(e, runtime(expected=("scheduler",)))
        svc = {s["service"]: s for s in await get(e, "/services")}
        assert svc["scheduler"]["status"] == "STOPPED"
        assert svc["scheduler"]["failure_reason"] == "container not found"
        assert (await get(e, "/services", status="STOPPED"))[0]["service"] == "scheduler"


# --- live feed: reconnect, replay, DB outage --------------------------------------------------


async def _collect(gen: Any, n: int) -> list[str]:
    out: list[str] = []

    async def take() -> None:
        async for chunk in gen:
            if chunk.startswith(("id:", "event:")):
                out.append(chunk)
            if len(out) >= n:
                return

    await asyncio.wait_for(take(), 5.0)
    return out


def _stream(maker: Any, current: Any, cursor: str | None) -> Any:
    return devops_read.stream(
        maker, current, last_event_id=cursor, types=[], repo=None,
        poll_seconds=0.05, heartbeat_seconds=5, max_seconds=30,
    )  # fmt: skip


def _ids(chunks: list[str]) -> list[tuple[str, str]]:
    out = []
    for ch in chunks:
        lines = dict(line.split(": ", 1) for line in ch.strip().splitlines() if ": " in line)
        out.append((lines.get("id", ""), lines.get("event", "")))
    return out


async def test_stream_replays_after_reconnect_without_duplicates(env: DevEnv) -> None:
    async with env() as e, public(e) as c:
        await hook(c, "workflow_run", run(500, conclusion="failure"), "s-1")
        await hook(c, "pull_request", pr(9), "s-2")
        await drain(e)
        current = await _session(e)
        gen = _stream(e.maker, current, "0")
        first = _ids(await _collect(gen, 3))
        await gen.aclose()
        assert first == [("0", "hello"), ("1", "build.failed"), ("2", "pr.merged")]
        # Offline: two more things happen. Reconnect with Last-Event-ID = 2.
        await hook(c, "workflow_run", run(501), "s-3")
        await drain(e)
        await record_runtime(e, runtime(worker_state="exited"))
        gen = _stream(e.maker, current, "2")
        again = _ids(await _collect(gen, 3))
        await gen.aclose()
        assert again == [("2", "hello"), ("3", "build.completed"), ("4", "service.unhealthy")]
        # The polling fallback returns the same events.
        f = await get(e, "/feed", after_id=2)
        assert [x["id"] for x in f["items"]] == [3, 4] and f["last_id"] == 4
        assert f["has_more"] is False
        f = await get(e, "/feed", after_id=0, limit=1)
        assert f["has_more"] is True and f["last_id"] == 1
        # Without a cursor the stream starts at the newest event (no backlog).
        gen = _stream(e.maker, current, None)
        assert _ids(await _collect(gen, 1)) == [("4", "hello")]
        await gen.aclose()


async def test_stream_endpoint_auth_and_wire_format(env: DevEnv) -> None:
    async with env(DEVOPS_STREAM_MAX_SECONDS=10.0) as e, public(e) as c:
        assert (await c.get(D + "/stream")).status_code == 401
        assert (await e.get("a", D + "/stream")).status_code == 403
        await hook(c, "pull_request", pr(3), "x-1")
        await drain(e)
        async with e.clients["admin"].stream(
            "GET", D + "/stream", params={"last_event_id": "0"}
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            buf = ""
            async for chunk in r.aiter_text():
                buf += chunk
                if "event: pr.merged" in buf:
                    break
        assert "id: 1\nevent: pr.merged\ndata: " in buf
        data = json.loads(buf.split("event: pr.merged\ndata: ", 1)[1].split("\n", 1)[0])
        assert data["type"] == "pr.merged" and data["repo"] == "me/app"


async def test_stream_survives_a_database_outage(
    env: DevEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with env() as e, public(e) as c:
        current = await _session(e)
        real = e.maker
        calls = {"n": 0}

        def flaky() -> Any:
            calls["n"] += 1
            if calls["n"] in (2, 3):  # two polls while "Postgres restarts"
                raise OSError("connection refused")
            return real()

        gen = _stream(flaky, current, "0")
        hello = _ids(await _collect(gen, 1))
        assert hello == [("0", "hello")]
        await hook(c, "pull_request", pr(4), "db-1")
        await drain(e)
        after = _ids(await _collect(gen, 1))
        await gen.aclose()
        assert after == [("1", "pr.merged")] and calls["n"] >= 4


async def _session(e: Env) -> Any:
    from sqlalchemy import select

    from app.auth.deps import CurrentSession
    from app.models.auth import User, UserSession

    async with e.maker() as db:
        s = await db.scalar(
            select(UserSession).where(UserSession.user_id == e.ids["admin"])
            .order_by(UserSession.created_at.desc()).limit(1)
        )  # fmt: skip
        u = await db.get(User, e.ids["admin"])
        assert s is not None and u is not None
        return CurrentSession(user=u, session=s, token="")


# --- authorization, GitHub credentials, rate limits ------------------------------------------

READS = ("/overview", "/repositories", "/events", "/builds", "/deployments", "/services",
         "/feed", "/notifications", "/notifications/preferences")  # fmt: skip


async def test_authorization(env: DevEnv) -> None:
    async with env() as e, public(e) as anon:
        for path in READS:
            assert (await anon.get(D + path)).status_code == 401, path
            assert (await e.get("a", D + path)).status_code == 403, path
            assert (await e.get("admin", D + path)).status_code == 200, path
        # Writes need the CSRF header too.
        r = await e.clients["admin"].post(D + "/notifications/read", json={"up_to_id": 1})
        assert r.status_code == 403
        r = await e.send("a", "POST", D + "/notifications/read", json={"up_to_id": 1})
        assert r.status_code == 403
        r = await e.send("admin", "PUT", D + "/notifications/preferences",
                         json={"repos": ["not a repo"]})  # fmt: skip
        assert r.status_code == 422
        # Nothing secret is ever in the answers.
        text = json.dumps(await get(e, "/overview"))
        assert SECRET not in text and "github_pat_" not in text


async def test_missing_github_credentials(env: DevEnv) -> None:
    async with env(GITHUB_TOKEN=None) as e:
        ov = await get(e, "/overview")
        assert ov["github"]["configured"] is False and ov["github"]["last_backfill_at"] is None
        assert "devops-backfill" not in {j[0] for j in default_jobs(e.app.state.settings)}
        assert "devops-purge" in {j[0] for j in default_jobs(e.app.state.settings)}
        ctx = TaskContext(e.app.state.settings, e.maker, None, github=None)
        with pytest.raises(RuntimeError, match="not configured"):
            await devops_backfill(ctx)
        # Server deploys and the runtime are still monitored without GitHub.
        await deploy(e, "backend", SHA, None, 0, {"health_ok": ago(0)})
        assert (await get(e, "/deployments"))["items"][0]["status"] == "SUCCEEDED"


async def test_backfill_stops_on_github_rate_limit(env: DevEnv) -> None:
    async with env() as e:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/user").mock(return_value=httpx.Response(200, json={"login": "me"}))
            pulls = router.get(url__regex=rf"{API}/repos/me/\w+/pulls").mock(
                return_value=httpx.Response(
                    403, json={"message": "API rate limit exceeded"},
                    headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
                )
            )  # fmt: skip
            ctx = TaskContext(e.app.state.settings, e.maker, None, github=e.app.state.github)
            out = await devops_backfill(ctx)
        assert out["rate_limited"] is True and out["pr_events"] == 0
        assert pulls.call_count == 1  # stopped at the first refusal, not every repo


# --- multi-repo + notifications ---------------------------------------------------------------


async def test_multi_repo_notifications_and_preferences(env: DevEnv) -> None:
    async with env() as e, public(e) as c:
        await e.sql("UPDATE core.users SET role_id = 1 WHERE email = 'a@spasta.online'")
        await hook(c, "pull_request", pr(1, "me/app"), "n-1")
        await hook(c, "pull_request", pr(2, "me/web"), "n-2")
        await hook(c, "workflow_run", run(600, "me/web", conclusion="failure"), "n-3")
        await record_runtime(e, runtime(worker_state="exited"))
        await drain(e)
        assert (await get(e, "/events", repo="me/web", type="pr.merged"))["total"] == 1
        assert (await get(e, "/builds", repo="me/app"))["total"] == 0
        n = await get(e, "/notifications")
        assert n["unread_count"] == 4 and [i["read"] for i in n["items"]] == [False] * 4
        # Mark one read; the other admin is unaffected.
        first = n["items"][-1]["id"]
        r = await e.send("admin", "POST", D + "/notifications/read", json={"event_ids": [first]})
        assert r.json() == {"unread_count": 3}
        assert (await get(e, "/notifications", who="a"))["unread_count"] == 4
        # Subscribe to me/web failures only: the runtime event (no repo) stays visible.
        r = await e.send("admin", "PUT", D + "/notifications/preferences",
                         json={"repos": ["me/web"], "failures_only": True})  # fmt: skip
        assert r.status_code == 200 and r.json()["repos"] == ["me/web"]
        types = [i["type"] for i in (await get(e, "/notifications"))["items"]]
        assert types == ["build.failed", "service.unhealthy"]  # newest first
        # The oldest item (service.unhealthy) was already marked read above.
        assert (await get(e, "/overview"))["unread_notifications"] == 1
        r = await e.send("admin", "POST", D + "/notifications/read", json={"up_to_id": 10**6})
        assert r.json() == {"unread_count": 0}
        assert (await get(e, "/notifications", unread_only=True))["items"] == []
        r = await e.send("admin", "PUT", D + "/notifications/preferences", json={"enabled": False})
        assert (await get(e, "/notifications"))["items"] == []
        # A repeated runtime report does not create a new notification.
        await record_runtime(e, runtime(worker_state="exited"))
        assert (await get(e, "/notifications", who="a"))["unread_count"] == 4


async def test_data_survives_an_api_restart(env: DevEnv) -> None:
    async with env() as e, public(e) as c:
        await hook(c, "pull_request", pr(11), "r-1")
        await drain(e)
        settings = e.app.state.settings
    app = create_app(settings)  # a new process on the same database
    async with (
        LifespanManager(app) as mgr,
        AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c,
    ):
        await c.post("/api/auth/login", json={"email": "admin@spasta.online", "password": PASSWORD})
        f = (await c.get(D + "/feed", params={"after_id": 0})).json()
        assert [i["type"] for i in f["items"]] == ["pr.merged"]


async def test_retention_purge(env: DevEnv) -> None:
    async with env(DEVOPS_RETENTION_DAYS=1, DEVOPS_SNAPSHOT_RETENTION_DAYS=1) as e, public(e) as c:
        await hook(c, "pull_request", pr(12), "t-1")
        await drain(e)
        await record_runtime(e, runtime())
        await e.sql("UPDATE devops.events SET occurred_at = now() - interval '3 days'")
        await e.sql("UPDATE devops.repo_events SET occurred_at = now() - interval '3 days'")
        await e.sql("UPDATE devops.service_snapshots SET observed_at = now() - interval '3 days'")
        out = await devops_purge(TaskContext(e.app.state.settings, e.maker, None))
        assert out == {"events": 1, "repo_events": 2, "snapshots": 3, "deployments": 0}
        assert (await get(e, "/services"))[0]["status"] == "HEALTHY"  # current state is kept
