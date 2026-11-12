"""Phase 16 units (docs/19-github-issues-prs.md): client write semantics, raw/capped bodies,
artifact hashing, the executor registry and input helpers. No DB."""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest
import respx

from app.core.config import Settings
from app.integrations.github.client import GitHubClient, GitHubError, GitHubUnavailable
from app.services import github as gh
from app.services import github_ops as ops
from app.services.approvals import REGISTRY, ExecutorRegistry, ExecutorSpec, artifact_sha256

API = "https://api.github.test"
TOKEN = "github_pat_" + "A1b2C3d4" * 10


async def _noop(_s: float) -> None:
    return None


def make(**kw: Any) -> GitHubClient:
    kw.setdefault("cache_ttl", 60)
    return GitHubClient(TOKEN, API, sleep=_noop, backoff_base=0, **kw)


async def test_post_is_never_retried_after_a_5xx() -> None:
    c = make()
    with respx.mock(base_url=API) as router:
        route = router.post("/repos/me/app/issues").mock(return_value=httpx.Response(502))
        with pytest.raises(GitHubUnavailable):
            await c.request("POST", "/repos/me/app/issues", json={"title": "x"})
    assert route.call_count == 1
    await c.aclose()


async def test_post_is_never_retried_after_a_network_error() -> None:
    c = make()
    with respx.mock(base_url=API) as router:
        route = router.post("/repos/me/app/issues").mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(GitHubUnavailable):
            await c.request("POST", "/repos/me/app/issues", json={"title": "x"})
    assert route.call_count == 1
    await c.aclose()


async def test_put_is_retried_after_a_5xx() -> None:
    c = make()
    with respx.mock(base_url=API) as router:
        route = router.put("/repos/me/app/issues/1/lock").mock(
            side_effect=[httpx.Response(502), httpx.Response(204)]
        )
        resp = await c.request("PUT", "/repos/me/app/issues/1/lock", json={})
    assert resp.status == 204 and route.call_count == 2
    await c.aclose()


async def test_a_write_clears_the_get_cache() -> None:
    c = make()
    with respx.mock(base_url=API) as router:
        get = router.get("/repos/me/app/issues/1").mock(
            return_value=httpx.Response(200, json={"number": 1})
        )
        router.patch("/repos/me/app/issues/1").mock(return_value=httpx.Response(200, json={}))
        await c.get_json("/repos/me/app/issues/1")
        await c.get_json("/repos/me/app/issues/1")
        assert get.call_count == 1  # cached
        await c.request("PATCH", "/repos/me/app/issues/1", json={"title": "y"})
        await c.get_json("/repos/me/app/issues/1")
    assert get.call_count == 2
    await c.aclose()


async def test_raw_capped_body_and_accept_header() -> None:
    c = make()
    body = b"diff --git a/x b/x\n" + b"+" * 5000
    with respx.mock(base_url=API) as router:
        route = router.get("/repos/me/app/pulls/1").mock(
            return_value=httpx.Response(200, content=body)
        )
        resp = await c.request(
            "GET",
            "/repos/me/app/pulls/1",
            accept=ops.DIFF_ACCEPT,
            raw=True,
            max_bytes=1000,
        )
    assert route.calls[0].request.headers["accept"] == ops.DIFF_ACCEPT
    assert isinstance(resp.data, bytes) and len(resp.data) == 1001  # cap + 1: "was cut"
    await c.aclose()


async def test_405_maps_to_conflict() -> None:
    c = make()
    with respx.mock(base_url=API) as router:
        router.put("/repos/me/app/pulls/1/merge").mock(
            return_value=httpx.Response(405, json={"message": "Pull Request is not mergeable"})
        )
        with pytest.raises(GitHubError) as ei:
            await c.request("PUT", "/repos/me/app/pulls/1/merge", json={})
    assert ei.value.status == 409 and ei.value.code == "github_conflict"
    await c.aclose()


def test_artifact_sha256_is_canonical() -> None:
    assert artifact_sha256(None) is None
    assert artifact_sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()
    assert artifact_sha256("abc") == hashlib.sha256(b"abc").hexdigest()
    assert artifact_sha256({"b": 1, "a": [1, 2]}) == artifact_sha256({"a": [1, 2], "b": 1})
    assert artifact_sha256({"a": 1}) != artifact_sha256({"a": 2})


def test_github_kinds_are_registered() -> None:
    assert set(ops.KIND_OPS) <= set(REGISTRY.kinds())
    spec = REGISTRY.get("github.pr.merge")
    assert spec is not None and spec.requires == ("github",) and spec.current_artifact


async def test_registry_replaces_by_kind() -> None:
    reg = ExecutorRegistry()

    async def ex(_ctx: Any, _req: Any) -> dict[str, Any]:
        return {}

    reg.register(ExecutorSpec("k", ex))
    reg.register(ExecutorSpec("k", ex, requires=("x",)))
    assert reg.kinds() == ["k"] and reg.get("k").requires == ("x",)  # type: ignore[union-attr]


def test_head_and_branch_checks() -> None:
    assert ops.check_head("feature/x") == "feature/x"
    assert ops.check_head("someone:feature/x") == "someone:feature/x"
    for bad in ("../x", "-x", "a..b", "bad owner!:x"):
        with pytest.raises(gh.InvalidInput):
            ops.check_head(bad)
    with pytest.raises(gh.InvalidInput):
        ops.check_branch("refs/../x")


def test_summarize_and_safe_payload() -> None:
    c = make()
    s = ops.summarize(201, {"id": 5, "number": 3, "body": "x", "object": {"sha": "a" * 40}})
    assert s == {"status": 201, "id": 5, "number": 3, "sha": "a" * 40}
    safe = ops.safe_payload(c, {"body": f"token {TOKEN} " + "y" * 9000})
    assert TOKEN not in safe["body"] and len(safe["body"]) <= ops.MAX_PAYLOAD_TEXT + 1


def test_settings_defaults() -> None:
    s = Settings(
        ENV="test",
        DATABASE_URL="postgresql+asyncpg://x:x@127.0.0.1:1/none",
        SECRET_KEY="t" * 40,
    )
    assert s.GITHUB_DIFF_MAX_BYTES == 2 * 1024 * 1024
    assert s.APPROVALS_TTL_MINUTES == 30 and s.APPROVALS_ALLOW_SELF_APPROVAL is True
