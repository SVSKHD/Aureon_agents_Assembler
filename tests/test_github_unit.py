"""GitHub client (docs/18-github-foundation.md): pagination, retry, rate limits, redirects,
redaction, plus input validation and the allowlist. No DB."""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
import respx
import structlog

from app.core.config import Settings
from app.integrations.github import build_github_client
from app.integrations.github import client as gh_client
from app.integrations.github.client import (
    GitHubClient,
    GitHubError,
    GitHubNotFound,
    GitHubRateLimited,
    GitHubUnavailable,
    fingerprint,
)
from app.integrations.github.redact import REDACTED, redact, redact_obj
from app.services import github as gh
from app.workers.scheduler import default_jobs

API = "https://api.github.test"
TOKEN = "github_pat_" + "A1b2C3d4" * 10
CLASSIC = "ghp_" + "Z9y8X7w6" * 5


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, s: float) -> None:
        self.calls.append(s)


def make(sleep: Sleeps | None = None, **kw: Any) -> GitHubClient:
    kw.setdefault("cache_ttl", 0)
    kw.setdefault("backoff_base", 0.5)
    return GitHubClient(TOKEN, API, sleep=sleep or Sleeps(), **kw)


def rl_headers(remaining: int, reset_in: int = 600) -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-used": str(5000 - remaining),
        "x-ratelimit-reset": str(int(time.time()) + reset_in),
        "x-ratelimit-resource": "core",
    }


# --- redaction ----------------------------------------------------------------------------


def test_redact_known_token_and_token_shapes() -> None:
    text = (
        f"bad creds {TOKEN} and {CLASSIC} and gho_abcdefghij1234 "
        'headers {"Authorization": "Bearer sometokenvalue123"} token required'
    )
    out = redact(text, [TOKEN])
    assert TOKEN not in out and CLASSIC not in out and "gho_abcdefghij1234" not in out
    assert "sometokenvalue123" not in out
    assert out.count(REDACTED) >= 4
    assert "token required" in out  # ordinary prose is left alone
    assert redact_obj({"a": [f"x{CLASSIC}"], "n": 1}) == {"a": [f"x{REDACTED}"], "n": 1}


def test_fingerprint_is_not_the_token() -> None:
    fp = fingerprint(TOKEN)
    assert len(fp) == 16 and fp not in TOKEN and fp == fingerprint(TOKEN)


def capture(monkeypatch: pytest.MonkeyPatch) -> structlog.testing.LogCapture:
    """Capture what the client hands to structlog (module loggers are cached on first use,
    so swap the client's logger for one that records event dicts)."""
    cap = structlog.testing.LogCapture()
    monkeypatch.setattr(gh_client, "log", structlog.wrap_logger(None, processors=[cap]))
    return cap


@respx.mock
async def test_token_in_error_body_never_reaches_exception_or_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    respx.get(f"{API}/repos/o/r").mock(
        return_value=httpx.Response(
            400, json={"message": f"Bad credentials for {TOKEN} (also {CLASSIC})"}
        )
    )
    client = make()
    logs = capture(monkeypatch).entries
    with pytest.raises(GitHubError) as ei:
        await client.request("GET", "/repos/o/r")
    assert TOKEN not in str(ei.value) and CLASSIC not in ei.value.message
    assert REDACTED in ei.value.message
    assert logs, "the client logs the failed request"
    assert TOKEN not in repr(logs) and CLASSIC not in repr(logs)
    await client.aclose()


@respx.mock
async def test_authorization_header_is_sent_but_not_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = respx.get(f"{API}/user").mock(return_value=httpx.Response(200, json={"login": "me"}))
    client = make()
    logs = capture(monkeypatch).entries
    user, scopes = await client.get_user()
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    assert user["login"] == "me" and scopes == [] and await client.login() == "me"
    assert TOKEN not in repr(logs)
    await client.aclose()


# --- pagination ---------------------------------------------------------------------------


@respx.mock
async def test_pagination_follows_link_over_three_pages() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("page", "1"))
        headers = {}
        if n < 3:
            headers["link"] = (
                f'<{API}/user/repos?per_page=100&page={n + 1}>; rel="next", '
                f'<{API}/user/repos?per_page=100&page=3>; rel="last"'
            )
        return httpx.Response(200, json=[{"n": n, "i": i} for i in range(2)], headers=headers)

    route = respx.get(f"{API}/user/repos").mock(side_effect=page)
    client = make()
    items, complete = await client.paginate_all("/user/repos")
    assert [i["n"] for i in items] == [1, 1, 2, 2, 3, 3] and complete
    assert route.call_count == 3
    assert route.calls[0].request.url.params["per_page"] == "100"
    await client.aclose()


@respx.mock
async def test_pagination_respects_max_pages() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params.get("page", "1"))
        link = f'<{API}/x?page={n + 1}>; rel="next"'
        return httpx.Response(200, json=[n], headers={"link": link})

    route = respx.get(f"{API}/x").mock(side_effect=page)
    client = make(max_pages=2)
    items, complete = await client.paginate_all("/x")
    assert items == [1, 2] and not complete and route.call_count == 2
    await client.aclose()


@respx.mock
async def test_pagination_never_follows_offhost_next_link() -> None:
    respx.get(f"{API}/x").mock(
        return_value=httpx.Response(
            200, json=[1], headers={"link": '<https://evil.test/x?page=2>; rel="next"'}
        )
    )
    evil = respx.get("https://evil.test/x").mock(return_value=httpx.Response(200, json=[2]))
    client = make()
    assert await client.paginate("/x") == [1]
    assert evil.call_count == 0
    await client.aclose()


# --- retry --------------------------------------------------------------------------------


@respx.mock
async def test_retry_on_502_then_success() -> None:
    route = respx.get(f"{API}/repos/o/r").mock(
        side_effect=[httpx.Response(502, text="bad gateway"), httpx.Response(200, json={"a": 1})]
    )
    sleeps = Sleeps()
    client = make(sleeps)
    assert (await client.request("GET", "/repos/o/r")).data == {"a": 1}
    assert route.call_count == 2 and sleeps.calls == [0.5]
    await client.aclose()


@respx.mock
async def test_5xx_gives_up_after_three_attempts_with_backoff() -> None:
    route = respx.get(f"{API}/x").mock(return_value=httpx.Response(503))
    sleeps = Sleeps()
    client = make(sleeps)
    with pytest.raises(GitHubUnavailable):
        await client.request("GET", "/x")
    assert route.call_count == 3 and sleeps.calls == [0.5, 1.0]
    await client.aclose()


@respx.mock
async def test_network_error_is_retried() -> None:
    route = respx.get(f"{API}/x").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json=[])]
    )
    client = make()
    assert (await client.request("GET", "/x")).data == []
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_4xx_is_not_retried() -> None:
    route = respx.get(f"{API}/x").mock(return_value=httpx.Response(404, json={"message": "nf"}))
    client = make()
    with pytest.raises(GitHubNotFound):
        await client.request("GET", "/x")
    assert route.call_count == 1
    await client.aclose()


# --- rate limits --------------------------------------------------------------------------


@respx.mock
async def test_429_waits_for_retry_after_then_succeeds() -> None:
    route = respx.get(f"{API}/x").mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "3"}, json={"message": "slow down"}),
            httpx.Response(200, json={"ok": True}, headers=rl_headers(4000)),
        ]
    )
    sleeps = Sleeps()
    client = make(sleeps)
    assert (await client.request("GET", "/x")).data == {"ok": True}
    assert route.call_count == 2 and sleeps.calls == [3]
    assert client.rate_limit is not None and client.rate_limit.remaining == 4000
    await client.aclose()


@respx.mock
async def test_secondary_rate_limit_403_is_retried() -> None:
    route = respx.get(f"{API}/x").mock(
        side_effect=[
            httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"}),
            httpx.Response(200, json=[]),
        ]
    )
    sleeps = Sleeps()
    client = make(sleeps, max_retry_wait=120)
    await client.request("GET", "/x")
    assert route.call_count == 2 and sleeps.calls == [60]
    await client.aclose()


@respx.mock
async def test_long_retry_after_is_returned_not_waited() -> None:
    respx.get(f"{API}/x").mock(return_value=httpx.Response(429, headers={"retry-after": "600"}))
    sleeps = Sleeps()
    client = make(sleeps, max_retry_wait=20)
    with pytest.raises(GitHubRateLimited) as ei:
        await client.request("GET", "/x")
    assert ei.value.retry_after == 600 and ei.value.status == 429 and sleeps.calls == []
    await client.aclose()


@respx.mock
async def test_rate_limit_floor_refuses_without_calling_github() -> None:
    route = respx.get(f"{API}/x").mock(
        return_value=httpx.Response(200, json=[], headers=rl_headers(10, reset_in=300))
    )
    client = make(rate_limit_floor=50)
    await client.request("GET", "/x")  # learns remaining=10
    with pytest.raises(GitHubRateLimited) as ei:
        await client.request("GET", "/x")
    assert ei.value.code == "github_rate_limit_floor" and 0 < ei.value.retry_after <= 301
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_floor_lifts_once_the_window_has_reset() -> None:
    respx.get(f"{API}/x").mock(
        return_value=httpx.Response(200, json=[], headers=rl_headers(10, reset_in=-5))
    )
    client = make(rate_limit_floor=50)
    await client.request("GET", "/x")
    await client.request("GET", "/x")  # reset time passed: allowed
    await client.aclose()


# --- redirects, cache ---------------------------------------------------------------------


@respx.mock
async def test_redirect_to_other_host_is_refused() -> None:
    respx.get(f"{API}/x").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.test/steal"})
    )
    evil = respx.get("https://evil.test/steal").mock(return_value=httpx.Response(200))
    client = make()
    with pytest.raises(GitHubError) as ei:
        await client.request("GET", "/x")
    assert ei.value.code == "github_redirect" and evil.call_count == 0
    await client.aclose()


@respx.mock
async def test_same_host_redirect_is_followed() -> None:
    respx.get(f"{API}/repos/old/name").mock(
        return_value=httpx.Response(301, headers={"location": f"{API}/repositories/7"})
    )
    respx.get(f"{API}/repositories/7").mock(return_value=httpx.Response(200, json={"id": 7}))
    client = make()
    assert (await client.request("GET", "/repos/old/name")).data == {"id": 7}
    await client.aclose()


@respx.mock
async def test_get_json_cache() -> None:
    route = respx.get(f"{API}/x").mock(return_value=httpx.Response(200, json={"v": 1}))
    client = make(cache_ttl=60)
    await client.get_json("/x")
    await client.get_json("/x")
    assert route.call_count == 1
    await client.get_json("/x", cache=False)
    assert route.call_count == 2
    await client.aclose()


# --- validation, allowlist, config --------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../etc/passwd",
        "a/../../b",
        "a/./b",
        "/abs",
        "a//b",
        "a\\b",
        "%2e%2e/x",
        "a/%2E%2E/b",
        "x\x00y",
        "~/x",
    ],
)
def test_check_path_rejects_traversal(path: str) -> None:
    with pytest.raises(gh.InvalidInput):
        gh.check_path(path)


def test_check_path_accepts_normal_paths() -> None:
    assert gh.check_path("src/app/main.py") == "src/app/main.py"
    assert gh.check_path("docs/") == "docs"
    assert gh.check_path("") == ""
    assert gh.check_path(".github/workflows/ci.yml") == ".github/workflows/ci.yml"


@pytest.mark.parametrize("ref", ["../x", "-x", "a..b", "a b", "x@{1}", "/x"])
def test_check_ref_rejects(ref: str) -> None:
    with pytest.raises(gh.InvalidInput):
        gh.check_ref(ref)


def test_check_ref_accepts() -> None:
    assert gh.check_ref("feature/x-1.2") == "feature/x-1.2"
    assert gh.check_ref("a" * 40) == "a" * 40
    assert gh.check_ref(None) is None


def test_allowlist_rules() -> None:
    own = gh.Allowlist(frozenset(), frozenset())
    assert own.allows("Me/x", "me") and not own.allows("other/x", "me")
    assert not own.allows("me/x", None)
    owners = gh.Allowlist(frozenset({"acme"}), frozenset())
    assert owners.allows("ACME/x", "me") and not owners.allows("me/x", "me")
    repos = gh.Allowlist(frozenset(), frozenset({"acme/api"}))
    assert repos.allows("acme/API", None) and not repos.allows("acme/web", None)
    both = gh.Allowlist(frozenset({"me"}), frozenset({"acme/api", "me/x"}))
    assert both.allows("me/x", None) and not both.allows("acme/api", None)


def _settings(**kw: Any) -> Settings:
    kw.setdefault("GITHUB_TOKEN", None)  # ignore any token in the host environment
    return Settings(DATABASE_URL="postgresql+asyncpg://x:x@h/d", SECRET_KEY="s" * 40, **kw)


def test_config_parses_json_lists_and_builds_client() -> None:
    s = _settings(
        GITHUB_TOKEN=TOKEN,
        GITHUB_ALLOWED_OWNERS='["acme", "me"]',
        GITHUB_ALLOWED_REPOS='["acme/api"]',
    )
    assert s.GITHUB_ALLOWED_OWNERS == ["acme", "me"] and s.GITHUB_ALLOWED_REPOS == ["acme/api"]
    assert s.github_configured
    assert TOKEN not in repr(s)
    client = build_github_client(s)
    assert client is not None and client.token_kind == "fine_grained"
    assert ("github-sync", "github_sync", s.GITHUB_SYNC_CRON) in default_jobs(s)


def test_no_token_means_no_client_and_no_job() -> None:
    s = _settings(GITHUB_ALLOWED_OWNERS="")
    assert not s.github_configured and build_github_client(s) is None
    assert s.GITHUB_ALLOWED_OWNERS == []
    assert all(j[1] != "github_sync" for j in default_jobs(s))


def test_global_log_processor_masks_github_tokens_in_free_text() -> None:
    from app.core.logging import redact_secrets

    ev = redact_secrets(None, "info", {"event": f"lib said {TOKEN} / {CLASSIC}", "x": [CLASSIC]})
    assert TOKEN not in repr(ev) and CLASSIC not in repr(ev)


def test_prod_check_requires_https_github_api() -> None:
    from app.core.prod_checks import production_problems

    s = _settings(GITHUB_TOKEN=TOKEN, GITHUB_API_URL="http://plain.test")
    assert any("GITHUB_API_URL" in p for p in production_problems(s))
    s = _settings(GITHUB_TOKEN=TOKEN)
    assert not any("GITHUB_API_URL" in p for p in production_problems(s))
