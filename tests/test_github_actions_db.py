"""Phase 17 against the DB (docs/20-github-actions-webhooks.md): Actions reads and logs,
allowlisted / gated dispatch and re-run, cancel, the webhook receiver and the worker, with
GitHub mocked by respx."""

from __future__ import annotations

import contextlib
import io
import json
import zipfile
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text

from app.models.agents import Agent
from app.services import github_webhooks as hooks
from app.workers.github_webhooks import WebhookWorker
from app.workers.scheduler import TaskContext, github_actions_sync
from tests.conftest import TEST_REDIS_URL
from tests.test_finance_db import PASSWORD, Env
from tests.test_github_db import API, TOKEN, EnvFactory, gh_env, repo_json, rl  # noqa: F401

pytestmark = pytest.mark.db

R = "/api/v1/github/repositories/me/app"
A = R + "/actions"
GH = f"{API}/repos/me/app"
BLOB = "https://pipelines.actions.githubusercontent.com/logs/77?sig=PRESIGNED"
SECRET = "whsec-" + "f" * 58
SHA1 = "1" * 40
SHA2 = "2" * 40
HOOK = "/api/v1/github/webhooks"

ActEnv = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


@pytest.fixture
def env(gh_env: EnvFactory) -> ActEnv:  # noqa: F811
    @contextlib.asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[Env]:
        overrides.setdefault("GITHUB_ALLOWED_OWNERS", ["me"])
        overrides.setdefault("GITHUB_DISPATCH_ALLOWLIST", ["me/app:ci.yml", "me/app:deploy.yml"])
        overrides.setdefault("GITHUB_WEBHOOK_SECRET", SecretStr(SECRET))
        overrides.setdefault("GITHUB_WEBHOOK_RETRY_BASE_SECONDS", 0.0)
        async with gh_env(**overrides) as e:
            await e.sql(
                "TRUNCATE github.webhook_events, github.workflow_runs, github.workflow_jobs"
            )
            yield e

    return factory


def writes(router: respx.MockRouter) -> list[httpx.Request]:
    return [c.request for c in router.calls if c.request.method != "GET"]


def workflow(wid: int, file: str, name: str, state: str = "active") -> dict[str, Any]:
    return {
        "id": wid,
        "name": name,
        "path": f".github/workflows/{file}",
        "state": state,
        "html_url": f"https://github.com/me/app/blob/main/.github/workflows/{file}",
    }


def run(rid: int, **kw: Any) -> dict[str, Any]:
    return {
        "id": rid,
        "name": "CI",
        "workflow_id": 10,
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "head_branch": "main",
        "head_sha": SHA1,
        "status": "completed",
        "conclusion": "success",
        "run_number": 5,
        "run_attempt": 1,
        "actor": {"login": "me"},
        "html_url": f"https://github.com/me/app/actions/runs/{rid}",
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-01T10:05:00Z",
        **kw,
    }


def job(jid: int, run_id: int, **kw: Any) -> dict[str, Any]:
    return {
        "id": jid,
        "run_id": run_id,
        "name": "test",
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 1,
        "runner_name": "r1",
        "labels": ["ubuntu-latest"],
        "started_at": "2026-11-01T10:00:00Z",
        "completed_at": "2026-11-01T10:04:00Z",
        "steps": [{"number": 1, "name": "Run pytest", "status": "completed",
                   "conclusion": "failure"}],
        **kw,
    }  # fmt: skip


async def ops_rows(env: Env) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = await env.sql(
        "SELECT op, status, target, approval_request_id FROM github.tracked_operations ORDER BY id"
    )
    return rows


def approve(e: Env, aid: str) -> Any:
    return e.send("admin", "POST", f"/api/v1/approvals/{aid}/approve", json={"password": PASSWORD})


# --- reads --------------------------------------------------------------------------------


async def test_reads(env: ActEnv) -> None:
    async with env() as e:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{GH}/actions/workflows").mock(
                return_value=httpx.Response(200, json={"total_count": 3, "workflows": [
                    workflow(10, "ci.yml", "CI"), workflow(11, "deploy.yml", "Deploy"),
                    workflow(12, "lint.yml", "Lint")]}, headers=rl())
            )  # fmt: skip
            r = await e.get("admin", f"{A}/workflows")
            assert r.status_code == 200, r.text
            w = {x["file"]: x for x in r.json()}
            assert w["ci.yml"]["dispatch_allowed"] and not w["ci.yml"]["production"]
            assert w["deploy.yml"]["dispatch_allowed"] and w["deploy.yml"]["production"]
            assert not w["lint.yml"]["dispatch_allowed"]

            runs = router.get(f"{GH}/actions/workflows/ci.yml/runs").mock(
                return_value=httpx.Response(
                    200,
                    json={"total_count": 1, "workflow_runs": [run(100)]},
                    headers={**rl(), "link": f'<{GH}/actions/runs?page=2>; rel="next"'},
                )
            )
            q = {"workflow": "ci.yml", "branch": "main", "status": "failure", "per_page": 5}
            r = await e.get("admin", f"{A}/runs", params=q)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["has_next"] and body["items"][0]["id"] == 100
            assert body["items"][0]["branch"] == "main" and body["items"][0]["actor"] == "me"
            q = runs.calls[0].request.url.params
            assert q["branch"] == "main" and q["status"] == "failure" and q["per_page"] == "5"
            assert (
                await e.get("admin", f"{A}/runs", params={"status": "bogus"})
            ).status_code == 422
            r = await e.get("admin", f"{A}/runs", params={"workflow": "../x.yml"})
            assert r.status_code == 400

            router.get(f"{GH}/actions/runs/100").mock(
                return_value=httpx.Response(200, json=run(100))
            )
            r = await e.get("admin", f"{A}/runs/100")
            assert r.json()["conclusion"] == "success"
            router.get(f"{GH}/actions/runs/100/jobs").mock(
                return_value=httpx.Response(200, json={"total_count": 1, "jobs": [job(7, 100)]})
            )
            r = await e.get("admin", f"{A}/runs/100/jobs")
            assert r.status_code == 200
            j = r.json()[0]
            assert j["id"] == 7 and j["steps"][0]["name"] == "Run pytest"
            # non-admins: 403; repos off the allowlist: 404 without a GitHub call
            assert (await e.get("a", f"{A}/workflows")).status_code == 403
            n = len(router.calls)
            r = await e.get("admin", "/api/v1/github/repositories/other/app/actions/workflows")
            assert r.status_code == 404 and len(router.calls) == n


# --- logs ---------------------------------------------------------------------------------


async def test_job_logs_follow_the_redirect_without_the_token(env: ActEnv) -> None:
    log_text = (
        "\x1b[32m2026-11-02T10:00:00Z Run tests\x1b[0m\r\n"
        "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
        f"echo {TOKEN}\n"
        "curl -H 'Authorization: Bearer sk-abcdefghijklmnop'\n"
        "API_TOKEN=Zx9Qw8Er7Ty6Ui5Op4As3Df2\n"
        "FAILED tests/test_x.py::test_y\n"
    )
    async with env() as e:
        with respx.mock(assert_all_called=False) as router:
            api = router.get(f"{GH}/actions/jobs/77/logs").mock(
                return_value=httpx.Response(302, headers={"location": BLOB})
            )
            blob = router.get(BLOB).mock(
                return_value=httpx.Response(200, content=log_text.encode())
            )
            r = await e.get("admin", f"{A}/jobs/77/logs")
            assert r.status_code == 200, r.text
            assert r.headers["content-type"].startswith("text/plain")
            assert r.headers["x-log-truncated"] == "false"
            assert api.calls[0].request.headers["authorization"] == f"Bearer {TOKEN}"
            assert "authorization" not in blob.calls[0].request.headers
            body = r.text
            assert "\x1b" not in body and "\r" not in body
            assert "Run tests" in body and "FAILED tests/test_x.py::test_y" in body
            for secret in ("AKIAIOSFODNN7EXAMPLE", TOKEN, "sk-abcdefghijklmnop", "Zx9Qw8Er7"):
                assert secret not in body
            assert body.count("[REDACTED]") >= 4


async def test_log_output_is_capped_tail_first(env: ActEnv) -> None:
    lines = "".join(f"line {i:06d}\n" for i in range(20_000))  # ~240 KB
    async with env(GITHUB_LOG_MAX_BYTES=4096) as e:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{GH}/actions/jobs/77/logs").mock(
                return_value=httpx.Response(302, headers={"location": BLOB})
            )
            router.get(BLOB).mock(return_value=httpx.Response(200, content=lines.encode()))
            r = await e.get("admin", f"{A}/jobs/77/logs")
            assert r.status_code == 200
            assert r.headers["x-log-truncated"] == "true"
            assert len(r.content) <= 4096
            assert r.text.rstrip().endswith("line 019999") and "line 000001" not in r.text


async def test_log_redirect_to_another_host_is_refused(env: ActEnv) -> None:
    async with env() as e:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{GH}/actions/jobs/77/logs").mock(
                return_value=httpx.Response(302, headers={"location": "https://evil.test/x?t=1"})
            )
            evil = router.get("https://evil.test/x").mock(return_value=httpx.Response(200))
            r = await e.get("admin", f"{A}/jobs/77/logs")
            assert r.status_code == 502 and r.json()["detail"]["code"] == "github_log_host_refused"
            assert evil.call_count == 0
            # the API host is not a log host either: no token-bearing fetch elsewhere
            router.get(f"{GH}/actions/jobs/78/logs").mock(
                return_value=httpx.Response(302, headers={"location": "http://pipelines.actions.githubusercontent.com/x"})
            )  # fmt: skip
            r = await e.get("admin", f"{A}/jobs/78/logs")
            assert r.status_code == 502


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def test_run_logs_unzip_with_caps(env: ActEnv) -> None:
    archive = _zip({
        "1_build.txt": b"\x1b[1mbuild ok\x1b[0m\n",
        "2_test.txt": f"boom\nGITHUB_TOKEN={TOKEN}\n".encode(),
        "test/1_Run pytest.txt": b"step copy\n",
    })  # fmt: skip
    async with env(GITHUB_LOG_ZIP_MAX_ENTRIES=5) as e:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{GH}/actions/runs/100/logs").mock(
                return_value=httpx.Response(302, headers={"location": BLOB})
            )
            router.get(BLOB).mock(return_value=httpx.Response(200, content=archive))
            r = await e.get("admin", f"{A}/runs/100/logs")
            assert r.status_code == 200, r.text
            t = r.text
            assert "===== 1_build.txt =====" in t and "build ok" in t and "boom" in t
            assert "step copy" not in t and TOKEN not in t and "\x1b" not in t

            many = _zip({f"{i}_j.txt": b"x" for i in range(10)})
            router.get(f"{GH}/actions/runs/101/logs").mock(
                return_value=httpx.Response(302, headers={"location": BLOB + "&n=1"})
            )
            router.get(BLOB + "&n=1").mock(return_value=httpx.Response(200, content=many))
            r = await e.get("admin", f"{A}/runs/101/logs")
            assert r.status_code == 413
            assert r.json()["detail"]["code"] == "log_archive_too_many_entries"


async def test_run_log_archive_over_the_download_cap_is_413(env: ActEnv) -> None:
    async with env(GITHUB_LOG_DOWNLOAD_MAX_BYTES=1024) as e:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{GH}/actions/runs/100/logs").mock(
                return_value=httpx.Response(302, headers={"location": BLOB})
            )
            router.get(BLOB).mock(return_value=httpx.Response(200, content=b"P" * 5000))
            r = await e.get("admin", f"{A}/runs/100/logs")
            assert r.status_code == 413 and r.json()["detail"]["code"] == "log_archive_too_large"


# --- dispatch / rerun / cancel ------------------------------------------------------------


async def test_dispatch_blocked_when_not_allowlisted(env: ActEnv) -> None:
    async with env() as e:
        with respx.mock(assert_all_called=False) as router:
            r = await e.send("admin", "POST", f"{A}/workflows/lint.yml/dispatch",
                             json={"ref": "main"})  # fmt: skip
            assert r.status_code == 403, r.text
            assert r.json()["detail"]["code"] == "workflow_not_allowlisted"
            assert router.calls.call_count == 0  # refused before any GitHub call
            # by id: looked up, then refused, nothing written
            router.get(f"{GH}/actions/workflows/12").mock(
                return_value=httpx.Response(200, json=workflow(12, "lint.yml", "Lint"))
            )
            r = await e.send("admin", "POST", f"{A}/workflows/12/dispatch", json={"ref": "main"})
            assert r.status_code == 403 and writes(router) == []
            r = await e.send("admin", "POST", f"{A}/workflows/ci.yml/dispatch",
                             json={"ref": "../main"})  # fmt: skip
            assert r.status_code == 400
            r = await e.send("admin", "POST", f"{A}/workflows/ci.yml/dispatch",
                             json={"ref": "main", "extra": 1})  # fmt: skip
            assert r.status_code == 422
        assert await ops_rows(e) == []


async def test_dispatch_allowlisted_runs_directly_and_is_tracked(env: ActEnv) -> None:
    async with env() as e:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{GH}/actions/workflows/ci.yml").mock(
                return_value=httpx.Response(200, json=workflow(10, "ci.yml", "CI"))
            )
            send = router.post(f"{GH}/actions/workflows/10/dispatches").mock(
                return_value=httpx.Response(204)
            )
            body = {"ref": "main", "inputs": {"debug": True, "level": "2"}}
            r = await e.send("admin", "POST", f"{A}/workflows/ci.yml/dispatch", json=body)
            assert r.status_code == 202, r.text
            assert r.json()["status"] == "sent" and r.json()["approval"] is None
            assert json.loads(send.calls[0].request.content) == {
                "ref": "main", "inputs": {"debug": True, "level": "2"}}  # fmt: skip
            # an inactive workflow is refused
            router.get(f"{GH}/actions/workflows/ci.yml").mock(
                return_value=httpx.Response(200, json=workflow(10, "ci.yml", "CI",
                                                               "disabled_manually"))
            )  # fmt: skip
            r = await e.send("admin", "POST", f"{A}/workflows/ci.yml/dispatch", json={"ref": "x"})
            assert r.status_code == 409 and send.call_count == 1
        rows = await ops_rows(e)
        assert [(o, s, t) for o, s, t, _a in rows] == [
            ("actions.workflow_dispatch", "succeeded", "workflow:ci.yml@main")
        ]
        n = await e.sql(
            "SELECT count(*) FROM audit.audit_events WHERE event_type = "
            "'github_actions_workflow_dispatch'"
        )
        assert n[0][0] == 1


async def test_prod_dispatch_is_gated_and_runs_only_after_approval(env: ActEnv) -> None:
    async with env(GITHUB_PROD_WORKFLOWS=["Deploy"]) as e:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{GH}/actions/workflows/deploy.yml").mock(
                return_value=httpx.Response(200, json=workflow(11, "deploy.yml", "Deploy"))
            )
            router.get(f"{GH}/actions/workflows/11").mock(
                return_value=httpx.Response(200, json=workflow(11, "deploy.yml", "Deploy"))
            )
            sha = router.get(f"{GH}/commits/main").mock(
                return_value=httpx.Response(200, json={"sha": SHA1})
            )
            send = router.post(f"{GH}/actions/workflows/11/dispatches").mock(
                return_value=httpx.Response(204)
            )
            r = await e.send("admin", "POST", f"{A}/workflows/deploy.yml/dispatch",
                             json={"ref": "main", "inputs": {"env": "prod"}})  # fmt: skip
            assert r.status_code == 202, r.text
            body = r.json()
            assert body["status"] == "pending_approval"
            g = body["approval"]
            assert g["kind"] == "github.workflow_dispatch_prod" and g["status"] == "pending"
            assert g["artifact"]["ref_sha"] == SHA1 and g["artifact"]["inputs"] == {"env": "prod"}
            assert writes(router) == []  # zero GitHub write calls before approval
            rows = await ops_rows(e)
            assert [(o, s) for o, s, _t, _a in rows] == [
                ("actions.workflow_dispatch", "pending_approval")
            ]

            r = await approve(e, g["approval_id"])
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "executed"
            assert send.call_count == 1
            assert json.loads(send.calls[0].request.content) == {
                "ref": "main", "inputs": {"env": "prod"}}  # fmt: skip
            r = await approve(e, g["approval_id"])  # single use
            assert r.status_code == 409 and send.call_count == 1
            assert sha.call_count == 2  # once at request, once at approval

            # ref moved between request and approval -> invalidated, nothing sent
            r = await e.send("admin", "POST", f"{A}/workflows/deploy.yml/dispatch",
                             json={"ref": "main"})  # fmt: skip
            aid = r.json()["approval"]["approval_id"]
            sha.mock(return_value=httpx.Response(200, json={"sha": SHA2}))
            r = await approve(e, aid)
            assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_invalidated"
            assert send.call_count == 1
        rows = await ops_rows(e)
        assert [(o, s) for o, s, _t, _a in rows] == [
            ("actions.workflow_dispatch", "succeeded"),
            ("actions.workflow_dispatch", "invalidated"),
        ]


async def test_keyword_prod_workflows_are_gated_by_default(env: ActEnv) -> None:
    async with env() as e:  # GITHUB_PROD_WORKFLOWS empty: "deploy" in the file still gates
        with respx.mock(assert_all_called=False) as router:
            for w in ("deploy.yml", "11"):
                router.get(f"{GH}/actions/workflows/{w}").mock(
                    return_value=httpx.Response(200, json=workflow(11, "deploy.yml", "Ship"))
                )
            router.get(f"{GH}/commits/main").mock(
                return_value=httpx.Response(200, json={"sha": SHA1})
            )
            r = await e.send("admin", "POST", f"{A}/workflows/deploy.yml/dispatch",
                             json={"ref": "main"})  # fmt: skip
            assert r.status_code == 202 and r.json()["status"] == "pending_approval"
            assert writes(router) == []


async def test_rerun_rerun_failed_and_cancel_are_tracked(env: ActEnv) -> None:
    async with env() as e:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{GH}/actions/runs/100").mock(
                return_value=httpx.Response(200, json=run(100, conclusion="failure"))
            )
            rr = router.post(f"{GH}/actions/runs/100/rerun").mock(
                return_value=httpx.Response(201, json={})
            )
            rf = router.post(f"{GH}/actions/runs/100/rerun-failed-jobs").mock(
                return_value=httpx.Response(201, json={})
            )
            cancel = router.post(f"{GH}/actions/runs/101/cancel").mock(
                return_value=httpx.Response(202, json={})
            )
            for suffix in ("rerun", "rerun-failed"):
                r = await e.send("admin", "POST", f"{A}/runs/100/{suffix}")
                assert r.status_code == 202 and r.json()["status"] == "sent", r.text
            r = await e.send("admin", "POST", f"{A}/runs/101/cancel")
            assert r.status_code == 202 and r.json()["action"] == "cancel"
            assert rr.call_count == rf.call_count == cancel.call_count == 1

            # a run of a workflow off the dispatch allowlist cannot be re-run
            router.get(f"{GH}/actions/runs/200").mock(
                return_value=httpx.Response(200, json=run(200, path=".github/workflows/lint.yml"))
            )
            r = await e.send("admin", "POST", f"{A}/runs/200/rerun")
            assert r.status_code == 403
            # a run still in progress cannot be re-run
            router.get(f"{GH}/actions/runs/300").mock(
                return_value=httpx.Response(200, json=run(300, status="in_progress"))
            )
            assert (await e.send("admin", "POST", f"{A}/runs/300/rerun")).status_code == 409
            # CSRF and admin are required
            assert (await e.clients["admin"].post(f"{A}/runs/101/cancel")).status_code == 403
            assert (await e.send("a", "POST", f"{A}/runs/101/cancel")).status_code == 403
            # a GitHub failure is recorded as failed
            router.post(f"{GH}/actions/runs/102/cancel").mock(
                return_value=httpx.Response(409, json={"message": "Cannot cancel"})
            )
            assert (await e.send("admin", "POST", f"{A}/runs/102/cancel")).status_code == 409
        rows = await ops_rows(e)
        assert [(o, s, t) for o, s, t, _a in rows] == [
            ("actions.run_rerun", "succeeded", "run:100"),
            ("actions.run_rerun_failed", "succeeded", "run:100"),
            ("actions.run_cancel", "succeeded", "run:101"),
            ("actions.run_cancel", "failed", "run:102"),
        ]


async def test_prod_rerun_is_gated(env: ActEnv) -> None:
    async with env() as e:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{GH}/actions/runs/400").mock(
                return_value=httpx.Response(200, json=run(400, name="Deploy",
                                                          path=".github/workflows/deploy.yml"))
            )  # fmt: skip
            rr = router.post(f"{GH}/actions/runs/400/rerun").mock(
                return_value=httpx.Response(201, json={})
            )
            r = await e.send("admin", "POST", f"{A}/runs/400/rerun")
            assert r.status_code == 202 and r.json()["status"] == "pending_approval"
            assert rr.call_count == 0
            r = await approve(e, r.json()["approval"]["approval_id"])
            assert r.status_code == 200 and r.json()["status"] == "executed", r.text
            assert rr.call_count == 1


# --- webhook receiver ---------------------------------------------------------------------


def wf_payload(rid: int = 100, *, repo: str = "me/app", **kw: Any) -> dict[str, Any]:
    return {
        "action": "completed",
        "repository": {"full_name": repo, "id": 1},
        "workflow_run": run(rid, **kw),
        "sender": {"login": "me"},
    }


@contextlib.asynccontextmanager
async def public(e: Env) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=e.app), base_url="https://test") as c:
        yield c


def hook_headers(body: bytes, *, event: str = "workflow_run", delivery: str = "d-1",
                 secret: str = SECRET) -> dict[str, str]:  # fmt: skip
    return {
        "content-type": "application/json",
        "x-github-event": event,
        "x-github-delivery": delivery,
        "x-hub-signature-256": hooks.signature_for(secret, body),
    }


async def post_hook(c: AsyncClient, payload: Any, **kw: Any) -> httpx.Response:
    body = json.dumps(payload).encode()
    return await c.post(HOOK, content=body, headers=hook_headers(body, **kw))


async def events(e: Env) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = await e.sql(
        "SELECT delivery_id, event, action, repo, status, attempts, signature_valid "
        "FROM github.webhook_events ORDER BY id"
    )
    return rows


async def queued() -> list[str]:
    r = Redis.from_url(TEST_REDIS_URL)
    try:
        return [x.decode() for x in await r.lrange(hooks.QUEUE_KEY, 0, -1)]
    finally:
        await r.aclose()


async def test_webhook_valid_signature_is_stored_and_queued(env: ActEnv) -> None:
    async with env() as e, public(e) as c:
        r = await post_hook(c, wf_payload())
        assert r.status_code == 202, r.text
        assert r.json() == {"status": "accepted", "delivery_id": "d-1"}
        assert await events(e) == [
            ("d-1", "workflow_run", "completed", "me/app", "pending", 0, True)
        ]
        assert await queued() == ["d-1"]


async def test_webhook_bad_or_missing_signature_is_401_and_stores_nothing(env: ActEnv) -> None:
    async with env() as e, public(e) as c:
        body = json.dumps(wf_payload()).encode()
        h = hook_headers(body, secret="wrong-secret")
        assert (await c.post(HOOK, content=body, headers=h)).status_code == 401
        h = hook_headers(body)
        del h["x-hub-signature-256"]
        r = await c.post(HOOK, content=body, headers=h)
        assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_signature"
        h = hook_headers(body)
        assert (await c.post(HOOK, content=body + b" ", headers=h)).status_code == 401
        assert await events(e) == [] and await queued() == []
        redis = Redis.from_url(TEST_REDIS_URL)
        try:
            assert int(await redis.hget(hooks.REJECTED_KEY, "bad_signature") or 0) == 3
        finally:
            await redis.aclose()


async def test_webhook_duplicate_delivery_is_processed_once(env: ActEnv) -> None:
    async with env() as e, public(e) as c:
        assert (await post_hook(c, wf_payload())).status_code == 202
        r = await post_hook(c, wf_payload())
        assert r.status_code == 200 and r.json()["status"] == "duplicate"
        assert len(await events(e)) == 1 and await queued() == ["d-1"]
        settings = e.app.state.settings
        first = await hooks.process_next(e.maker, settings, delivery_id="d-1")
        assert first is not None and first.status == "processed"
        assert await hooks.process_next(e.maker, settings, delivery_id="d-1") is None
        r = await post_hook(c, wf_payload())  # a redelivery after processing: still once
        assert r.status_code == 200 and r.json()["status"] == "duplicate"
        assert [x[4:6] for x in await events(e)] == [("processed", 1)]


async def test_webhook_limits(env: ActEnv) -> None:
    async with env(GITHUB_WEBHOOK_MAX_BYTES=2048) as e, public(e) as c:
        big = wf_payload(pad="x" * 5000)
        r = await post_hook(c, big)
        assert r.status_code == 413 and r.json()["detail"]["code"] == "payload_too_large"
        body = json.dumps(wf_payload()).encode()
        h = hook_headers(body) | {"content-type": "application/x-www-form-urlencoded"}
        assert (await c.post(HOOK, content=body, headers=h)).status_code == 415
        h = hook_headers(body) | {"x-github-delivery": ""}
        assert (await c.post(HOOK, content=body, headers=h)).status_code == 400
        arr = b"[1, 2]"
        assert (await c.post(HOOK, content=arr, headers=hook_headers(arr))).status_code == 400
        assert await events(e) == []


async def test_webhook_without_secret_is_503(env: ActEnv) -> None:
    async with env(GITHUB_WEBHOOK_SECRET=None) as e, public(e) as c:
        r = await post_hook(c, wf_payload())
        assert r.status_code == 503 and r.json()["detail"]["code"] == "webhook_not_configured"
        assert await events(e) == []


async def test_webhook_unallowlisted_repo_and_unhandled_events_are_ignored(env: ActEnv) -> None:
    async with env() as e, public(e) as c:
        r = await post_hook(c, wf_payload(repo="stranger/repo"), delivery="d-x")
        assert r.status_code == 202 and r.json()["status"] == "ignored"
        r = await post_hook(c, {"repository": {"full_name": "me/app"}}, event="star",
                            delivery="d-y")  # fmt: skip
        assert r.status_code == 202 and r.json()["status"] == "ignored"
        rows = await events(e)
        assert [(x[0], x[4]) for x in rows] == [("d-x", "ignored"), ("d-y", "ignored")]
        assert await queued() == []
        assert await hooks.process_next(e.maker, e.app.state.settings) is None


async def test_webhook_route_needs_no_session_or_csrf(env: ActEnv) -> None:
    async with env() as e, public(e) as c:
        r = await post_hook(c, {"zen": "hi", "hook_id": 1, "repository": {"full_name": "me/app"}},
                            event="ping", delivery="p-1")  # fmt: skip
        assert r.status_code == 202, r.text
        res = await hooks.process_next(e.maker, e.app.state.settings)
        assert res is not None and res.status == "processed"


# --- worker -------------------------------------------------------------------------------


async def test_worker_processes_workflow_events_and_fires_the_failure_hook(env: ActEnv) -> None:
    seen: list[hooks.WorkflowFailure] = []

    async def on_fail(f: hooks.WorkflowFailure) -> None:
        seen.append(f)

    hooks.on_workflow_failed(on_fail)
    redis = Redis.from_url(TEST_REDIS_URL)
    try:
        async with env(GITHUB_WEBHOOK_POLL_SECONDS=1.0) as e, public(e) as c:
            settings = e.app.state.settings
            worker = WebhookWorker(settings, e.maker, redis)
            ok = wf_payload(100)
            ok["action"] = "in_progress"
            ok["workflow_run"] |= {"status": "in_progress", "conclusion": None}
            assert (await post_hook(c, ok, delivery="w-1")).status_code == 202
            failed = wf_payload(100, conclusion="failure", updated_at="2026-11-01T10:09:00Z")
            assert (await post_hook(c, failed, delivery="w-2")).status_code == 202
            jp = {"action": "completed", "repository": {"full_name": "me/app"},
                  "workflow_job": job(7, 100)}  # fmt: skip
            assert (await post_hook(c, jp, event="workflow_job", delivery="w-3")).status_code == 202
            # an older event arriving late never overwrites newer data
            stale = wf_payload(100, conclusion="success", updated_at="2026-11-01T09:00:00Z")
            assert (await post_hook(c, stale, delivery="w-4")).status_code == 202

            assert await worker.step() == 4
            # one id came off the list, the rest were found by the DB scan
            assert worker.processed == 4 and await queued() == ["w-4", "w-3", "w-2"]
            runs = await e.sql(
                "SELECT id, repo, status, conclusion, branch, head_sha, run_attempt "
                "FROM github.workflow_runs"
            )
            assert runs == [(100, "me/app", "completed", "failure", "main", SHA1, 1)]
            jobs = await e.sql("SELECT id, run_id, conclusion, steps FROM github.workflow_jobs")
            assert jobs[0][:3] == (7, 100, "failure") and jobs[0][3][0]["name"] == "Run pytest"
            assert [(f.run_id, f.conclusion, f.delivery_id) for f in seen] == [
                (100, "failure", "w-2")
            ]
            assert seen[0].branch == "main" and seen[0].repo == "me/app"
            assert {x[4] for x in await events(e)} == {"processed"}
            assert await worker.step() == 0  # nothing left
    finally:
        hooks.remove_workflow_failed(on_fail)
        await redis.aclose()


async def test_worker_retries_then_poisons(env: ActEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def broken(db: Any, ev: Any) -> None:
        calls.append(ev.delivery_id)
        await db.execute(text("INSERT INTO github.workflow_jobs (id, run_id) VALUES (1, 1)"))
        raise RuntimeError(f"handler bug {TOKEN}")

    async with env(GITHUB_WEBHOOK_MAX_ATTEMPTS=3) as e, public(e) as c:
        settings = e.app.state.settings
        assert (await post_hook(c, wf_payload(), delivery="bad-1")).status_code == 202
        monkeypatch.setattr(hooks, "_handle", broken)
        r1 = await hooks.process_next(e.maker, settings)
        assert r1 is not None and r1.status == "pending" and r1.attempts == 1
        rows = await e.sql(
            "SELECT status, attempts, error, next_attempt_at > received_at "
            "FROM github.webhook_events"
        )
        assert rows[0][:2] == ("pending", 1) and "handler bug" in rows[0][2]
        assert TOKEN not in rows[0][2]
        # the savepoint rolled the handler's partial write back
        assert await e.sql("SELECT count(*) FROM github.workflow_jobs") == [(0,)]
        r2 = await hooks.process_next(e.maker, settings)
        r3 = await hooks.process_next(e.maker, settings)
        assert r2 is not None and r2.status == "pending"
        assert r3 is not None and r3.status == "failed" and r3.attempts == 3
        assert await hooks.process_next(e.maker, settings) is None  # poison: never again
        assert calls == ["bad-1"] * 3
        rows = await e.sql("SELECT status, attempts, processed_at IS NOT NULL "
                           "FROM github.webhook_events")  # fmt: skip
        assert rows == [("failed", 3, True)]


async def test_retry_waits_for_its_backoff(env: ActEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(db: Any, ev: Any) -> None:
        raise ValueError("nope")

    async with env(GITHUB_WEBHOOK_RETRY_BASE_SECONDS=600.0) as e, public(e) as c:
        assert (await post_hook(c, wf_payload(), delivery="slow-1")).status_code == 202
        monkeypatch.setattr(hooks, "_handle", broken)
        settings = e.app.state.settings
        assert (await hooks.process_next(e.maker, settings)) is not None
        assert await hooks.process_next(e.maker, settings) is None  # not due for 10 minutes


async def test_worker_heartbeat_agent(env: ActEnv) -> None:
    from app.agents.registry import AgentRegistry, Heartbeat
    from app.workers.github_webhooks import GITHUB_WEBHOOKS

    async with env() as e:
        worker = WebhookWorker(e.app.state.settings, e.maker, None)
        hb = Heartbeat(AgentRegistry(e.maker), GITHUB_WEBHOOKS, 60, worker.state)
        await hb.beat()
        async with e.maker() as db:
            agent = await db.get(Agent, "github-webhooks")
            assert agent is not None and agent.kind == "integration"


# --- scheduler ----------------------------------------------------------------------------


async def test_actions_sync_task(env: ActEnv) -> None:
    async with env() as e:
        settings = e.app.state.settings
        await e.sql(
            "INSERT INTO github.repositories (id, full_name, owner, name, private, html_url) "
            "VALUES (1, 'me/app', 'me', 'app', true, 'x'), (2, 'me/off', 'me', 'off', true, 'x')"
        )
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{API}/user").mock(
                return_value=httpx.Response(200, json={"login": "me", "id": 1})
            )
            router.get(f"{GH}/actions/runs").mock(
                return_value=httpx.Response(200, json={"workflow_runs": [run(100), run(101)]})
            )
            router.get(f"{API}/repos/me/off/actions/runs").mock(
                return_value=httpx.Response(404, json={"message": "Not Found"})
            )
            ctx = TaskContext(settings, e.maker, None, github=e.app.state.github)
            out = await github_actions_sync(ctx)
        assert out["runs_upserted"] == 2 and out["skipped"] == ["me/off"]
        rows = await e.sql("SELECT id FROM github.workflow_runs ORDER BY id")
        assert rows == [(100,), (101,)]
