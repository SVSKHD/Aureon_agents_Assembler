"""GitHub foundation endpoints against the DB, with GitHub mocked by respx."""

from __future__ import annotations

import base64
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import respx
import structlog
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.bootstrap import bootstrap_admin
from app.auth.security import hash_password
from app.core.config import Settings
from app.integrations.github import client as gh_client
from app.main import create_app
from app.models.auth import User
from app.workers.scheduler import TaskContext, github_sync
from tests.test_finance_db import PASSWORD, Env

pytestmark = pytest.mark.db

API = "https://api.github.test"
TOKEN = "github_pat_" + "Q7w8E9r0" * 10
BASE = "/api/v1/github"

EnvFactory = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


def repo_json(rid: int, full: str, **kw: Any) -> dict[str, Any]:
    owner, name = full.split("/")
    return {
        "id": rid,
        "full_name": full,
        "name": name,
        "owner": {"login": owner},
        "private": True,
        "default_branch": "main",
        "archived": False,
        "pushed_at": "2026-10-01T10:00:00Z",
        "updated_at": "2026-10-01T10:00:00Z",
        "html_url": f"https://github.com/{full}",
        "description": None,
        "permissions": {"admin": True, "push": True, "pull": True},
        **kw,
    }


def rl(remaining: int = 4900) -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-used": str(5000 - remaining),
        "x-ratelimit-reset": str(int(time.time()) + 900),
        "x-ratelimit-resource": "core",
    }


@pytest.fixture
def gh_env(db_clean: str, settings: Settings) -> EnvFactory:
    @contextlib.asynccontextmanager
    async def factory(configured: bool = True, **overrides: Any) -> AsyncIterator[Env]:
        engine = create_async_engine(db_clean)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as db, db.begin():
            await db.execute(
                text("TRUNCATE github.connections, github.repositories, github.sync_runs")
            )
        admin = await bootstrap_admin(maker, "admin@spasta.online", PASSWORD)
        ids: dict[str, Any] = {"admin": admin.id}
        async with maker() as db, db.begin():
            u = User(email="a@spasta.online", password_hash=hash_password(PASSWORD), role_id=2)
            db.add(u)
            await db.flush()
            ids["a"] = u.id
        await engine.dispose()
        update: dict[str, Any] = {
            "GITHUB_TOKEN": SecretStr(TOKEN) if configured else None,
            "GITHUB_API_URL": API,
            "GITHUB_RETRY_BACKOFF_SECONDS": 0.0,
            "GITHUB_CACHE_TTL_SECONDS": 0.0,
            **overrides,
        }
        app = create_app(settings.model_copy(update=update))
        async with LifespanManager(app) as mgr:
            t = ASGITransport(app=mgr.app)
            clients = {w: AsyncClient(transport=t, base_url="https://test") for w in ("admin", "a")}
            csrf = {}
            for who, c in clients.items():
                email = "admin@spasta.online" if who == "admin" else f"{who}@spasta.online"
                r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
                csrf[who] = r.json()["csrf_token"]
            try:
                yield Env(app, clients, csrf, ids)
            finally:
                for c in clients.values():
                    await c.aclose()

    return factory


def mock_user(router: respx.MockRouter, login: str = "me") -> respx.Route:
    return router.get("/user").mock(
        return_value=httpx.Response(
            200,
            json={"login": login, "id": 42},
            headers={**rl(), "x-oauth-scopes": "repo, workflow"},
        )
    )


REPO_ENDPOINTS = [
    "/repositories",
    "/repositories/me/app",
    "/repositories/me/app/branches",
    "/repositories/me/app/commits",
    "/repositories/me/app/tree",
    "/repositories/me/app/contents/README.md",
]


async def test_not_configured(gh_env: EnvFactory) -> None:
    async with gh_env(configured=False) as env:
        r = await env.get("admin", f"{BASE}/connection")
        assert r.status_code == 200
        body = r.json()
        assert body["configured"] is False and body["login"] is None
        assert body["fingerprint"] is None
        for path in REPO_ENDPOINTS:
            r = await env.get("admin", BASE + path)
            assert r.status_code == 503, path
            assert r.json()["detail"]["code"] == "github_not_configured"
        r = await env.send("admin", "POST", f"{BASE}/connection/check")
        assert r.status_code == 503 and r.json()["detail"]["code"] == "github_not_configured"


async def test_non_admin_forbidden_and_anonymous_unauthorized(gh_env: EnvFactory) -> None:
    async with gh_env() as env:
        for path in ["/connection", *REPO_ENDPOINTS]:
            assert (await env.get("a", BASE + path)).status_code == 403, path
        assert (await env.send("a", "POST", f"{BASE}/connection/check")).status_code == 403
        anon = AsyncClient(transport=ASGITransport(app=env.app), base_url="https://test")
        assert (await anon.get(f"{BASE}/connection")).status_code == 401
        await anon.aclose()


async def test_connection_check_and_health(gh_env: EnvFactory) -> None:
    async with gh_env() as env:
        with respx.mock(base_url=API) as router:
            mock_user(router)
            # CSRF is required for the check.
            r = await env.clients["admin"].post(f"{BASE}/connection/check")
            assert r.status_code == 403
            r = await env.send("admin", "POST", f"{BASE}/connection/check")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["configured"] and body["ok"] and body["login"] == "me"
        assert body["scopes"] == ["repo", "workflow"]
        assert body["token_kind"] == "fine_grained" and len(body["fingerprint"]) == 16
        assert body["rate_limit"]["remaining"] == 4900
        assert TOKEN not in r.text
        r = await env.get("admin", f"{BASE}/connection")
        assert r.json()["login"] == "me" and r.json()["last_checked_at"] and TOKEN not in r.text
        rows = await env.sql("SELECT fingerprint, login, ok FROM github.connections")
        assert rows == [(body["fingerprint"], "me", True)]
        audit = await env.sql(
            "SELECT count(*) FROM audit.audit_events WHERE event_type='github_connection_checked'"
        )
        assert audit[0][0] == 1


async def test_connection_check_failure_is_recorded_and_redacted(gh_env: EnvFactory) -> None:
    async with gh_env() as env:
        with respx.mock(base_url=API) as router:
            router.get("/user").mock(
                return_value=httpx.Response(401, json={"message": f"Bad credentials {TOKEN}"})
            )
            r = await env.send("admin", "POST", f"{BASE}/connection/check")
        assert r.status_code == 200
        assert r.json()["ok"] is False and "github_auth_failed" in r.json()["detail"]
        assert TOKEN not in r.text
        rows = await env.sql("SELECT detail FROM github.connections")
        assert TOKEN not in rows[0][0]


def _repos_route(router: respx.MockRouter, pages: list[list[dict[str, Any]]]) -> respx.Route:
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("page", "1"))
        headers = rl()
        if n < len(pages):
            headers["link"] = f'<{API}/user/repos?per_page=100&page={n + 1}>; rel="next"'
        return httpx.Response(200, json=pages[n - 1], headers=headers)

    return router.get("/user/repos").mock(side_effect=handler)


async def test_repositories_sync_list_and_removal(gh_env: EnvFactory) -> None:
    async with gh_env() as env:
        with respx.mock(base_url=API) as router:
            mock_user(router)
            _repos_route(
                router,
                [
                    [repo_json(1, "me/app"), repo_json(2, "someorg/shared")],
                    [repo_json(3, "me/site", private=False, archived=True)],
                ],
            )
            r = await env.get("admin", f"{BASE}/repositories?refresh=true")
        assert r.status_code == 200, r.text
        page = r.json()
        # someorg/shared is not the token user's own repo: filtered by the default allowlist.
        assert page["total"] == 2
        assert {i["full_name"] for i in page["items"]} == {"me/app", "me/site"}
        site = next(i for i in page["items"] if i["full_name"] == "me/site")
        assert site["archived"] is True and site["private"] is False
        runs = await env.sql(
            "SELECT trigger, status, repos_seen, repos_upserted FROM github.sync_runs"
        )
        assert runs == [("api", "succeeded", 3, 2)]

        # Served from the table (no GitHub call) with paging and a name filter.
        r = await env.get("admin", f"{BASE}/repositories?limit=1&q=site")
        assert r.json()["total"] == 1 and r.json()["items"][0]["full_name"] == "me/site"

        # me/site disappears from GitHub: marked removed, row kept.
        with respx.mock(base_url=API) as router:
            _repos_route(router, [[repo_json(1, "me/app")]])
            r = await env.get("admin", f"{BASE}/repositories?refresh=true")
        assert [i["full_name"] for i in r.json()["items"]] == ["me/app"]
        rows = await env.sql(
            "SELECT full_name, removed_at IS NOT NULL FROM github.repositories ORDER BY id"
        )
        assert rows == [("me/app", False), ("me/site", True)]
        conn = (await env.get("admin", f"{BASE}/connection")).json()
        assert conn["last_sync"]["repos_removed"] == 1


async def test_refresh_reuses_a_sync_already_running(gh_env: EnvFactory) -> None:
    """A retried GET ?refresh=true (client timed out during a slow sync) must not start a
    second sync; a stale 'running' row from a crashed process must not block forever."""
    async with gh_env() as env:
        await env.sql("INSERT INTO github.sync_runs (trigger, status) VALUES ('api', 'running')")
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_user(router)
            listed = _repos_route(router, [[repo_json(1, "me/app")]])
            r = await env.get("admin", f"{BASE}/repositories?refresh=true")
            assert r.status_code == 200, r.text
            assert listed.call_count == 0
            assert await env.sql("SELECT count(*) FROM github.sync_runs") == [(1,)]

            await env.sql("UPDATE github.sync_runs SET started_at = now() - interval '10 minutes'")
            r = await env.get("admin", f"{BASE}/repositories?refresh=true")
            assert r.status_code == 200, r.text
            assert listed.call_count == 1
            assert [i["full_name"] for i in r.json()["items"]] == ["me/app"]


async def test_allowlisted_owners_and_repos(gh_env: EnvFactory) -> None:
    async with gh_env(
        GITHUB_ALLOWED_OWNERS=["acme"], GITHUB_ALLOWED_REPOS=["acme/api", "acme/extra"]
    ) as env:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_user(router)
            _repos_route(router, [[repo_json(1, "acme/api"), repo_json(2, "acme/web")]])
            router.get("/repos/acme/extra").mock(
                return_value=httpx.Response(200, json=repo_json(9, "acme/extra"))
            )
            outside = router.get("/repos/acme/web").mock(return_value=httpx.Response(200))
            r = await env.get("admin", f"{BASE}/repositories?refresh=true")
            assert {i["full_name"] for i in r.json()["items"]} == {"acme/api", "acme/extra"}
            # A repo outside the allowlist is a 404, and GitHub is never asked.
            for path in ("", "/branches", "/commits", "/tree", "/contents/README.md"):
                r = await env.get("admin", f"{BASE}/repositories/acme/web{path}")
                assert r.status_code == 404, path
            r = await env.get("admin", f"{BASE}/repositories/me/app")
            assert r.status_code == 404
            assert outside.call_count == 0


async def test_default_allowlist_is_own_repos(gh_env: EnvFactory) -> None:
    async with gh_env() as env:
        with respx.mock(base_url=API, assert_all_called=False) as router:
            mock_user(router, login="me")
            other = router.get("/repos/other/x").mock(return_value=httpx.Response(200))
            r = await env.get("admin", f"{BASE}/repositories/other/x")
            assert r.status_code == 404 and other.call_count == 0
            r = await env.get("admin", f"{BASE}/repositories/bad name/x")
            assert r.status_code in (400, 404)


async def test_repo_detail_branches_commits_tree(gh_env: EnvFactory) -> None:
    async with gh_env(GITHUB_ALLOWED_OWNERS=["me"]) as env:
        with respx.mock(base_url=API) as router:
            router.get("/repos/me/app").mock(
                return_value=httpx.Response(200, json=repo_json(1, "me/app", language="Python"))
            )

            def branches(request: httpx.Request) -> httpx.Response:
                n = int(request.url.params.get("page", "1"))
                headers = {}
                if n == 1:
                    headers["link"] = (
                        f'<{API}/repos/me/app/branches?per_page=100&page=2>; rel="next"'
                    )
                return httpx.Response(
                    200,
                    json=[{"name": f"b{n}", "commit": {"sha": f"s{n}"}, "protected": n == 1}],
                    headers=headers,
                )

            router.get("/repos/me/app/branches").mock(side_effect=branches)
            commits = router.get("/repos/me/app/commits").mock(
                return_value=httpx.Response(
                    200,
                    json=[
                        {
                            "sha": "abc",
                            "html_url": "https://github.com/me/app/commit/abc",
                            "author": {"login": "me"},
                            "commit": {
                                "message": "fix: thing",
                                "author": {"name": "Me", "date": "2026-10-01T10:00:00Z"},
                                "committer": {"name": "Me", "date": "2026-10-01T10:00:00Z"},
                            },
                        }
                    ],
                )
            )
            trees = router.get(path__regex=r"^/repos/me/app/git/trees/.+").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "sha": "t1",
                        "truncated": False,
                        "tree": [
                            {"path": "a.py", "type": "blob", "mode": "100644", "size": 3},
                            {"path": "src", "type": "tree", "mode": "040000"},
                        ],
                    },
                )
            )

            r = await env.get("admin", f"{BASE}/repositories/me/app")
            assert r.status_code == 200 and r.json()["language"] == "Python"
            assert r.json()["permissions"]["admin"] is True

            r = await env.get("admin", f"{BASE}/repositories/me/app/branches")
            assert [(b["name"], b["sha"], b["protected"]) for b in r.json()] == [
                ("b1", "s1", True),
                ("b2", "s2", False),
            ]

            url = f"{BASE}/repositories/me/app/commits?sha=dev&path=src/a.py&per_page=5"
            r = await env.get("admin", url)
            assert r.status_code == 200 and r.json()[0]["sha"] == "abc"
            q = commits.calls.last.request.url.params
            assert (q["sha"], q["path"], q["per_page"]) == ("dev", "src/a.py", "5")
            r = await env.get("admin", f"{BASE}/repositories/me/app/commits?per_page=500")
            assert r.status_code == 422
            r = await env.get("admin", f"{BASE}/repositories/me/app/commits?sha=../x")
            assert r.status_code == 400

            r = await env.get("admin", f"{BASE}/repositories/me/app/tree?recursive=true")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ref"] == "main" and len(body["entries"]) == 2 and not body["truncated"]
            assert trees.calls.last.request.url.params["recursive"] == "1"
            r = await env.get("admin", f"{BASE}/repositories/me/app/tree?ref=dev&path=src")
            assert r.status_code == 200
            assert trees.calls.last.request.url.path == "/repos/me/app/git/trees/dev:src"
            r = await env.get("admin", f"{BASE}/repositories/me/app/tree?path=../etc")
            assert r.status_code == 400


async def test_contents_text_binary_large_dir_and_traversal(gh_env: EnvFactory) -> None:
    async with gh_env(GITHUB_ALLOWED_OWNERS=["me"], GITHUB_MAX_FILE_BYTES=1024) as env:
        with respx.mock(base_url=API, assert_all_called=False) as router:

            def file(path: str, raw: bytes, size: int | None = None) -> dict[str, Any]:
                return {
                    "type": "file",
                    "name": path.rsplit("/", 1)[-1],
                    "path": path,
                    "size": len(raw) if size is None else size,
                    "sha": "x",
                    "encoding": "base64",
                    "content": base64.encodebytes(raw).decode(),
                }

            router.get("/repos/me/app/contents/src/main.py").mock(
                return_value=httpx.Response(200, json=file("src/main.py", "print('hé')\n".encode()))
            )
            router.get("/repos/me/app/contents/logo.png").mock(
                return_value=httpx.Response(200, json=file("logo.png", b"\x89PNG\x00\x01\x02"))
            )
            router.get("/repos/me/app/contents/big.txt").mock(
                return_value=httpx.Response(200, json=file("big.txt", b"a" * 2000))
            )
            router.get("/repos/me/app/contents/huge.bin").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "type": "file",
                        "name": "huge.bin",
                        "path": "huge.bin",
                        "size": 5_000_000,
                        "encoding": "none",
                        "content": "",
                    },
                )
            )
            router.get("/repos/me/app/contents/src").mock(
                return_value=httpx.Response(
                    200,
                    json=[{"name": "main.py", "path": "src/main.py", "type": "file", "size": 9}],
                )
            )
            router.get("/repos/me/app/contents/nope").mock(
                return_value=httpx.Response(404, json={"message": "Not Found"})
            )
            any_call = router.route(path__regex=r".*\.\..*")

            r = await env.get("admin", f"{BASE}/repositories/me/app/contents/src/main.py?ref=dev")
            assert r.status_code == 200, r.text
            assert r.json()["content_kind"] == "text" and r.json()["text"] == "print('hé')\n"

            r = await env.get("admin", f"{BASE}/repositories/me/app/contents/logo.png")
            assert r.json()["content_kind"] == "binary" and r.json()["text"] is None

            r = await env.get("admin", f"{BASE}/repositories/me/app/contents/big.txt")
            assert r.json()["content_kind"] == "too_large" and r.json()["text"] is None
            r = await env.get("admin", f"{BASE}/repositories/me/app/contents/huge.bin")
            assert r.json()["content_kind"] == "too_large" and r.json()["size"] == 5_000_000

            r = await env.get("admin", f"{BASE}/repositories/me/app/contents/src")
            assert r.json()["type"] == "dir" and r.json()["entries"][0]["name"] == "main.py"

            r = await env.get("admin", f"{BASE}/repositories/me/app/contents/nope")
            assert r.status_code == 404

            for bad in ("a/%2e%2e/%2e%2e/etc/passwd", "%2e%2e", "a/%252e%252e/b", "a/%2e/b",
                        "a%5cb", "a//b"):  # fmt: skip
                r = await env.get("admin", f"{BASE}/repositories/me/app/contents/{bad}")
                assert r.status_code == 400, (bad, r.text)
                assert r.json()["detail"]["code"] == "invalid_path"
            assert any_call.call_count == 0


async def test_upstream_error_with_token_is_redacted_in_response_and_logs(
    gh_env: EnvFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = structlog.testing.LogCapture()
    monkeypatch.setattr(gh_client, "log", structlog.wrap_logger(None, processors=[cap]))
    async with gh_env(GITHUB_ALLOWED_OWNERS=["me"]) as env:
        with respx.mock(base_url=API) as router:
            route = router.get("/repos/me/app/branches").mock(
                return_value=httpx.Response(
                    500, text=f"internal error, token={TOKEN} Authorization: token {TOKEN}"
                )
            )
            r = await env.get("admin", f"{BASE}/repositories/me/app/branches")
        assert r.status_code == 502 and r.json()["detail"]["code"] == "github_unavailable"
        assert route.call_count == 3  # retried, then gave up
        assert TOKEN not in r.text and "[REDACTED]" in r.text
        # What the client hands to structlog carries the error, redacted.
        events = [e["event"] for e in cap.entries]
        assert events.count("github_retry") == 2 and "github_request_error" in events
        assert "[REDACTED]" in repr(cap.entries) and TOKEN not in repr(cap.entries)


async def test_rate_limited_upstream_returns_429(gh_env: EnvFactory) -> None:
    async with gh_env(GITHUB_ALLOWED_OWNERS=["me"]) as env:
        with respx.mock(base_url=API) as router:
            router.get("/repos/me/app").mock(
                return_value=httpx.Response(
                    429, headers={"retry-after": "120"}, json={"message": "slow"}
                )
            )
            r = await env.get("admin", f"{BASE}/repositories/me/app")
        assert r.status_code == 429 and r.headers["retry-after"] == "120"
        assert r.json()["detail"]["code"] == "github_rate_limited"


async def test_rate_limit_floor_refusal_via_api(gh_env: EnvFactory) -> None:
    async with gh_env(GITHUB_ALLOWED_OWNERS=["me"], GITHUB_RATE_LIMIT_FLOOR=100) as env:
        with respx.mock(base_url=API) as router:
            route = router.get("/repos/me/app").mock(
                return_value=httpx.Response(200, json=repo_json(1, "me/app"), headers=rl(20))
            )
            assert (await env.get("admin", f"{BASE}/repositories/me/app")).status_code == 200
            r = await env.get("admin", f"{BASE}/repositories/me/app")
            assert r.status_code == 429
            assert r.json()["detail"]["code"] == "github_rate_limit_floor"
            assert int(r.headers["retry-after"]) > 0
            assert route.call_count == 1
        conn = (await env.get("admin", f"{BASE}/connection")).json()
        assert conn["rate_limit"]["remaining"] == 20


async def test_scheduler_github_sync_heartbeats_agent(gh_env: EnvFactory) -> None:
    async with gh_env() as env:
        ctx = TaskContext(env.app.state.settings, env.maker, None, github=env.app.state.github)
        with respx.mock(base_url=API) as router:
            mock_user(router)
            _repos_route(router, [[repo_json(1, "me/app")]])
            out = await github_sync(ctx)
        assert out["ok"] and out["repos_upserted"] == 1
        agent = await env.sql("SELECT kind, state, details FROM agents.agents WHERE id='github'")
        kind, state, details = agent[0]
        assert (kind, state) == ("integration", "idle")
        assert details["login"] == "me" and details["ok"] is True and TOKEN not in str(details)
        runs = await env.sql("SELECT trigger, status FROM github.sync_runs")
        assert runs == [("scheduler", "succeeded")]

        with respx.mock(base_url=API) as router:
            router.get("/user").mock(return_value=httpx.Response(401, json={"message": "bad"}))
            with pytest.raises(RuntimeError, match="github_auth_failed"):
                await github_sync(ctx)
        agent = await env.sql("SELECT state, details FROM agents.agents WHERE id='github'")
        assert agent[0][0] == "error" and agent[0][1]["ok"] is False
