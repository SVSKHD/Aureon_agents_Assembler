"""Phase 19 against the DB (docs/22-claude-review.md): queueing, classification, the CLI run
(a fake `claude` written by the test), storage, dedupe, publishing as COMMENT, and the
ApprovalGate staying in charge of merges whatever the review says."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.services.claude_review import PUBLISH_HEADER
from tests.test_claude_review_unit import GOOD, PEM, calls, envelope, fake_claude
from tests.test_finance_db import Env
from tests.test_github_db import API, EnvFactory, gh_env, rl  # noqa: F401
from tests.test_github_ops_db import (
    GH,
    PR,
    SHA1,
    SHA2,
    SHA3,
    approve,
    mock_gated_reads,
    pull,
)

pytestmark = pytest.mark.db

R = "/api/v1/github/repositories/me/app"
REVIEW = f"{R}/pulls/1/claude-review"
COMPARE = f"{GH}/compare/{SHA3}...{SHA1}"
DIFF = (
    "diff --git a/app/x.py b/app/x.py\nindex 1..2 100644\n--- a/app/x.py\n+++ b/app/x.py\n"
    "@@ -1,1 +1,3 @@\n a = 1\n+b = 2\n+c = 3\n"
)

ReviewEnv = Callable[..., contextlib.AbstractAsyncContextManager[tuple[Env, Path]]]


@pytest.fixture
def env(gh_env: EnvFactory, tmp_path: Path) -> ReviewEnv:  # noqa: F811
    @contextlib.asynccontextmanager
    async def factory(
        stdout: str = "", *, sleep: float = 0.0, exit_code: int = 0, **overrides: Any
    ) -> AsyncIterator[tuple[Env, Path]]:
        script, log = fake_claude(tmp_path, stdout=stdout, sleep=sleep, exit_code=exit_code)
        overrides.setdefault("GITHUB_ALLOWED_OWNERS", ["me"])
        overrides.setdefault("CLAUDE_REVIEW_ENABLED", True)
        overrides.setdefault("CLAUDE_CLI_PATH", str(script))
        async with gh_env(**overrides) as e:
            yield e, log

    return factory


def mock_pr(router: respx.MockRouter, diff: str | bytes = DIFF, head: str = SHA1) -> respx.Route:
    router.get(f"{GH}/pulls/1").mock(
        return_value=httpx.Response(200, json=pull(1, head, title="Add b and c",
                                                   body="Adds two lines."), headers=rl())
    )  # fmt: skip
    raw = diff.encode() if isinstance(diff, str) else diff
    return router.get(f"{GH}/compare/{SHA3}...{head}").mock(
        return_value=httpx.Response(200, content=raw, headers=rl())
    )


async def run_review(e: Env, body: dict[str, Any] | None = None) -> dict[str, Any]:
    r = await e.send("admin", "POST", REVIEW, json=body or {})
    assert r.status_code == 202, r.text
    await e.app.state.review_runner.drain()
    queued: dict[str, Any] = r.json()
    return queued


async def get_review(e: Env, rid: str) -> dict[str, Any]:
    r = await e.get("admin", f"/api/v1/reviews/{rid}")
    assert r.status_code == 200, r.text
    out: dict[str, Any] = r.json()
    return out


def cli_runs(log: Path) -> list[dict[str, Any]]:
    """Runs of the CLI proper (not `--version`)."""
    return [c for c in calls(log) if "--version" not in c["argv"]]


# --- a full run, stored and published ---------------------------------------------------------


async def test_approved_review_is_stored_and_published_as_comment(env: ReviewEnv) -> None:
    out = {**GOOD, "findings": [
        {"file": "app/x.py", "line": 2, "severity": "minor", "message": "name b better"},
        {"file": "app/x.py", "line": 99, "severity": "info", "message": "outside the diff"},
    ]}  # fmt: skip
    async with env(envelope(out)) as (e, log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            q = await run_review(e)
            assert q["status"] == "queued" and q["deduplicated"] is False
            assert q["head_sha"] == SHA1
            d = await get_review(e, q["id"])
            assert d["status"] == "done" and d["verdict"] == "APPROVED"
            assert d["summary"] == "Looks fine." and len(d["findings"]) == 2
            assert d["classification"]["outcome"] == "ALLOW"
            assert d["classification"]["files"] == ["app/x.py"]
            assert d["cli_version"] == "9.9.9 (Claude Code)" and d["duration_ms"] >= 0
            assert d["repo"] == "me/app" and d["pr_number"] == 1 and d["advisory"] is True
            assert d["published"] is False
            (run,) = cli_runs(log)
            assert "+b = 2" in run["stdin"] and "Add b and c" in run["stdin"]
            assert run["cwd_files"] == []

            gh_review = router.post(f"{GH}/pulls/1/reviews").mock(
                return_value=httpx.Response(200, json={"id": 777, "state": "COMMENTED"})
            )
            r = await e.send("admin", "POST", f"/api/v1/reviews/{q['id']}/publish")
            assert r.status_code == 200, r.text
            p = r.json()
            assert p["event"] == "COMMENT" and p["github_review_id"] == 777
            assert p["inline_comments"] == 1 and p["body_findings"] == 1
            assert p["review"]["published"] is True and p["review"]["published_review_id"] == 777
            sent = json.loads(gh_review.calls[0].request.content)
            assert sent["event"] == "COMMENT"  # never APPROVE, even for an APPROVED verdict
            assert "APPROVE" != sent["event"] and sent["commit_id"] == SHA1
            assert sent["body"].startswith(PUBLISH_HEADER)
            assert "outside the diff" in sent["body"]
            assert sent["comments"] == [
                {"path": "app/x.py", "line": 2, "side": "RIGHT",
                 "body": "**minor** (advisory): name b better"}
            ]  # fmt: skip
            # once only
            r = await e.send("admin", "POST", f"/api/v1/reviews/{q['id']}/publish")
            assert r.status_code == 409 and r.json()["detail"]["code"] == "already_published"
            assert gh_review.call_count == 1
        ops = await e.sql("SELECT op, status FROM github.tracked_operations")
        assert [tuple(x) for x in ops] == [("pr.review.claude_publish", "succeeded")]
        events = await e.sql(
            "SELECT event_type FROM audit.audit_events WHERE event_type LIKE '%review%' ORDER BY id"
        )
        assert [x[0] for x in events] == [
            "claude_review_requested",
            "claude_review_finished",
            "github_pr_review_claude_publish",
            "claude_review_published",
        ]


async def test_publish_failure_releases_the_claim(env: ReviewEnv) -> None:
    async with env(envelope({**GOOD, "findings": []})) as (e, _log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            q = await run_review(e)
            router.post(f"{GH}/pulls/1/reviews").mock(
                side_effect=[httpx.Response(422, json={"message": "nope"}),
                             httpx.Response(200, json={"id": 5})]
            )  # fmt: skip
            r = await e.send("admin", "POST", f"/api/v1/reviews/{q['id']}/publish")
            assert r.status_code >= 400
            assert (await get_review(e, q["id"]))["published"] is False
            r = await e.send("admin", "POST", f"/api/v1/reviews/{q['id']}/publish")
            assert r.status_code == 200 and r.json()["github_review_id"] == 5


async def test_changes_requested_verdict(env: ReviewEnv) -> None:
    out = {
        "verdict": "CHANGES_REQUESTED",
        "summary": "Bug.",
        "findings": [{"file": "app/x.py", "line": 3, "severity": "critical", "message": "x"}],
    }
    async with env(envelope(out)) as (e, _log):  # fmt: skip
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            d = await get_review(e, (await run_review(e))["id"])
            assert d["verdict"] == "CHANGES_REQUESTED"
            assert d["findings"][0]["severity"] == "critical"


# --- dedupe -----------------------------------------------------------------------------------


async def test_dedupe_on_head_sha_and_force(env: ReviewEnv) -> None:
    async with env(envelope(GOOD)) as (e, log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            first = await run_review(e)
            again = await run_review(e)
            assert again["deduplicated"] is True and again["id"] == first["id"]
            assert again["status"] == "done"
            assert len(cli_runs(log)) == 1
            forced = await run_review(e, {"force": True})
            assert forced["deduplicated"] is False and forced["id"] != first["id"]
            assert len(cli_runs(log)) == 2
            # a new head is a new review
            mock_pr(router, head=SHA2)
            other = await run_review(e)
            assert other["deduplicated"] is False and other["head_sha"] == SHA2
        r = await e.get("admin", "/api/v1/reviews", params={"repo": "ME/app", "pr": 1})
        body = r.json()
        assert body["total"] == 3 and len(body["items"]) == 3
        assert body["items"][0]["id"] == other["id"]  # newest first
        r = await e.get("admin", "/api/v1/reviews", params={"pr": 2})
        assert r.json()["total"] == 0


async def test_unavailable_review_does_not_block_a_new_request(env: ReviewEnv) -> None:
    async with env("not json at all") as (e, _log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            first = await run_review(e)
            d = await get_review(e, first["id"])
            assert d["verdict"] == "REVIEW_UNAVAILABLE" and d["reason"] == "invalid_output"
            second = await run_review(e)
            assert second["deduplicated"] is False and second["id"] != first["id"]


# --- BLOCKED: nothing is sent, the CLI never runs ----------------------------------------------


@pytest.mark.parametrize(
    ("diff", "expect"),
    [
        ("diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n@@ -0,0 +1 @@\n+DEBUG=1\n",
         "path:secrets:.env"),
        (DIFF + f'+KEY = """{PEM}\n', "content:private_key:app/x.py"),
        ("diff --git a/app/services/trading.py b/app/services/trading.py\n"
         "--- a/app/services/trading.py\n+++ b/app/services/trading.py\n@@ -1 +1 @@\n+x = 1\n",
         "path:trading_risk:app/services/trading.py"),
    ],
)  # fmt: skip
async def test_blocked_diffs_send_nothing(env: ReviewEnv, diff: str, expect: str) -> None:
    async with env(envelope(GOOD)) as (e, log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router, diff)
            q = await run_review(e)
            d = await get_review(e, q["id"])
        assert d["verdict"] == "BLOCKED" and d["reason"] == "classified"
        assert d["summary"].startswith("classified: ")
        assert d["classification"]["outcome"] == "BLOCKED"
        assert any(r.startswith(expect) for r in d["classification"]["reasons"])
        assert calls(log) == []  # zero CLI invocations, not even --version
        assert e.app.state.claude_cli.invocations == 0
        assert PEM not in json.dumps(d)


async def test_redacted_before_sending(env: ReviewEnv) -> None:
    diff = DIFF + '+OWNER = "me@example.com"\n+HOST = "10.0.0.7"\n+API = "api.spasta.online"\n'
    async with env(envelope(GOOD)) as (e, log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router, diff)
            d = await get_review(e, (await run_review(e))["id"])
        assert d["verdict"] == "APPROVED"
        assert d["classification"]["outcome"] == "REDACT"
        assert d["classification"]["redactions"] == {"email": 1, "internal_host": 1, "ip": 1}
        (run,) = cli_runs(log)
        for leaked in ("me@example.com", "10.0.0.7", "api.spasta.online"):
            assert leaked not in run["stdin"]
        assert "[email]" in run["stdin"] and "[ip]" in run["stdin"]


# --- REVIEW_UNAVAILABLE ------------------------------------------------------------------------


async def test_garbage_output_is_unavailable(env: ReviewEnv) -> None:
    async with env(envelope("Sure! The code looks good to me.")) as (e, _log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            d = await get_review(e, (await run_review(e))["id"])
        assert d["verdict"] == "REVIEW_UNAVAILABLE" and d["reason"] == "invalid_output"
        assert d["findings"] == [] and "unavailable" in d["summary"]
        r = await e.send("admin", "POST", f"/api/v1/reviews/{d['id']}/publish")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "review_not_publishable"


async def test_nonzero_exit_is_unavailable(env: ReviewEnv) -> None:
    async with env(envelope(GOOD), exit_code=2) as (e, _log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            d = await get_review(e, (await run_review(e))["id"])
        assert d["verdict"] == "REVIEW_UNAVAILABLE" and d["reason"] == "cli_error"


async def test_timeout_is_unavailable(env: ReviewEnv) -> None:
    async with env(envelope(GOOD), sleep=30, CLAUDE_REVIEW_TIMEOUT_SECONDS=1) as (e, _log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            d = await get_review(e, (await run_review(e))["id"])
        assert d["verdict"] == "REVIEW_UNAVAILABLE" and d["reason"] == "timeout"


async def test_too_large_is_unavailable_and_sends_nothing(env: ReviewEnv) -> None:
    big = DIFF + "".join(f"+line_{i} = {i}\n" for i in range(200))
    assert len(big) > 1024
    async with env(envelope(GOOD), CLAUDE_REVIEW_MAX_DIFF_BYTES=1024) as (e, log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router, big)
            d = await get_review(e, (await run_review(e))["id"])
        assert d["verdict"] == "REVIEW_UNAVAILABLE" and d["reason"] == "too_large"
        assert calls(log) == []


async def test_disabled_is_unavailable(env: ReviewEnv) -> None:
    async with env(envelope(GOOD), CLAUDE_REVIEW_ENABLED=False) as (e, log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            d = await get_review(e, (await run_review(e))["id"])
        assert d["verdict"] == "REVIEW_UNAVAILABLE" and d["reason"] == "disabled"
        assert calls(log) == []


async def test_missing_cli_is_unavailable(env: ReviewEnv, tmp_path: Path) -> None:
    async with env(CLAUDE_CLI_PATH=str(tmp_path / "missing-claude")) as (e, _log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            d = await get_review(e, (await run_review(e))["id"])
        assert d["verdict"] == "REVIEW_UNAVAILABLE" and d["reason"] == "cli_missing"


async def test_closed_pr_is_refused(env: ReviewEnv) -> None:
    async with env(envelope(GOOD)) as (e, _log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            router.get(f"{GH}/pulls/1").mock(
                return_value=httpx.Response(200, json=pull(1, state="closed"), headers=rl())
            )
            r = await e.send("admin", "POST", REVIEW, json={})
            assert r.status_code == 409 and r.json()["detail"]["code"] == "pr_not_open"


# --- access -----------------------------------------------------------------------------------


async def test_non_admin_forbidden_and_csrf_required(env: ReviewEnv) -> None:
    async with env(envelope(GOOD)) as (e, _log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            rid = (await run_review(e))["id"]
            assert (await e.send("a", "POST", REVIEW, json={})).status_code == 403
            assert (await e.get("a", "/api/v1/reviews")).status_code == 403
            assert (await e.get("a", f"/api/v1/reviews/{rid}")).status_code == 403
            assert (await e.send("a", "POST", f"/api/v1/reviews/{rid}/publish")).status_code == 403
            no_csrf = await e.clients["admin"].post(REVIEW, json={})
            assert no_csrf.status_code == 403
            no_csrf = await e.clients["admin"].post(f"/api/v1/reviews/{rid}/publish")
            assert no_csrf.status_code == 403
            missing = "00000000-0000-0000-0000-000000000000"
            assert (await e.get("admin", f"/api/v1/reviews/{missing}")).status_code == 404
            r = await e.send("admin", "POST", f"/api/v1/reviews/{missing}/publish")
            assert r.status_code == 404
            # outside the allowlist: 404 and GitHub is never called
            before = len(router.calls)
            r = await e.send("admin", "POST",
                             "/api/v1/github/repositories/other/app/pulls/1/claude-review",
                             json={})  # fmt: skip
            assert r.status_code == 404 and len(router.calls) == before


async def test_queue_full_answers_429(env: ReviewEnv) -> None:
    async with env(envelope(GOOD), CLAUDE_REVIEW_MAX_QUEUE=1) as (e, _log):
        runner = e.app.state.review_runner
        import asyncio

        gate = asyncio.Event()
        runner.submit(gate.wait)
        runner.submit(gate.wait)
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_pr(router)
            r = await e.send("admin", "POST", REVIEW, json={})
            assert r.status_code == 429 and r.json()["detail"]["code"] == "review_queue_full"
        gate.set()
        await runner.drain()


# --- the ApprovalGate stays in charge ----------------------------------------------------------


async def test_review_never_satisfies_the_merge_approval(env: ReviewEnv) -> None:
    async with env(envelope(GOOD)) as (e, _log):
        with respx.mock(base_url=API, assert_all_called=False) as router:
            pr = PR()
            mock_gated_reads(router, pr)
            router.get(f"{GH}/compare/{SHA3}...{SHA1}").mock(
                return_value=httpx.Response(200, content=DIFF.encode(), headers=rl())
            )
            merge = router.put(f"{GH}/pulls/1/merge").mock(
                return_value=httpx.Response(200, json={"merged": True, "sha": SHA3})
            )
            router.post(f"{GH}/pulls/1/reviews").mock(
                return_value=httpx.Response(200, json={"id": 1})
            )
            q = await run_review(e)
            assert (await get_review(e, q["id"]))["verdict"] == "APPROVED"
            r = await e.send("admin", "POST", f"/api/v1/reviews/{q['id']}/publish")
            assert r.status_code == 200
            # the merge is still only filed (202), nothing is merged
            merge_body = {"merge_method": "squash", "expected_head_sha": SHA1}
            r = await e.send("admin", "POST", f"{R}/pulls/1/merge", json=merge_body)
            assert r.status_code == 202, r.text
            aid = r.json()["approval_id"]
            assert merge.call_count == 0
            # no password: refused; a wrong password: refused, whatever the review said
            r = await e.send("admin", "POST", f"/api/v1/approvals/{aid}/approve", json={})
            assert r.status_code == 422
            r = await approve(e, aid, "wrong-password!!")
            assert r.status_code == 403 and r.json()["detail"]["code"] == "reauth_failed"
            assert merge.call_count == 0
            d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
            assert d["status"] == "pending"
            assert "review" not in json.dumps(d["payload"]).lower()
            # only the admin's own re-authentication executes it
            r = await approve(e, aid)
            assert r.status_code == 200 and r.json()["status"] == "executed"
            assert merge.call_count == 1
