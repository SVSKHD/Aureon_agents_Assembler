"""Phase 18 against the DB (docs/21-self-repair.md): incident detection for every source,
recovery before escalation, the repair flow end to end (real git repo, fake Ollama, GitHub
mocked by respx), hard blocks, the recursion guard, the circuit breaker and the API."""

from __future__ import annotations

import base64
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from redis.asyncio import Redis
from sqlalchemy import func, select

from app.core.exception_sink import SINK
from app.core.logging import get_logger
from app.models.agents import Agent
from app.models.auth import AuditEvent
from app.models.github import GitHubTrackedOperation, GitHubWorkflowRun
from app.models.repairs import Incident, RepairTask
from app.services import github_webhooks as hooks
from app.services import incidents, repair_recovery, repairs
from app.services.github_webhooks import WorkflowFailure
from tests.conftest import TEST_REDIS_URL
from tests.test_coding_db import FakeOllama, git
from tests.test_finance_db import Env
from tests.test_github_db import API, EnvFactory, gh_env  # noqa: F401

pytestmark = pytest.mark.db

R = "/api/v1/repairs"
GH = "/repos/me/app"
WIDGET = "app/services/widget.py"

PATCH_WIDGET = f"""The divide-by-zero is guarded now.

```diff
diff --git a/{WIDGET} b/{WIDGET}
--- a/{WIDGET}
+++ b/{WIDGET}
@@ -1,2 +1,2 @@
 def ratio(a, b):
-    return a / b
+    return a / b if b else 0
```
"""

PATCH_TRADING = """```diff
diff --git a/app/services/paper.py b/app/services/paper.py
--- a/app/services/paper.py
+++ b/app/services/paper.py
@@ -1 +1 @@
-LIMIT = 1
+LIMIT = 1000000
```"""


@pytest.fixture
def code_repo(tmp_path: Path) -> Path:
    r = tmp_path / "aureon"
    (r / "app" / "services").mkdir(parents=True)
    (r / WIDGET).write_text("def ratio(a, b):\n    return a / b\n")
    (r / "app" / "services" / "paper.py").write_text("LIMIT = 1\n")
    (r / "app" / "services" / "trading.py").write_text("X = 1\n")
    git(r, "init", "-q", "-b", "main")
    git(r, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return r


RpEnv = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


@pytest.fixture
def rp_env(gh_env: EnvFactory, code_repo: Path, tmp_path: Path) -> RpEnv:  # noqa: F811
    @contextlib.asynccontextmanager
    async def factory(tests: list[list[str]] | None = None, **overrides: Any) -> AsyncIterator[Env]:
        cfg: dict[str, Any] = {
            "GITHUB_ALLOWED_OWNERS": ["me"],
            "CODE_REPOS": {
                "aureon": {
                    "path": str(code_repo),
                    "base": "main",
                    "tests": [["true"]] if tests is None else tests,
                }
            },
            "CODE_ALLOWED_COMMANDS": [["true"], ["false"]],
            "CODE_WORKTREE_ROOT": tmp_path / "worktrees",
            "REPAIR_MODEL": "qwen2.5-coder:7b",
            "REPAIR_REPO": "aureon",
            "REPAIR_GITHUB_REPO": "me/app",
            "INCIDENT_RECOVERY_COOLDOWN_SECONDS": 0.0,
            "INCIDENT_API_READY_URL": "",
            "INCIDENT_DOCKER_PROBE_SECONDS": 0.0,
            **overrides,
        }
        async with gh_env(**cfg) as e:
            await e.sql("DELETE FROM github.workflow_runs")
            await e.sql("DELETE FROM github.tracked_operations")
            SINK.clear()
            yield e
            SINK.clear()

    return factory


def st(e: Env) -> Any:
    return e.app.state.settings


def deps(e: Env, **kw: Any) -> repair_recovery.Deps:
    return repair_recovery.Deps(e.maker, st(e), **kw)


async def incident(e: Env, iid: Any) -> Incident:
    async with e.maker() as db:
        inc = await db.get(Incident, iid)
        assert inc is not None
        return inc


async def all_incidents(e: Env, source: str | None = None) -> list[Incident]:
    async with e.maker() as db:
        q = select(Incident).order_by(Incident.first_seen)
        if source:
            q = q.where(Incident.source == source)
        return list((await db.scalars(q)).all())


async def audits(e: Env, event: str) -> int:
    async with e.maker() as db:
        return int(
            await db.scalar(
                select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == event)
            )
            or 0
        )


async def code_incident(
    e: Env, file: str = WIDGET, *, self_origin: bool = False, fp: str | None = None
) -> uuid.UUID:
    async with e.maker() as db, db.begin():
        inc = await incidents.record(
            db,
            source="exception",
            fp=fp or incidents.fingerprint("exception", file, uuid.uuid4()),
            severity="error",
            title=f"ZeroDivisionError in {file}",
            details={
                "type": "ZeroDivisionError",
                "message": "division by zero",
                "location": {"file": file, "line": 2, "function": "ratio"},
                "frames": [f"{file}:2 ratio"],
                "self_origin": self_origin,
            },
        )
        return inc.id


async def task_of(e: Env, rid: Any) -> RepairTask:
    async with e.maker() as db:
        t = await db.get(RepairTask, rid)
        assert t is not None
        return t


async def run_next(e: Env, answer: str, ollama: FakeOllama | None = None) -> RepairTask:
    rid = await repairs.claim(e.maker, st(e))
    assert rid is not None
    fake = ollama or FakeOllama(answer)
    cfg = repairs.RepairConfig.from_settings(st(e))
    await repairs.run_repair(e.maker, st(e), cfg, fake, e.app.state.github, rid)
    return await task_of(e, rid)


def mock_publish(router: respx.MockRouter, base_sha: str) -> dict[str, respx.Route]:
    return {
        "base": router.get(f"{GH}/git/commits/{base_sha}").mock(
            return_value=httpx.Response(200, json={"sha": base_sha, "tree": {"sha": "e" * 40}})
        ),
        "blob": router.post(f"{GH}/git/blobs").mock(
            return_value=httpx.Response(201, json={"sha": "b" * 40})
        ),
        "tree": router.post(f"{GH}/git/trees").mock(
            return_value=httpx.Response(201, json={"sha": "c" * 40})
        ),
        "commit": router.post(f"{GH}/git/commits").mock(
            return_value=httpx.Response(201, json={"sha": "d" * 40})
        ),
        "ref": router.post(f"{GH}/git/refs").mock(
            return_value=httpx.Response(201, json={"ref": "refs/heads/x", "object": {"sha": "d"}})
        ),
        "pull": router.post(f"{GH}/pulls").mock(
            return_value=httpx.Response(
                201, json={"number": 7, "html_url": "https://github.com/me/app/pull/7"}
            )
        ),
    }


def leftover_worktrees(root: Path) -> list[Path]:
    return list(root.glob("repair-*"))


def body(route: respx.Route, i: int = -1) -> Any:
    return json.loads(route.calls[i].request.content)


# --- detection --------------------------------------------------------------------------------


async def test_exception_detection_rate_limited(rp_env: RpEnv) -> None:
    async with rp_env(INCIDENT_EXCEPTION_RATE_SECONDS=3600.0) as e:
        log = get_logger("tests.repairs")
        for _ in range(3):
            try:
                repairs.normalize("/abs")
            except Exception:
                log.exception("boom")
        assert await incidents.flush_exceptions(e.maker, st(e), "test") == 1
        [inc] = await all_incidents(e, "exception")
        assert inc.count == 3 and inc.status == "open"
        assert inc.details["location"]["file"] == "app/agents/coding/paths.py"
        assert inc.details["process"] == "test"
        try:
            repairs.normalize("/abs")
        except Exception:
            log.exception("boom again")
        assert await incidents.flush_exceptions(e.maker, st(e), "test") == 0  # rate-limited
        quick = st(e).model_copy(update={"INCIDENT_EXCEPTION_RATE_SECONDS": 0.0})
        assert await incidents.flush_exceptions(e.maker, quick, "test") == 1
        [inc] = await all_incidents(e, "exception")
        assert inc.count == 4  # one incident, aggregated


async def test_heartbeat_recovery_first_then_escalation(rp_env: RpEnv) -> None:
    async with rp_env() as e:
        old = datetime.now(UTC) - timedelta(minutes=10)
        async with e.maker() as db, db.begin():
            db.add(Agent(id="imaging", name="img", kind="imaging", last_heartbeat_at=old))
            db.add(Agent(id="scheduler", name="s", kind="scheduler", last_heartbeat_at=old))
            db.add(
                Agent(
                    id="mt5-bot",
                    name="m",
                    kind="trading",
                    last_heartbeat_at=old,
                    details={"stale_after_s": 3600},
                )
            )
            db.add(Agent(id="fresh", name="f", kind="x", last_heartbeat_at=datetime.now(UTC)))
            db.add(Agent(id="gone", name="g", kind="x", state="stopped", last_heartbeat_at=old))
        out = await incidents.scan_heartbeats(e.maker, st(e))
        assert sorted(out["stale_agents"]) == ["imaging", "scheduler"]  # mt5: own window
        redis = Redis.from_url(TEST_REDIS_URL)
        await redis.set("aureon:imaging:worker-lock", "dead-worker")
        try:
            d = deps(e, redis=redis)
            [img] = [i for i in await all_incidents(e, "heartbeat") if "imaging" in i.title]
            assert await repair_recovery.handle(d, img.id) == "recovering"
            assert await redis.get("aureon:imaging:worker-lock") is None  # lock cleared
            assert await repair_recovery.handle(d, img.id) == "recovering"
            inc = await incident(e, img.id)
            assert inc.recovery_attempts == 2 and len(inc.details["_recovery"]) == 2
            assert await repair_recovery.handle(d, img.id) == "manual"  # escalated after 2
            inc = await incident(e, img.id)
            assert inc.details["_escalated"] and inc.details["_manual"] is True
            assert await repair_recovery.handle(d, img.id) == "skipped"  # idempotent
        finally:
            await redis.aclose()
        async with e.maker() as db:
            assert (await db.get(Agent, "imaging")).paused is False  # type: ignore[union-attr]
        assert await audits(e, "incident_recovery_attempt") == 2
        assert await audits(e, "incident_escalated") == 1
        # the scan keeps bumping the same incident while stale (no duplicates) ...
        await incidents.scan_heartbeats(e.maker, st(e))
        assert len(await all_incidents(e, "heartbeat")) == 2
        # ... and resolves it once the heartbeat is fresh again
        await e.sql("UPDATE agents.agents SET last_heartbeat_at = now() WHERE id = 'imaging'")
        await incidents.scan_heartbeats(e.maker, st(e))
        assert (await incident(e, img.id)).status == "resolved"


async def test_docker_detection(rp_env: RpEnv) -> None:
    async with rp_env() as e:
        rows = [
            {"name": "aureon-api", "state": "running", "status": "Up 2 hours (unhealthy)"},
            {"name": "aureon-scheduler", "state": "running", "status": "Up 2 hours (healthy)"},
            {"name": "aureon-coding", "state": "exited", "status": "Exited (1)"},
        ]

        async def probe() -> list[dict[str, Any]]:
            return rows

        assert await incidents.scan_docker(e.maker, incidents.unknown_docker_probe) == {
            "docker": "unknown"
        }
        out = await incidents.scan_docker(e.maker, probe)
        assert sorted(out["unhealthy"]) == ["aureon-api", "aureon-coding"]
        docker = await all_incidents(e, "docker")
        assert len(docker) == 2
        # the shipped proxy is read-only: recovery records "manual" and escalates
        assert await repair_recovery.handle(deps(e), docker[0].id) == "manual"
        inc = await incident(e, docker[0].id)
        [attempt] = inc.details["_recovery"]
        assert attempt["results"][0]["outcome"] == "manual"
        # the real probe goes through the proxy's GET /containers/json only
        with respx.mock(assert_all_called=True) as router:
            router.get("http://proxy.test/containers/json").mock(
                return_value=httpx.Response(
                    200, json=[{"Names": ["/aureon-api"], "State": "running", "Status": "Up"}]
                )
            )
            s = st(e).model_copy(update={"DOCKER_API_URL": "http://proxy.test"})
            async with httpx.AsyncClient() as http:
                await incidents.scan_docker(e.maker, incidents.proxy_docker_probe(http, s))
        [api] = [i for i in docker if "aureon-api" in i.title]
        assert (await incident(e, api.id)).status == "resolved"


async def test_docker_restart_only_when_proxy_allows(rp_env: RpEnv) -> None:
    async with rp_env(
        INCIDENT_DOCKER_PROXY_ALLOWS_RESTART=True,
        INCIDENT_RESTART_CONTAINERS=["aureon-coding"],
        DOCKER_API_URL="http://proxy.test",
    ) as e:

        async def probe() -> list[dict[str, Any]]:
            return [
                {"name": "aureon-coding", "state": "exited", "status": "Exited"},
                {"name": "aureon-api", "state": "exited", "status": "Exited"},
            ]

        await incidents.scan_docker(e.maker, probe)
        with respx.mock(assert_all_called=True) as router:
            restart = router.post("http://proxy.test/containers/aureon-coding/restart").mock(
                return_value=httpx.Response(204)
            )
            async with httpx.AsyncClient() as http:
                for inc in await all_incidents(e, "docker"):
                    await repair_recovery.handle(deps(e, http=http), inc.id)
        assert restart.call_count == 1  # aureon-api is not allowlisted: manual
        states = {i.details["container"]: i.status for i in await all_incidents(e, "docker")}
        assert states == {"aureon-coding": "recovering", "aureon-api": "open"}


def failure(run_id: int = 99, branch: str = "main") -> WorkflowFailure:
    return WorkflowFailure(
        delivery_id=f"d-{run_id}",
        repo="me/app",
        run_id=run_id,
        workflow_id=5,
        name="CI",
        path=".github/workflows/ci.yml",
        branch=branch,
        head_sha="a" * 40,
        event="push",
        conclusion="failure",
        run_attempt=1,
        html_url=f"https://github.com/me/app/actions/runs/{run_id}",
    )


async def test_actions_detection_and_single_rerun(rp_env: RpEnv) -> None:
    async with rp_env(GITHUB_DISPATCH_ALLOWLIST=["me/app:ci.yml"]) as e:
        cb = incidents.subscribe_actions(e.maker)
        try:
            await hooks._fire(failure(99))
            await hooks._fire(failure(99))  # the same run twice: counted once
        finally:
            hooks.remove_workflow_failed(cb)
        [inc] = await all_incidents(e, "actions")
        assert inc.count == 1 and inc.details["run_id"] == 99
        assert inc.details["self_origin"] is False
        with respx.mock(base_url=API, assert_all_called=False) as router:
            router.get(f"{GH}/actions/runs/99").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": 99,
                        "status": "completed",
                        "name": "CI",
                        "path": ".github/workflows/ci.yml",
                    },
                )
            )
            rerun = router.post(f"{GH}/actions/runs/99/rerun-failed-jobs").mock(
                return_value=httpx.Response(201, json={})
            )
            d = deps(e, github=e.app.state.github)
            assert await repair_recovery.handle(d, inc.id) == "recovering"
            assert await repair_recovery.handle(d, inc.id) == "attempted"  # once per run
            assert await repair_recovery.handle(d, inc.id) == "manual"  # escalated
        assert rerun.call_count == 1
        async with e.maker() as db:
            ops = (await db.scalars(select(GitHubTrackedOperation))).all()
        assert [o.op for o in ops] == ["actions.run_rerun_failed"] and ops[0].actor is None


async def test_actions_table_scan_and_repair_branch_guard(rp_env: RpEnv) -> None:
    async with rp_env() as e:
        now = datetime.now(UTC)
        async with e.maker() as db, db.begin():
            db.add(
                GitHubWorkflowRun(
                    id=501, repo="me/app", name="CI", path=".github/workflows/ci.yml",
                    branch="repair/0123456789ab", status="completed", conclusion="failure",
                    created_at=now, updated_at=now,
                )
            )  # fmt: skip
            db.add(
                GitHubWorkflowRun(
                    id=502, repo="me/app", name="CI", path=".github/workflows/ci.yml",
                    branch="main", status="completed", conclusion="success",
                    created_at=now, updated_at=now,
                )
            )  # fmt: skip
        assert (await incidents.scan_actions(e.maker))["failed_runs"] == 1
        [inc] = await all_incidents(e, "actions")
        assert inc.details["self_origin"] is True  # a repair PR's own CI
        await repair_recovery.handle(deps(e), inc.id)
        await repair_recovery.handle(deps(e), inc.id)
        async with e.maker() as db:
            assert await db.scalar(select(func.count()).select_from(RepairTask)) == 0


async def test_db_and_api_probes(rp_env: RpEnv) -> None:
    async with rp_env() as e:
        slow = st(e).model_copy(update={"INCIDENT_DB_SLOW_MS": 0.0001})
        await incidents.scan_db(e.maker, slow)
        [db_inc] = await all_incidents(e, "db")
        assert db_inc.severity == "warning" and "slow" in db_inc.title
        await incidents.scan_db(e.maker, st(e))
        assert (await incident(e, db_inc.id)).status == "resolved"

        s = st(e).model_copy(update={"INCIDENT_API_READY_URL": "http://api.test/api/health/ready"})
        with respx.mock(assert_all_called=True) as router:
            route = router.get("http://api.test/api/health/ready")
            route.mock(
                return_value=httpx.Response(
                    503, json={"status": "not_ready", "checks": {"redis": {"ok": False}}}
                )
            )
            async with httpx.AsyncClient() as http:
                assert (await incidents.scan_api(e.maker, s, http))["api"] == "not_ready"
                [api_inc] = await all_incidents(e, "api")
                assert api_inc.details["checks"]["redis"]["ok"] is False
                route.mock(return_value=httpx.Response(200, json={"status": "ready"}))
                await incidents.scan_api(e.maker, s, http)
        assert (await incident(e, api_inc.id)).status == "resolved"


# --- repairs ----------------------------------------------------------------------------------


async def test_happy_path_to_pr_opened(rp_env: RpEnv, code_repo: Path) -> None:
    async with rp_env() as e:
        iid = await code_incident(e)
        out = await repair_recovery.process_incidents(deps(e))
        assert out["outcomes"] == {"repair_queued": 1}
        inc = await incident(e, iid)
        assert inc.status == "repair_queued"
        base = git(code_repo, "rev-parse", "main")
        fake = FakeOllama(PATCH_WIDGET)
        with respx.mock(base_url=API, assert_all_called=True) as router:
            routes = mock_publish(router, base)
            task = await run_next(e, PATCH_WIDGET, fake)
            merges = [c for c in router.calls if "/merge" in str(c.request.url)]
        assert task.status == "pr_opened", (task.error, task.test_output)
        assert task.pr_url == "https://github.com/me/app/pull/7" and task.pr_number == 7
        assert task.branch == f"repair/{task.id.hex[:12]}"
        assert task.files == [WIDGET] and "return a / b if b else 0" in (task.patch or "")
        assert task.prompt_digest and len(task.prompt_digest) == 64
        assert "$ true  -> exit 0" in (task.test_output or "")
        assert task.worktree is None and task.base_sha == base
        # the model saw the incident and the target file
        prompt = fake.prompts[0][1]["content"]
        assert "ZeroDivisionError" in prompt and "def ratio" in prompt
        # what went to GitHub: the patched file, one commit on the repair branch, one PR
        blob = base64.b64decode(body(routes["blob"])["content"]).decode()
        assert "return a / b if b else 0" in blob
        assert body(routes["tree"])["tree"][0]["path"] == WIDGET
        assert body(routes["commit"])["parents"] == [base]
        assert body(routes["ref"]) == {"ref": f"refs/heads/{task.branch}", "sha": "d" * 40}
        pr = body(routes["pull"])
        assert pr["head"] == task.branch and pr["base"] == "main"
        assert merges == []  # never merged here
        # the main checkout is untouched and the local repair branch is gone
        assert (code_repo / WIDGET).read_text() == "def ratio(a, b):\n    return a / b\n"
        assert "repair/" not in git(code_repo, "branch", "--list")
        assert not leftover_worktrees(st(e).CODE_WORKTREE_ROOT)
        assert (await incident(e, iid)).status == "repair_queued"  # admin resolves after merge
        assert await audits(e, "repair_pr_opened") == 1
        # API: detail carries patch + test output
        r = await e.get("admin", f"{R}/{task.id}")
        assert r.status_code == 200 and r.json()["patch"] and r.json()["test_output"]
        r = await e.get("admin", f"{R}/incidents/{iid}")
        assert r.json()["repairs"][0]["status"] == "pr_opened"


async def test_hard_block_trading_before_model(rp_env: RpEnv) -> None:
    async with rp_env() as e:
        iid = await code_incident(e, "app/services/trading.py")
        await repair_recovery.process_incidents(deps(e))
        async with e.maker() as db:
            [task] = (await db.scalars(select(RepairTask))).all()
        assert task.status == "blocked" and "trading" in (task.blocked_reason or "")
        assert (await incident(e, iid)).details["_manual"] is True
        assert await repairs.claim(e.maker, st(e)) is None  # nothing to run
        # the worker re-checks before calling the model too
        async with e.maker() as db, db.begin():
            t = RepairTask(
                incident_id=iid, repo="aureon", target_paths=["app/agents/trading/x.py"],
                status="queued",
            )  # fmt: skip
            db.add(t)
        fake = FakeOllama(PATCH_WIDGET)
        task = await run_next(e, "", fake)
        assert task.status == "blocked" and fake.prompts == []


async def test_hard_block_after_model(rp_env: RpEnv, code_repo: Path) -> None:
    async with rp_env() as e:
        iid = await code_incident(e)
        r = await e.send("admin", "POST", R, json={"incident_id": str(iid)})
        assert r.status_code == 201 and r.json()["status"] == "queued"
        with respx.mock(base_url=API, assert_all_called=False) as router:
            task = await run_next(e, PATCH_TRADING)
            assert router.calls.call_count == 0  # nothing pushed
        assert task.status == "blocked"
        assert "paper" in (task.blocked_reason or "") and task.files == ["app/services/paper.py"]
        assert task.worktree is None and "repair/" not in git(code_repo, "branch", "--list")
        assert (code_repo / "app/services/paper.py").read_text() == "LIMIT = 1\n"
        assert (await incident(e, iid)).status == "open"


async def test_tests_failed_means_no_push(rp_env: RpEnv) -> None:
    async with rp_env(tests=[["true"], ["false"]]) as e:
        await code_incident(e)
        await repair_recovery.process_incidents(deps(e))
        with respx.mock(base_url=API, assert_all_called=False) as router:
            task = await run_next(e, PATCH_WIDGET)
            assert router.calls.call_count == 0
        assert task.status == "tests_failed" and task.pr_url is None
        assert "$ false  -> exit 1" in (task.test_output or "")
        assert task.branch is None and task.worktree is None


async def test_disabled_without_model(rp_env: RpEnv) -> None:
    async with rp_env(REPAIR_MODEL=None) as e:
        iid = await code_incident(e)
        r = await e.send("admin", "POST", R, json={"incident_id": str(iid)})
        assert r.status_code == 201
        body_ = r.json()
        assert body_["status"] == "queued" and "REPAIR_MODEL" in body_["blocked_reason"]
        assert await repairs.claim(e.maker, st(e)) is None  # stays queued
        r = await e.get("admin", f"{R}/status")
        assert r.json()["enabled"] is False and "REPAIR_MODEL" in r.json()["reason"]


async def test_recursion_guard(rp_env: RpEnv) -> None:
    async with rp_env(REPAIR_MAX_PER_FINGERPRINT_PER_DAY=5) as e:
        # an incident from the repair system itself never gets a repair
        own = await code_incident(e, "app/services/finance.py", self_origin=True)
        await repair_recovery.process_incidents(deps(e))
        assert "recursion_guard" in (await incident(e, own)).details["_repair_refused"]
        r = await e.send("admin", "POST", R, json={"incident_id": str(own)})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "recursion_guard"
        # retry depth <= 1
        iid = await code_incident(e)
        r = await e.send("admin", "POST", R, json={"incident_id": str(iid)})
        first = r.json()["id"]
        r = await e.send("admin", "POST", R, json={"incident_id": str(iid)})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "repair_active"
        assert (await e.send("admin", "POST", f"{R}/{first}/cancel")).json()["status"] == (
            "cancelled"
        )
        r = await e.send("admin", "POST", f"{R}/{first}/retry")
        assert r.status_code == 201 and r.json()["parent_repair_id"] == first
        second = r.json()["id"]
        await e.send("admin", "POST", f"{R}/{second}/cancel")
        r = await e.send("admin", "POST", f"{R}/{second}/retry")
        assert r.status_code == 409 and "depth" in r.json()["detail"]["detail"]
        r = await e.send("admin", "POST", f"{R}/{second}/cancel")
        assert r.status_code == 409  # already cancelled


async def test_per_fingerprint_daily_limit(rp_env: RpEnv) -> None:
    async with rp_env(REPAIR_MAX_PER_FINGERPRINT_PER_DAY=1) as e:
        iid = await code_incident(e, fp="f" * 40)
        r = await e.send("admin", "POST", R, json={"incident_id": str(iid)})
        await e.send("admin", "POST", f"{R}/{r.json()['id']}/cancel")
        r = await e.send("admin", "POST", R, json={"incident_id": str(iid)})
        assert r.status_code == 429 and r.json()["detail"]["code"] == "repair_daily_limit"


async def test_circuit_breaker(rp_env: RpEnv) -> None:
    async with rp_env(REPAIR_BREAKER_MAX_FAILURES_PER_HOUR=3) as e:
        iid = await code_incident(e)
        async with e.maker() as db, db.begin():
            for i in range(4):
                db.add(
                    RepairTask(
                        incident_id=iid, repo="aureon",
                        status="failed" if i % 2 else "tests_failed",
                        finished_at=datetime.now(UTC),
                    )
                )  # fmt: skip
            db.add(RepairTask(incident_id=iid, repo="aureon", target_paths=[WIDGET]))
        assert await repairs.claim(e.maker, st(e)) is None
        assert await repairs.is_paused(e.maker)
        assert await audits(e, "repair_circuit_open") == 1
        assert await repairs.check_breaker(e.maker, st(e))  # still open, audited once
        assert await audits(e, "repair_circuit_open") == 1
        r = await e.get("admin", f"{R}/status")
        assert r.json()["paused"] is True


async def test_incident_api_and_admin_only(rp_env: RpEnv) -> None:
    async with rp_env() as e:
        iid = await code_incident(e)
        for path in ("/incidents", f"/incidents/{iid}", "", "/status"):
            assert (await e.get("a", R + path)).status_code == 403, path
        for path in (f"/incidents/{iid}/resolve", f"/incidents/{iid}/ignore", ""):
            r = await e.send("a", "POST", R + path, json={"incident_id": str(iid)})
            assert r.status_code == 403, path
        anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app), base_url="https://test")
        async with anon:
            assert (await anon.get(f"{R}/incidents")).status_code == 401
        r = await e.get("admin", f"{R}/incidents?source=exception")
        assert r.status_code == 200 and r.json()["total"] == 1
        assert r.json()["items"][0]["id"] == str(iid)
        r = await e.send("admin", "POST", f"{R}/incidents/{iid}/ignore")
        assert r.status_code == 200 and r.json()["status"] == "ignored"
        r = await e.send("admin", "POST", f"{R}/incidents/{iid}/resolve")
        assert r.status_code == 409
        r = await e.send("admin", "POST", R, json={"incident_id": str(iid)})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "incident_closed"
        assert (await e.get("admin", f"{R}/incidents/{uuid.uuid4()}")).status_code == 404
        assert (await e.get("admin", f"{R}/{uuid.uuid4()}")).status_code == 404
        assert await audits(e, "incident_ignored") == 1


class CancellingOllama(FakeOllama):
    """Cancels the task through the API while the model is 'thinking'."""

    def __init__(self, e: Env, answer: str) -> None:
        super().__init__(answer)
        self.e = e

    async def chat_stream(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        [(rid,)] = await self.e.sql("SELECT id FROM agents.repair_tasks WHERE status = 'running'")
        r = await self.e.send("admin", "POST", f"{R}/{rid}/cancel")
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        async for chunk in super().chat_stream(model, messages, options):
            yield chunk


async def test_cancel_mid_flight_stops_before_push(rp_env: RpEnv, code_repo: Path) -> None:
    async with rp_env() as e:
        iid = await code_incident(e)
        await e.send("admin", "POST", R, json={"incident_id": str(iid)})
        with respx.mock(base_url=API, assert_all_called=False) as router:
            task = await run_next(e, "", CancellingOllama(e, PATCH_WIDGET))
            assert router.calls.call_count == 0
        assert task.status == "cancelled" and task.worktree is None
        assert "repair/" not in git(code_repo, "branch", "--list")
        assert (await incident(e, iid)).status == "open"
