"""Phase 16 against the DB (docs/19-github-issues-prs.md): issues, task links, PRs, tracked
operations and the approval-gated operations, with GitHub mocked by respx."""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.auth.security import hash_password
from app.models.auth import ROLE_ADMIN, Role, User
from app.services import github_ops as ops
from tests.test_finance_db import PASSWORD, Env
from tests.test_github_db import API, TOKEN, EnvFactory, gh_env, rl  # noqa: F401

pytestmark = pytest.mark.db

R = "/api/v1/github/repositories/me/app"
GH = "/repos/me/app"
SHA1 = "1" * 40
SHA2 = "2" * 40
SHA3 = "3" * 40

OpsEnv = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


@pytest.fixture
def env(gh_env: EnvFactory) -> OpsEnv:  # noqa: F811
    @contextlib.asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[Env]:
        overrides.setdefault("GITHUB_ALLOWED_OWNERS", ["me"])
        async with gh_env(**overrides) as e:
            yield e

    return factory


def writes(router: respx.MockRouter) -> list[httpx.Request]:
    return [c.request for c in router.calls if c.request.method != "GET"]


def issue(n: int, **kw: Any) -> dict[str, Any]:
    return {
        "number": n,
        "node_id": f"I_{n}",
        "title": f"Issue {n}",
        "body": "body",
        "state": "open",
        "labels": [{"name": "bug"}],
        "assignees": [{"login": "me"}],
        "milestone": {"number": 1, "title": "v1", "state": "open"},
        "user": {"login": "me"},
        "comments": 1,
        "created_at": "2026-10-01T10:00:00Z",
        "html_url": f"https://github.com/me/app/issues/{n}",
        **kw,
    }


def comment(cid: int, body: str = "hi") -> dict[str, Any]:
    return {"id": cid, "body": body, "user": {"login": "me"}, "created_at": "2026-10-01T10:00:00Z"}


def pull(n: int, head: str = SHA1, **kw: Any) -> dict[str, Any]:
    return {
        "number": n,
        "node_id": f"PR_{n}",
        "title": f"PR {n}",
        "body": "pr body",
        "state": "open",
        "draft": False,
        "merged": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "rebaseable": True,
        "user": {"login": "me"},
        "head": {"ref": "feature", "sha": head, "label": "me:feature",
                 "repo": {"full_name": "me/app"}},
        "base": {"ref": "main", "sha": SHA3, "repo": {"full_name": "me/app"}},
        "labels": [],
        "requested_reviewers": [{"login": "rev"}],
        "commits": 2,
        "additions": 10,
        "deletions": 1,
        "changed_files": 1,
        "html_url": f"https://github.com/me/app/pull/{n}",
        **kw,
    }  # fmt: skip


class PR:
    """A mutable PR #1 served for both JSON and the unified diff (by Accept header)."""

    def __init__(self, head: str = SHA1, diff: bytes = b"diff --git a/x b/x\n+1\n") -> None:
        self.head, self.diff, self.state = head, diff, "open"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("accept") == ops.DIFF_ACCEPT:
            return httpx.Response(200, content=self.diff, headers=rl())
        return httpx.Response(200, json=pull(1, self.head, state=self.state), headers=rl())


async def ops_rows(env: Env) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = await env.sql(
        "SELECT op, status, target, approval_request_id, error FROM github.tracked_operations "
        "ORDER BY id"
    )
    return rows


async def audit_count(env: Env, event: str) -> int:
    rows = await env.sql("SELECT count(*) FROM audit.audit_events WHERE event_type = :e", e=event)
    return int(rows[0][0])


# --- issues -------------------------------------------------------------------------------


async def test_issue_list_detail_and_writes(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API) as router:
            lst = router.get(f"{GH}/issues").mock(
                return_value=httpx.Response(
                    200,
                    json=[issue(1), issue(2, pull_request={"url": "x"})],
                    headers={**rl(), "link": f'<{API}{GH}/issues?page=2>; rel="next"'},
                )
            )
            r = await e.get("admin", f"{R}/issues", params={
                "state": "all", "labels": "bug,ui", "assignee": "me",
                "since": "2026-10-01T00:00:00Z", "per_page": 10, "page": 1})  # fmt: skip
            assert r.status_code == 200, r.text
            body = r.json()
            assert [i["number"] for i in body["items"]] == [1]  # the PR is filtered out
            assert body["has_next"] is True and body["items"][0]["labels"] == ["bug"]
            q = lst.calls[0].request.url.params
            assert q["state"] == "all" and q["labels"] == "bug,ui" and q["assignee"] == "me"
            assert q["since"].startswith("2026-10-01T00:00:00") and q["per_page"] == "10"
            r = await e.get("admin", f"{R}/issues", params={"include_pulls": "true"})
            assert len(r.json()["items"]) == 2

            router.get(f"{GH}/issues/1").mock(return_value=httpx.Response(200, json=issue(1)))
            router.get(f"{GH}/issues/1/comments").mock(
                return_value=httpx.Response(200, json=[comment(10), comment(11, TOKEN)])
            )
            r = await e.get("admin", f"{R}/issues/1")
            assert r.status_code == 200
            d = r.json()
            assert d["milestone"]["title"] == "v1" and len(d["comment_list"]) == 2
            assert TOKEN not in r.text  # bodies are token-redacted

            create = router.post(f"{GH}/issues").mock(
                return_value=httpx.Response(201, json=issue(3))
            )
            r = await e.send("admin", "POST", f"{R}/issues", json={
                "title": "New", "body": "b", "labels": ["bug"], "assignees": ["me"],
                "milestone": 1})  # fmt: skip
            assert r.status_code == 201 and r.json()["number"] == 3
            import json as _json

            sent = _json.loads(create.calls[0].request.content)
            assert sent == {"title": "New", "body": "b", "labels": ["bug"], "assignees": ["me"],
                            "milestone": 1}  # fmt: skip

            upd = router.patch(f"{GH}/issues/1").mock(
                side_effect=lambda req: httpx.Response(
                    200, json=issue(1, **_json.loads(req.content))
                )
            )
            r = await e.send("admin", "PATCH", f"{R}/issues/1",
                             json={"title": "T2", "milestone": None})  # fmt: skip
            assert r.status_code == 200 and r.json()["title"] == "T2"
            assert _json.loads(upd.calls[0].request.content) == {"title": "T2", "milestone": None}
            r = await e.send("admin", "PATCH", f"{R}/issues/1", json={})
            assert r.status_code == 422
            r = await e.send("admin", "POST", f"{R}/issues/1/close",
                             json={"state_reason": "not_planned"})  # fmt: skip
            assert r.json()["state"] == "closed"
            assert _json.loads(upd.calls[-1].request.content) == {
                "state": "closed", "state_reason": "not_planned"}  # fmt: skip
            r = await e.send("admin", "POST", f"{R}/issues/1/reopen")
            assert r.json()["state"] == "open"

            # comments
            router.post(f"{GH}/issues/1/comments").mock(
                return_value=httpx.Response(201, json=comment(20, "c"))
            )
            router.patch(f"{GH}/issues/comments/20").mock(
                return_value=httpx.Response(200, json=comment(20, "edited"))
            )
            router.delete(f"{GH}/issues/comments/20").mock(return_value=httpx.Response(204))
            r = await e.send("admin", "POST", f"{R}/issues/1/comments", json={"body": "c"})
            assert r.status_code == 201 and r.json()["id"] == 20
            r = await e.send("admin", "PATCH", f"{R}/issues/comments/20", json={"body": "edited"})
            assert r.json()["body"] == "edited"
            r = await e.send("admin", "DELETE", f"{R}/issues/comments/20")
            assert r.status_code == 204

            # labels + lock
            router.post(f"{GH}/issues/1/labels").mock(
                return_value=httpx.Response(200, json=[{"name": "bug"}, {"name": "ui"}])
            )
            rm = router.delete(f"{GH}/issues/1/labels/needs%20triage").mock(
                return_value=httpx.Response(200, json=[{"name": "bug"}])
            )
            router.put(f"{GH}/issues/1/lock").mock(return_value=httpx.Response(204))
            router.delete(f"{GH}/issues/1/lock").mock(return_value=httpx.Response(204))
            r = await e.send("admin", "POST", f"{R}/issues/1/labels", json={"labels": ["ui"]})
            assert r.json() == ["bug", "ui"]
            r = await e.send("admin", "DELETE", f"{R}/issues/1/labels/needs triage")
            assert r.json() == ["bug"] and rm.called
            r = await e.send("admin", "PUT", f"{R}/issues/1/lock", json={"lock_reason": "spam"})
            assert r.status_code == 204
            r = await e.send("admin", "DELETE", f"{R}/issues/1/lock")
            assert r.status_code == 204

            # a failed write is tracked as failed, audited, and answered with GitHub's error
            router.post(f"{GH}/issues/2/comments").mock(
                return_value=httpx.Response(422, json={"message": f"Validation Failed {TOKEN}"})
            )
            r = await e.send("admin", "POST", f"{R}/issues/2/comments", json={"body": "x"})
            assert r.status_code == 422 and r.json()["detail"]["code"] == "github_conflict"
            assert TOKEN not in r.text

        rows = await ops_rows(e)
        assert [(op, st) for op, st, *_ in rows] == [
            ("issue.create", "succeeded"),
            ("issue.update", "succeeded"),
            ("issue.close", "succeeded"),
            ("issue.reopen", "succeeded"),
            ("issue.comment.create", "succeeded"),
            ("issue.comment.edit", "succeeded"),
            ("issue.comment.delete", "succeeded"),
            ("issue.label.add", "succeeded"),
            ("issue.label.remove", "succeeded"),
            ("issue.lock", "succeeded"),
            ("issue.unlock", "succeeded"),
            ("issue.comment.create", "failed"),
        ]
        assert TOKEN not in (rows[-1][4] or "") and "github_conflict" in rows[-1][4]
        assert await audit_count(e, "github_issue_create") == 1
        assert await audit_count(e, "github_issue_comment_create") == 2
        r = await e.get("admin", "/api/v1/github/operations", params={"status": "failed"})
        assert r.json()["total"] == 1 and r.json()["items"][0]["op"] == "issue.comment.create"
        r = await e.get("admin", "/api/v1/github/operations", params={"limit": 5})
        assert r.json()["total"] == 12 and len(r.json()["items"]) == 5


async def test_tracked_payload_is_redacted(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API) as router:
            router.post(f"{GH}/issues").mock(return_value=httpx.Response(201, json=issue(3)))
            r = await e.send("admin", "POST", f"{R}/issues",
                             json={"title": "leak", "body": f"oops {TOKEN}"})  # fmt: skip
            assert r.status_code == 201
        rows = await e.sql("SELECT request_payload::text FROM github.tracked_operations")
        assert TOKEN not in rows[0][0] and "[REDACTED]" in rows[0][0]


# --- links --------------------------------------------------------------------------------


async def test_issue_task_links(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API) as router:
            router.get(f"{GH}/issues/1").mock(return_value=httpx.Response(200, json=issue(1)))
            router.get(f"{GH}/issues/1/comments").mock(return_value=httpx.Response(200, json=[]))
            router.get(f"{GH}/issues/9").mock(return_value=httpx.Response(404, json={}))
            body = {"task_ref": "task:abc-123"}
            r = await e.send("admin", "POST", f"{R}/issues/1/links", json=body)
            assert r.status_code == 201, r.text
            link = r.json()
            assert link["issue_node_id"] == "I_1" and link["task_kind"] == "spasta_task"
            assert link["repo_full_name"] == "me/app"
            r = await e.send("admin", "POST", f"{R}/issues/1/links", json=body)
            assert r.status_code == 200 and r.json()["id"] == link["id"]  # idempotent
            r = await e.send("admin", "POST", f"{R}/issues/9/links", json=body)
            assert r.status_code == 404
            r = await e.send("admin", "POST", f"{R}/issues/1/links", json={"task_ref": "a b"})
            assert r.status_code == 422
            r = await e.get("admin", f"{R}/issues/1")
            assert [x["task_ref"] for x in r.json()["links"]] == ["task:abc-123"]
        r = await e.get("admin", "/api/v1/github/links", params={"task_ref": "task:abc-123"})
        assert [x["issue_number"] for x in r.json()] == [1]
        assert (await e.get("admin", "/api/v1/github/links")).status_code == 422
        assert (await e.get("a", "/api/v1/github/links?task_ref=x")).status_code == 403
        r = await e.send("admin", "DELETE", f"{R}/issues/1/links", params={"task_ref": "nope"})
        assert r.status_code == 404
        r = await e.send(
            "admin", "DELETE", f"{R}/issues/1/links", params={"task_ref": "task:abc-123"}
        )
        assert r.status_code == 204
        r = await e.get("admin", "/api/v1/github/links", params={"task_ref": "task:abc-123"})
        assert r.json() == []
        assert await audit_count(e, "github_issue_link_created") == 1
        assert await audit_count(e, "github_issue_link_deleted") == 1


# --- pull requests ------------------------------------------------------------------------


async def test_pull_reads(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API) as router:
            pr = PR()
            lst = router.get(f"{GH}/pulls").mock(
                return_value=httpx.Response(200, json=[pull(1), pull(2)], headers=rl())
            )
            router.get(f"{GH}/pulls/1").mock(side_effect=pr)
            router.get(f"{GH}/pulls/1/files").mock(
                return_value=httpx.Response(200, json=[
                    {"filename": "a.py", "status": "modified", "additions": 1, "deletions": 0,
                     "changes": 1, "patch": "@@ -1 +1 @@\n+x"},
                    {"filename": "big.txt", "status": "added", "patch": "+" * 200_000},
                ])
            )  # fmt: skip
            router.get(f"{GH}/pulls/1/commits").mock(
                return_value=httpx.Response(200, json=[{
                    "sha": SHA1, "commit": {"message": f"feat {TOKEN}",
                                            "author": {"name": "Me"}}}])
            )  # fmt: skip
            router.get(f"{GH}/pulls/1/comments").mock(
                return_value=httpx.Response(
                    200, json=[comment(5) | {"path": "a.py", "line": 1, "side": "RIGHT"}]
                )
            )
            router.get(f"{GH}/issues/1/comments").mock(
                return_value=httpx.Response(200, json=[comment(6)])
            )
            router.get(f"{GH}/pulls/1/reviews").mock(
                return_value=httpx.Response(200, json=[
                    {"id": 7, "user": {"login": "rev"}, "state": "APPROVED", "body": ""}])
            )  # fmt: skip
            r = await e.get("admin", f"{R}/pulls", params={"state": "all", "base": "main",
                                                           "head": "me:feature"})  # fmt: skip
            assert r.status_code == 200 and len(r.json()["items"]) == 2
            q = lst.calls[0].request.url.params
            assert q["base"] == "main" and q["head"] == "me:feature" and q["state"] == "all"
            assert (await e.get("admin", f"{R}/pulls", params={"base": "../x"})).status_code == 400
            r = await e.get("admin", f"{R}/pulls/1")
            d = r.json()
            assert d["head"]["sha"] == SHA1 and d["mergeable"] is True
            assert d["requested_reviewers"] == ["rev"]
            r = await e.get("admin", f"{R}/pulls/1/files")
            files = r.json()
            assert files[0]["patch"].startswith("@@") and files[1]["patch_truncated"] is True
            r = await e.get("admin", f"{R}/pulls/1/commits")
            assert r.json()[0]["sha"] == SHA1 and TOKEN not in r.text
            r = await e.get("admin", f"{R}/pulls/1/comments")
            assert r.json()[0]["path"] == "a.py"
            r = await e.get("admin", f"{R}/pulls/1/issue-comments")
            assert r.json()[0]["id"] == 6
            r = await e.get("admin", f"{R}/pulls/1/reviews")
            assert r.json()[0]["state"] == "APPROVED"
            r = await e.get("admin", f"{R}/pulls/1/merge-status")
            assert r.json() == {
                "number": 1, "state": "open", "merged": False, "draft": False,
                "mergeable": True, "mergeable_state": "clean", "rebaseable": True,
                "head_ref": "feature", "head_sha": SHA1, "base_ref": "main"}  # fmt: skip
            r = await e.get("admin", f"{R}/pulls/1/diff")
            assert r.json()["diff"].startswith("diff --git") and r.json()["truncated"] is False
            assert not writes(router)


async def test_diff_cap(env: OpsEnv) -> None:
    async with env(GITHUB_DIFF_MAX_BYTES=1024) as e:
        with respx.mock(base_url=API) as router:
            router.get(f"{GH}/pulls/1").mock(side_effect=PR(diff=b"+" * 5000))
            router.get(f"{GH}/pulls/2").mock(
                return_value=httpx.Response(406, json={"message": "diff too large"})
            )
            r = await e.get("admin", f"{R}/pulls/1/diff")
            d = r.json()
            assert d["truncated"] is True and d["bytes_returned"] == 1024
            assert len(d["diff"]) == 1024 and d["max_bytes"] == 1024
            r = await e.get("admin", f"{R}/pulls/2/diff")
            assert r.status_code == 200
            assert r.json()["truncated"] is True and r.json()["too_large_upstream"] is True


async def test_pull_writes(env: OpsEnv) -> None:
    import json as _json

    async with env() as e:
        with respx.mock(base_url=API) as router:
            create = router.post(f"{GH}/pulls").mock(return_value=httpx.Response(201, json=pull(4)))
            upd = router.patch(f"{GH}/pulls/4").mock(
                return_value=httpx.Response(200, json=pull(4, title="T", state="closed"))
            )
            rc = router.post(f"{GH}/pulls/4/comments").mock(
                return_value=httpx.Response(201, json=comment(30) | {"path": "a.py"})
            )
            reply = router.post(f"{GH}/pulls/4/comments/30/replies").mock(
                return_value=httpx.Response(201, json=comment(31) | {"in_reply_to_id": 30})
            )
            router.post(f"{GH}/issues/4/comments").mock(
                return_value=httpx.Response(201, json=comment(32))
            )
            rev = router.post(f"{GH}/pulls/4/reviews").mock(
                return_value=httpx.Response(200, json={"id": 40, "state": "CHANGES_REQUESTED"})
            )
            r = await e.send("admin", "POST", f"{R}/pulls", json={
                "head": "feature", "base": "main", "title": "Add", "body": "b",
                "draft": True})  # fmt: skip
            assert r.status_code == 201 and r.json()["number"] == 4
            assert _json.loads(create.calls[0].request.content)["draft"] is True
            r = await e.send("admin", "POST", f"{R}/pulls", json={
                "head": "../evil", "base": "main", "title": "x"})  # fmt: skip
            assert r.status_code == 400
            r = await e.send("admin", "PATCH", f"{R}/pulls/4",
                             json={"title": "T", "state": "closed", "base": "main"})  # fmt: skip
            assert r.json()["state"] == "closed"
            assert _json.loads(upd.calls[0].request.content) == {
                "title": "T", "state": "closed", "base": "main"}  # fmt: skip
            r = await e.send("admin", "POST", f"{R}/pulls/4/comments", json={
                "body": "nit", "commit_id": SHA1, "path": "a.py", "line": 3,
                "side": "RIGHT"})  # fmt: skip
            assert r.status_code == 201 and r.json()["path"] == "a.py"
            assert _json.loads(rc.calls[0].request.content)["line"] == 3
            r = await e.send("admin", "POST", f"{R}/pulls/4/comments",
                             json={"body": "ok", "in_reply_to": 30})  # fmt: skip
            assert r.status_code == 201 and reply.called
            r = await e.send("admin", "POST", f"{R}/pulls/4/issue-comments", json={"body": "hi"})
            assert r.status_code == 201 and r.json()["id"] == 32
            r = await e.send("admin", "POST", f"{R}/pulls/4/reviews",
                             json={"event": "REQUEST_CHANGES"})  # fmt: skip
            assert r.status_code == 422
            r = await e.send("admin", "POST", f"{R}/pulls/4/reviews",
                             json={"event": "MERGE"})  # fmt: skip
            assert r.status_code == 422
            r = await e.send("admin", "POST", f"{R}/pulls/4/reviews", json={
                "event": "REQUEST_CHANGES", "body": "fix",
                "comments": [{"path": "a.py", "body": "here", "line": 1}]})  # fmt: skip
            assert r.status_code == 201 and r.json()["id"] == 40
            assert _json.loads(rev.calls[0].request.content)["event"] == "REQUEST_CHANGES"
            # non-admin: no review, no PR
            r = await e.send("a", "POST", f"{R}/pulls/4/reviews", json={"event": "APPROVE"})
            assert r.status_code == 403
        assert [op for op, *_ in await ops_rows(e)] == [
            "pr.create",
            "pr.update",
            "pr.review_comment.create",
            "pr.review_comment.reply",
            "pr.comment.create",
            "pr.review.create",
        ]


# --- gated operations ---------------------------------------------------------------------


def mock_gated_reads(router: respx.MockRouter, pr: PR | None = None) -> None:
    router.get(f"{GH}/pulls/1").mock(side_effect=pr or PR())
    router.get(f"{GH}/git/ref/heads/feature").mock(
        return_value=httpx.Response(200, json={"ref": "refs/heads/feature",
                                               "object": {"sha": SHA1}})
    )  # fmt: skip
    router.get(f"{GH}/git/commits/{SHA2}").mock(
        return_value=httpx.Response(200, json={"sha": SHA2})
    )
    router.get(f"{GH}/branches/main/protection").mock(
        return_value=httpx.Response(200, json={"enforce_admins": {"enabled": True}})
    )
    router.get(GH).mock(
        return_value=httpx.Response(200, json={"full_name": "me/app", "visibility": "private",
                                               "default_branch": "main"})
    )  # fmt: skip
    router.get(f"{GH}/actions/permissions").mock(
        return_value=httpx.Response(200, json={"enabled": True, "allowed_actions": "all"})
    )


PROTECTION = {
    "required_status_checks": {"strict": True, "contexts": ["ci"]},
    "enforce_admins": True,
    "required_pull_request_reviews": None,
    "restrictions": None,
}
GATED = [
    ("POST", f"{R}/pulls/1/merge", {"merge_method": "squash", "expected_head_sha": SHA1},
     "pr.merge"),
    ("POST", f"{R}/force-push", {"branch": "feature", "sha": SHA2}, "ref.force_update"),
    ("PUT", f"{R}/branch-protection/main", PROTECTION, "branch_protection.update"),
    ("DELETE", f"{R}/branch-protection/main", None, "branch_protection.delete"),
    ("DELETE", f"{R}/branches/feature", None, "branch.delete"),
    ("PATCH", f"{R}/settings", {"visibility": "public", "allow_squash_merge": False},
     "repo.update_settings"),
    ("PUT", f"{R}/actions-permissions", {"enabled": False}, "actions.set_permissions"),
]  # fmt: skip


@pytest.mark.parametrize(("method", "url", "body", "op"), GATED, ids=[g[3] for g in GATED])
async def test_gated_endpoints_answer_202_and_write_nothing(
    env: OpsEnv, method: str, url: str, body: dict[str, Any] | None, op: str
) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            r = await e.send("admin", method, url, json=body)
            assert r.status_code == 202, r.text
            g = r.json()
            assert g["status"] == "pending" and g["artifact_sha256"] and g["operation_id"]
            assert writes(router) == []  # zero GitHub write calls before approval
            # non-admins cannot even file one
            assert (await e.send("a", method, url, json=body)).status_code == 403
            assert writes(router) == []
        rows = await ops_rows(e)
        assert [(o, s, str(a)) for o, s, _t, a, _e in rows] == [
            (op, "pending_approval", g["approval_id"])
        ]
        r = await e.get("admin", f"/api/v1/approvals/{g['approval_id']}")
        d = r.json()
        assert d["status"] == "pending" and d["kind"] == f"github.{op}"
        assert [ev["event"] for ev in d["events"]] == ["requested"]


async def _merge_request(e: Env, who: str = "admin") -> str:
    r = await e.send(who, "POST", f"{R}/pulls/1/merge",
                     json={"merge_method": "squash", "expected_head_sha": SHA1})  # fmt: skip
    assert r.status_code == 202, r.text
    approval_id: str = r.json()["approval_id"]
    return approval_id


def approve(e: Env, aid: str, password: str = PASSWORD, who: str = "admin") -> Any:
    return e.send(who, "POST", f"/api/v1/approvals/{aid}/approve", json={"password": password})


async def test_merge_runs_only_after_approval_and_only_once(env: OpsEnv) -> None:
    import json as _json

    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            pr = PR()
            mock_gated_reads(router, pr)
            merge = router.put(f"{GH}/pulls/1/merge").mock(
                return_value=httpx.Response(200, json={"merged": True, "sha": SHA3,
                                                       "message": "Pull Request merged"})
            )  # fmt: skip
            # the expected head sha must match at request time
            r = await e.send("admin", "POST", f"{R}/pulls/1/merge",
                             json={"expected_head_sha": SHA2})  # fmt: skip
            assert r.status_code == 409 and r.json()["detail"]["code"] == "head_sha_mismatch"
            aid = await _merge_request(e)
            assert merge.call_count == 0
            d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
            expected = hashlib.sha256(
                _json.dumps(d["payload"]["artifact"], sort_keys=True,
                            separators=(",", ":")).encode()
            ).hexdigest()  # fmt: skip
            assert d["artifact_sha256"] == expected
            assert d["payload"]["artifact"]["head_sha"] == SHA1
            assert d["payload"]["artifact"]["diff_sha256"] == hashlib.sha256(pr.diff).hexdigest()

            r = await approve(e, aid)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "executed" and r.json()["result"]["merged"] is True
            assert merge.call_count == 1
            assert _json.loads(merge.calls[0].request.content) == {
                "sha": SHA1, "merge_method": "squash"}  # fmt: skip
            # single use
            r = await approve(e, aid)
            assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_already_used"
            assert merge.call_count == 1
        rows = await ops_rows(e)
        assert [(o, s) for o, s, *_ in rows] == [("pr.merge", "succeeded")]
        events = await e.sql(
            "SELECT event FROM audit.approval_events WHERE request_id = :i ORDER BY id", i=aid
        )
        assert [x[0] for x in events] == ["requested", "approved", "executed"]
        assert await audit_count(e, "approval_executed") == 1
        r = await e.get("admin", "/api/v1/approvals", params={"status": "executed"})
        assert r.json()["total"] == 1


async def test_expired_approval_is_409(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            merge = router.put(f"{GH}/pulls/1/merge").mock(return_value=httpx.Response(200))
            aid = await _merge_request(e)
            await e.sql(
                "UPDATE audit.approval_requests SET expires_at = now() - interval '1 minute' "
                "WHERE id = :i",
                i=aid,
            )
            r = await approve(e, aid)
            assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_expired"
            assert merge.call_count == 0 and writes(router) == []
        d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
        assert d["status"] == "expired"
        assert [(o, s) for o, s, *_ in await ops_rows(e)] == [("pr.merge", "expired")]


async def test_listing_expires_due_requests(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            aid = await _merge_request(e)
        await e.sql("UPDATE audit.approval_requests SET expires_at = now() - interval '1 second'")
        r = await e.get("admin", "/api/v1/approvals", params={"status": "pending"})
        assert r.json()["total"] == 0
        r = await e.get("admin", "/api/v1/approvals", params={"status": "expired"})
        assert [x["id"] for x in r.json()["items"]] == [aid]


async def test_head_drift_invalidates_and_sends_nothing(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            pr = PR()
            mock_gated_reads(router, pr)
            merge = router.put(f"{GH}/pulls/1/merge").mock(return_value=httpx.Response(200))
            aid = await _merge_request(e)
            pr.head = SHA2  # someone pushed after the request
            r = await approve(e, aid)
            assert r.status_code == 409
            assert r.json()["detail"]["code"] == "approval_invalidated"
            assert merge.call_count == 0 and writes(router) == []
            r = await approve(e, aid)  # an invalidated request cannot be retried
            assert r.status_code == 409 and r.json()["detail"]["code"] == "approval_not_pending"
        d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
        assert d["status"] == "invalidated" and d["result"] == {"reason": "artifact_drift"}
        assert [(o, s) for o, s, *_ in await ops_rows(e)] == [("pr.merge", "invalidated")]


async def test_diff_drift_invalidates(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            pr = PR()
            mock_gated_reads(router, pr)
            aid = await _merge_request(e)
            pr.diff = b"diff --git a/x b/x\n+2\n"
            r = await approve(e, aid)
            assert r.status_code == 409 and writes(router) == []


async def test_wrong_password_is_403_and_audited(env: OpsEnv) -> None:
    async with env(APPROVALS_MAX_REAUTH_FAILURES=3) as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            merge = router.put(f"{GH}/pulls/1/merge").mock(return_value=httpx.Response(200))
            aid = await _merge_request(e)
            r = await approve(e, aid, "wrong-password!!")
            assert r.status_code == 403 and r.json()["detail"]["code"] == "reauth_failed"
            assert "wrong-password" not in r.text
            d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
            assert d["status"] == "pending"
            assert [ev["event"] for ev in d["events"]] == ["requested", "reauth_failed"]
            assert await audit_count(e, "approval_reauth_failed") == 1
            # a session cookie alone is not enough: no password -> 422, nothing runs
            r = await e.send("admin", "POST", f"/api/v1/approvals/{aid}/approve", json={})
            assert r.status_code == 422
            # the 3rd failure denies the request
            for _ in range(2):
                assert (await approve(e, aid, "nope-nope-nope")).status_code == 403
            d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
            assert d["status"] == "denied"
            assert (await approve(e, aid)).status_code == 409
            assert merge.call_count == 0
        rows = await e.sql("SELECT details::text FROM audit.audit_events")
        assert all("nope-nope" not in x[0] and PASSWORD not in x[0] for x in rows)


async def test_deny_and_non_admin(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            aid = await _merge_request(e)
            assert (await e.get("a", "/api/v1/approvals")).status_code == 403
            assert (await e.get("a", f"/api/v1/approvals/{aid}")).status_code == 403
            assert (await approve(e, aid, PASSWORD, who="a")).status_code == 403
            r = await e.send("a", "POST", f"/api/v1/approvals/{aid}/deny")
            assert r.status_code == 403
            # CSRF is required for approve
            r = await e.clients["admin"].post(
                f"/api/v1/approvals/{aid}/approve", json={"password": PASSWORD}
            )
            assert r.status_code == 403
            r = await e.send("admin", "POST", f"/api/v1/approvals/{aid}/deny",
                             json={"reason": "not now"})  # fmt: skip
            assert r.status_code == 200 and r.json()["status"] == "denied"
            assert (await approve(e, aid)).status_code == 409
            r = await e.send("admin", "POST", f"/api/v1/approvals/{aid}/deny")
            assert r.status_code == 409
            assert writes(router) == []
            missing = "00000000-0000-0000-0000-000000000000"
            assert (await e.get("admin", f"/api/v1/approvals/{missing}")).status_code == 404
            assert (await approve(e, missing)).status_code == 404
        assert [(o, s) for o, s, *_ in await ops_rows(e)] == [("pr.merge", "denied")]


async def test_allowlist_404_without_github_calls(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            for method, url, body, _op in GATED:
                other = url.replace("/repositories/me/app", "/repositories/other/app")
                r = await e.send("admin", method, other, json=body)
                assert r.status_code == 404, (method, other)
            for path in ("/issues", "/issues/1", "/pulls", "/pulls/1/diff"):
                r = await e.get("admin", f"/api/v1/github/repositories/other/app{path}")
                assert r.status_code == 404
            r = await e.send("admin", "POST", "/api/v1/github/repositories/other/app/issues",
                             json={"title": "x"})  # fmt: skip
            assert r.status_code == 404
            assert router.calls.call_count == 0


async def test_executor_rechecks_allowlist(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            aid = await _merge_request(e)
            # the repo is dropped from the allowlist between request and approval
            e.app.state.settings = e.app.state.settings.model_copy(
                update={"GITHUB_ALLOWED_OWNERS": ["someone-else"]}
            )
            r = await approve(e, aid)
            assert r.status_code == 200 and r.json()["status"] == "failed"
            assert "not_found" in r.json()["result"]["error"]
            assert writes(router) == []


async def test_execution_failure_is_recorded(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            router.put(f"{GH}/pulls/1/merge").mock(
                return_value=httpx.Response(405, json={"message": f"not mergeable {TOKEN}"})
            )
            aid = await _merge_request(e)
            r = await approve(e, aid)
            assert r.status_code == 200 and r.json()["status"] == "failed"
            assert "github_conflict" in r.json()["result"]["error"] and TOKEN not in r.text
        rows = await ops_rows(e)
        assert rows[0][1] == "failed" and TOKEN not in rows[0][4]


@pytest.mark.parametrize(("method", "url", "body", "op"), GATED[1:], ids=[g[3] for g in GATED[1:]])
async def test_each_gated_kind_executes_after_approval(
    env: OpsEnv, method: str, url: str, body: dict[str, Any] | None, op: str
) -> None:
    import json as _json

    expected = {
        "ref.force_update": ("PATCH", f"{GH}/git/refs/heads/feature",
                             {"sha": SHA2, "force": True}),
        "branch_protection.update": ("PUT", f"{GH}/branches/main/protection", PROTECTION),
        "branch_protection.delete": ("DELETE", f"{GH}/branches/main/protection", None),
        "branch.delete": ("DELETE", f"{GH}/git/refs/heads/feature", None),
        "repo.update_settings": ("PATCH", GH,
                                 {"visibility": "public", "allow_squash_merge": False}),
        "actions.set_permissions": ("PUT", f"{GH}/actions/permissions", {"enabled": False}),
    }[op]  # fmt: skip
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            route = router.route(method=expected[0], path=expected[1]).mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            r = await e.send("admin", method, url, json=body)
            assert r.status_code == 202
            assert route.call_count == 0
            r = await approve(e, r.json()["approval_id"])
            assert r.status_code == 200 and r.json()["status"] == "executed", r.text
            assert route.call_count == 1 and len(writes(router)) == 1
            sent = route.calls[0].request.content
            assert (_json.loads(sent) if sent else None) == expected[2]
        assert [(o, s) for o, s, *_ in await ops_rows(e)] == [(op, "succeeded")]


async def test_force_push_unknown_sha_is_rejected(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            router.get(f"{GH}/git/commits/{SHA3}").mock(return_value=httpx.Response(404))
            r = await e.send("admin", "POST", f"{R}/force-push",
                             json={"branch": "feature", "sha": SHA3})  # fmt: skip
            assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_sha"
            r = await e.send("admin", "POST", f"{R}/force-push",
                             json={"branch": "feature", "sha": "abc"})  # fmt: skip
            assert r.status_code == 422
        assert await ops_rows(e) == []


async def _second_admin(e: Env) -> None:
    async with e.maker() as db, db.begin():
        role = await db.scalar(select(Role).where(Role.name == ROLE_ADMIN))
        assert role is not None
        db.add(User(email="b@spasta.online", password_hash=hash_password(PASSWORD),
                    role_id=role.id))  # fmt: skip


async def test_self_approval_rules(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            merge = router.put(f"{GH}/pulls/1/merge").mock(return_value=httpx.Response(200))
            aid = await _merge_request(e)
            await _second_admin(e)  # no longer the only admin
            r = await approve(e, aid)
            assert r.status_code == 403
            assert r.json()["detail"]["code"] == "self_approval_not_allowed"
            assert merge.call_count == 0
            d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
            assert d["status"] == "pending"
            assert d["events"][-1]["event"] == "self_approval_refused"
            # the other admin can approve
            b = e.clients["a"]
            r = await b.post("/api/auth/login",
                             json={"email": "b@spasta.online", "password": PASSWORD})  # fmt: skip
            e.csrf["a"] = r.json()["csrf_token"]
            r = await approve(e, aid, who="a")
            assert r.status_code == 200 and r.json()["status"] == "executed"
            assert merge.call_count == 1


async def test_self_approval_can_be_disabled(env: OpsEnv) -> None:
    async with env(APPROVALS_ALLOW_SELF_APPROVAL=False) as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            aid = await _merge_request(e)
            r = await approve(e, aid)
            assert r.status_code == 403 and writes(router) == []


async def test_gated_without_token_is_503_and_approval_not_consumed(env: OpsEnv) -> None:
    async with env() as e:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_gated_reads(router)
            aid = await _merge_request(e)
            client = e.app.state.github
            e.app.state.github = None
            try:
                r = await approve(e, aid)
            finally:
                e.app.state.github = client
            assert r.status_code == 503
            assert r.json()["detail"]["code"] == "github_not_configured"
            d = (await e.get("admin", f"/api/v1/approvals/{aid}")).json()
            assert d["status"] == "pending"
