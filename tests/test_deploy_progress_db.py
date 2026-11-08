"""Live deploy progress against PostgreSQL (docs/33-deploy-progress.md).

Reports go through the real worker parser (app.workers.deploy_progress.parse), exactly as
scripts/ops/deploy-poll.sh sends them; the deploy record goes through the real deploy_record
paths. Covers a normal deploy, a failure with rollback, a failure before anything changed,
an API restart in the middle (missed reports healed), stale reporting, two repositories
deploying at once, commit verification, PR numbers and log scrubbing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.agents.registry import AgentRegistry
from app.core.logging import register_secret_values
from app.services import devops
from app.workers import deploy_progress as worker
from app.workers import deploy_record
from tests.test_devops_db import DevEnv, env
from tests.test_finance_db import Env
from tests.test_github_db import gh_env

pytestmark = pytest.mark.db
FIXTURES = (gh_env, env)

SHA = "c" * 40
PREV = "d" * 40
M = "/api/v1/github/actions/monitor"
BACKEND = ("pulling", "validating", "building", "migrating", "restarting", "health_checking",
           "verifying")  # fmt: skip


def at(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=300 - seconds)).isoformat()


def ev(name: str, status: str, t: float, detail: str | None = None) -> dict[str, Any]:
    return {"name": name, "status": status, "at": at(t), "detail": detail}


async def start(e: Env, target: str = "backend", sha: str = SHA) -> int:
    registry = AgentRegistry(e.maker)
    await registry.register(deploy_record.DEPLOYER, {})
    a = deploy_record.build_parser().parse_args(
        ["start", "--target", target, "--sha", sha, "--from", PREV, "--log", "/x/deploy.log"]
    )
    run_id = await registry.start_run("deployer", f"deploy:{target}",
                                      details=deploy_record.start_details(a))  # fmt: skip
    async with e.maker() as db, db.begin():
        await devops.server_deploy_started(
            db, e.app.state.settings, agent_run_id=run_id, target=target, sha=sha,
            previous_sha=PREV, log_path=a.log,
        )  # fmt: skip
    return run_id


async def report(e: Env, run_id: int, stages: list[dict[str, Any]], *, target: str = "backend",
                 log: str | None = None, sha: str | None = SHA, pr: int | None = None
                 ) -> dict[str, Any]:  # fmt: skip
    doc: dict[str, Any] = {"schema": "aureon.deploy.progress/v1", "run_id": run_id,
                           "target": target, "stages": stages}  # fmt: skip
    if log is not None:
        doc["log"] = log
    if sha:
        doc["sha"] = sha
    if pr:
        doc["pr"] = pr
    parsed = worker.parse(json.dumps(doc).encode())
    async with e.maker() as db, db.begin():
        from app.services import deploy_progress

        return await deploy_progress.apply_report(db, e.app.state.settings, parsed)


async def finish(e: Env, run_id: int, code: int, phases: dict[str, str]) -> None:
    from app.agents.registry import AgentRegistry

    await AgentRegistry(e.maker).finish_run(run_id, "succeeded" if code == 0 else "failed",
                                            details={})  # fmt: skip
    async with e.maker() as db, db.begin():
        await devops.server_deploy_finished(
            db, e.app.state.settings, agent_run_id=run_id, exit_code=code,
            phases=devops.parse_phases(phases), error=None,
        )  # fmt: skip


async def progress(e: Env, run_id: int) -> dict[str, Any]:
    r = await e.get("admin", f"{M}/deploys/{run_id}/progress")
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def statuses(p: dict[str, Any]) -> dict[str, str]:
    return {s["name"]: s["status"] for s in p["stages"]}


def all_ok(t0: float = 0) -> list[dict[str, Any]]:
    out = []
    for i, n in enumerate(BACKEND):
        out += [ev(n, "running", t0 + i * 10), ev(n, "succeeded", t0 + i * 10 + 9)]
    return out


async def test_a_deploy_moves_stage_by_stage_and_completes_only_when_recorded(env: DevEnv) -> None:
    async with env() as e:
        run_id = await start(e)
        p = await progress(e, run_id)
        assert p["outcome"] == "running" and p["current_stage"] == "queued"
        assert statuses(p)["building"] == "pending"
        assert (p["stages_completed"], p["stages_total"]) == (0, 8)

        await report(e, run_id, [ev("pulling", "running", 1), ev("pulling", "succeeded", 5),
                                 ev("validating", "running", 6), ev("validating", "succeeded", 7),
                                 ev("building", "running", 8)])  # fmt: skip
        p = await progress(e, run_id)
        st = statuses(p)
        assert st["queued"] == "succeeded" and st["pulling"] == "succeeded"
        assert st["building"] == "running" and st["restarting"] == "pending"
        assert p["current_stage"] == "building" and p["stages_completed"] == 3
        building = next(s for s in p["stages"] if s["name"] == "building")
        assert building["duration_ms"] > 0  # elapsed so far
        # Monitor rows carry the stage for the timeline.
        item = next(i for i in (await e.get("admin", M)).json()["items"]
                    if i["id"] == f"deploy:{run_id}")  # fmt: skip
        assert (item["status"], item["stage"]) == ("running", "building")
        assert (item["stages_completed"], item["stages_total"]) == (3, 8)

        # Every stage done, health ok: still RUNNING until the deploy is recorded as finished.
        await report(e, run_id, all_ok())
        p = await progress(e, run_id)
        assert p["outcome"] == "running" and p["stages_completed"] == 8
        assert p["commit_verified"] is True and p["deployed_sha"] == SHA
        await finish(e, run_id, 0, {"health_ok": at(80)})
        p = await progress(e, run_id)
        assert p["outcome"] == "completed" and p["health_verified_at"] is not None
        item = next(i for i in (await e.get("admin", M)).json()["items"]
                    if i["id"] == f"deploy:{run_id}")  # fmt: skip
        assert item["status"] == "success"


async def test_failure_and_rollback_are_reported_as_they_happened(env: DevEnv) -> None:
    async with env() as e:
        run_id = await start(e)
        stages = [
            *all_ok()[:10],
            ev("health_checking", "running", 50),
            ev("health_checking", "failed", 55, "readiness: HTTP 503"),
            ev("rolling_back", "running", 56),
            ev("rolling_back", "succeeded", 70, "dddddddddddd is healthy again"),
        ]
        await report(e, run_id, stages)
        await finish(e, run_id, 1, {"failure": "readiness: HTTP 503"})
        p = await progress(e, run_id)
        st = statuses(p)
        assert p["outcome"] == "rolled_back" and p["rolled_back"] is True
        assert st["health_checking"] == "failed" and st["verifying"] == "skipped"
        assert st["rolling_back"] == "succeeded"
        failed = next(s for s in p["stages"] if s["name"] == "health_checking")
        assert failed["error"] == "readiness: HTTP 503"
        assert p["commit_verified"] is None  # never got to verification
        item = next(i for i in (await e.get("admin", M)).json()["items"]
                    if i["id"] == f"deploy:{run_id}")  # fmt: skip
        assert item["status"] == "failure" and item["rolled_back"] is True


async def test_a_stage_without_a_reported_end_is_failed_never_succeeded(env: DevEnv) -> None:
    async with env() as e:
        run_id = await start(e)
        await report(e, run_id, [ev("pulling", "running", 1), ev("pulling", "succeeded", 2),
                                 ev("validating", "running", 3)])  # fmt: skip
        await finish(e, run_id, 3, {"failure": "validation: docker compose config is invalid"})
        p = await progress(e, run_id)
        v = next(s for s in p["stages"] if s["name"] == "validating")
        assert v["status"] == "failed" and "without reporting" in v["error"]
        assert p["outcome"] == "failed" and p["rolled_back"] is False


async def test_missed_reports_during_an_api_restart_are_healed(env: DevEnv) -> None:
    async with env() as e:
        run_id = await start(e)
        await report(e, run_id, all_ok()[:8])  # up to migrating
        # The API restarts: the reports for restarting/health_checking never arrive. The next
        # report carries the whole file, so nothing is lost; old events never move it back.
        await report(e, run_id, all_ok())
        await report(e, run_id, all_ok()[:3])  # a late, older report
        p = await progress(e, run_id)
        assert all(s == "succeeded" for n, s in statuses(p).items() if n in BACKEND)
        # The stages survive a restart of the API itself: they are in the database.
        from asgi_lifespan import LifespanManager
        from httpx import ASGITransport, AsyncClient

        from app.main import create_app
        from tests.test_finance_db import PASSWORD

        app = create_app(e.app.state.settings)
        async with (
            LifespanManager(app) as mgr,
            AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c,
        ):
            await c.post("/api/auth/login",
                         json={"email": "admin@spasta.online", "password": PASSWORD})  # fmt: skip
            again = (await c.get(f"{M}/deploys/{run_id}/progress")).json()
        assert statuses(again) == statuses(p)


async def test_stale_progress_is_flagged(env: DevEnv) -> None:
    async with env(DEVOPS_DEPLOY_STALE_SECONDS=60) as e:
        run_id = await start(e)
        await report(e, run_id, [ev("building", "running", 1)])
        assert (await progress(e, run_id))["stale"] is False
        await e.sql("UPDATE devops.deployments SET progress_at = now() - interval '5 minutes'")
        p = await progress(e, run_id)
        assert p["stale"] is True and p["outcome"] == "running"  # never guessed as finished
        item = next(i for i in (await e.get("admin", M)).json()["items"]
                    if i["id"] == f"deploy:{run_id}")  # fmt: skip
        assert item["stale"] is True
        await finish(e, run_id, 1, {})
        assert (await progress(e, run_id))["stale"] is False  # finished: not stale


async def test_two_repositories_deploying_at_once(env: DevEnv) -> None:
    async with env() as e:
        b = await start(e, "backend")
        f = await start(e, "frontend", sha="e" * 40)
        await report(e, b, [ev("building", "running", 10)])
        await report(e, f, [ev("pulling", "running", 11), ev("pulling", "succeeded", 12),
                            ev("validating", "running", 13)], target="frontend",
                     sha="e" * 40)  # fmt: skip
        pb, pf = await progress(e, b), await progress(e, f)
        assert (pb["current_stage"], pf["current_stage"]) == ("building", "validating")
        assert pb["repo"] == "SVSKHD/Aureon_agents_Assembler"
        assert pf["repo"] == "SVSKHD/ASTRA-FRONTEND" and pf["stages_total"] == 7
        assert "migrating" not in statuses(pf)  # the frontend has no migrations
        # A report can never move another deploy: wrong target is refused.
        out = await report(e, b, [ev("building", "succeeded", 20)], target="frontend")
        assert out == {"applied": False, "reason": "target mismatch"}
        out = await report(e, 999999, [ev("building", "running", 20)])
        assert out["applied"] is False


async def test_commit_verification_and_pr_number(env: DevEnv) -> None:
    async with env() as e:
        # The poller expected SHA; the script deployed a newer commit (merged meanwhile).
        run_id = await start(e)
        await report(e, run_id, all_ok(), sha="f" * 40, pr=47)
        p = await progress(e, run_id)
        assert p["expected_sha"] == SHA and p["deployed_sha"] == "f" * 40
        assert p["commit_verified"] is False  # not the expected merge: flagged
        assert p["pr_number"] == 47  # from the merge commit subject
        # A recorded pr.merged event for the commit wins over the subject.
        await e.sql(
            "INSERT INTO devops.repo_events (dedup_key, kind, repo, number, sha, occurred_at, "
            "source) VALUES ('k1', 'pr.merged', 'SVSKHD/Aureon_agents_Assembler', 46, :sha, "
            "now(), 'webhook')", sha=SHA,
        )  # fmt: skip
        run2 = await start(e)
        await report(e, run2, all_ok(), sha=SHA, pr=99)
        p2 = await progress(e, run2)
        assert p2["commit_verified"] is True and p2["pr_number"] == 46


async def test_logs_are_scrubbed_capped_and_admin_only(env: DevEnv) -> None:
    async with env() as e:
        secret = "s3cr3t-" + "x" * 30
        register_secret_values([secret])
        run_id = await start(e)
        log = "\n".join(
            [f"2026-11-08T10:00:{i % 60:02d}Z line {i}" for i in range(800)]
            + [f"password in env: {secret}",
               "GET https://auth.dhan.co/app/consumeApp-consent?tokenId=7f3c2a10-9e8b",
               "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789"]
        )  # fmt: skip
        await report(e, run_id, [ev("building", "running", 1)], log=log)
        r = await e.get("admin", f"{M}/deploys/{run_id}/logs")
        assert r.status_code == 200
        body = r.json()
        assert body["truncated"] is True and body["running"] is True
        assert "line 799" in body["text"] and "line 0\n" not in body["text"]
        for leak in (secret, "7f3c2a10", "ghp_abcdefghij"):
            assert leak not in body["text"]
        assert (await e.get("a", f"{M}/deploys/{run_id}/logs")).status_code == 403
        assert (await e.get("a", f"{M}/deploys/{run_id}/progress")).status_code == 403
        assert (await e.get("admin", f"{M}/deploys/424242/progress")).status_code == 404
        p = await progress(e, run_id)
        assert p["log_available"] is True
