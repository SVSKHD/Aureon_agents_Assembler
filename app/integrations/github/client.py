"""Async GitHub REST client (docs/18-github-foundation.md).

- The token comes from the environment only (GITHUB_TOKEN, a fine-grained PAT). It is never
  read from the DB or a request, never logged, and every error message, log line and error
  body that leaves this module goes through `redact`.
- Pagination follows the `Link: rel="next"` header, capped at `max_pages`.
- Retries: up to `max_attempts` attempts in total, exponential backoff on 5xx and network
  errors. A 4xx is never retried, except a 429 / secondary rate limit, which waits for
  `retry-after` (or the reset time) when that is within `max_retry_wait`.
- Rate limits: every response's `x-ratelimit-*` headers update `rate_limit`. While the last
  snapshot is below `rate_limit_floor` (and its reset is still ahead) requests are refused
  locally with `GitHubRateLimited(code="github_rate_limit_floor")`.
- Timeouts are set; redirects are followed by hand and only to the API host itself, so the
  token is never sent anywhere else.
- Successful GETs are cached in memory for `cache_ttl` seconds (0 disables the cache).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.logging import get_logger
from app.integrations.github.redact import redact, redact_obj

log = get_logger("integrations.github")

API_VERSION = "2022-11-28"
USER_AGENT = "aureon-agentic-github/1"
MAX_REDIRECTS = 3
CACHE_MAX_ENTRIES = 256


def fingerprint(token: str) -> str:
    """Stable, non-reversible id for a token: the first 16 hex chars of its sha256."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def token_kind(token: str) -> str:
    if token.startswith("github_pat_"):
        return "fine_grained"
    if token.startswith("ghp_"):
        return "classic"
    return "other"


# --- errors -------------------------------------------------------------------------------


class GitHubError(Exception):
    """Base error. `status` is what the API should answer; `message` is already redacted."""

    status = 502
    code = "github_error"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code
        self.upstream_status = upstream_status


class GitHubNotConfigured(GitHubError):
    status = 503
    code = "github_not_configured"


class GitHubNotFound(GitHubError):
    status = 404
    code = "not_found"


class GitHubUnavailable(GitHubError):
    """5xx or network failure after every retry."""

    status = 502
    code = "github_unavailable"


class GitHubRateLimited(GitHubError):
    status = 429
    code = "github_rate_limited"

    def __init__(self, message: str, *, retry_after: int, code: str | None = None) -> None:
        super().__init__(message, code=code)
        self.retry_after = max(1, retry_after)


# --- rate limit snapshot ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RateLimit:
    limit: int | None
    remaining: int | None
    used: int | None
    reset_at: datetime | None
    resource: str | None
    observed_at: datetime

    @classmethod
    def from_headers(cls, headers: httpx.Headers) -> RateLimit | None:
        if "x-ratelimit-remaining" not in headers:
            return None

        def _int(name: str) -> int | None:
            try:
                return int(headers[name])
            except (KeyError, ValueError):
                return None

        reset = _int("x-ratelimit-reset")
        return cls(
            limit=_int("x-ratelimit-limit"),
            remaining=_int("x-ratelimit-remaining"),
            used=_int("x-ratelimit-used"),
            reset_at=datetime.fromtimestamp(reset, UTC) if reset is not None else None,
            resource=headers.get("x-ratelimit-resource"),
            observed_at=datetime.now(UTC),
        )

    def seconds_to_reset(self, now: datetime | None = None) -> int:
        if self.reset_at is None:
            return 60
        return max(0, int((self.reset_at - (now or datetime.now(UTC))).total_seconds()) + 1)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("reset_at", "observed_at"):
            d[k] = d[k].isoformat() if d[k] else None
        return d


@dataclass(slots=True)
class _CacheEntry:
    expires: float
    value: Any


@dataclass(slots=True)
class GitHubResponse:
    status: int
    data: Any
    headers: httpx.Headers = field(repr=False)


# --- client -------------------------------------------------------------------------------

SleepFn = Callable[[float], Awaitable[None]]


class GitHubClient:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 20.0,
        max_attempts: int = 3,
        backoff_base: float = 0.5,
        max_retry_wait: float = 20.0,
        rate_limit_floor: int = 50,
        max_pages: int = 10,
        cache_ttl: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        if not token:
            raise GitHubNotConfigured("GITHUB_TOKEN is not set")
        self._token = token
        self.fingerprint = fingerprint(token)
        self.token_kind = token_kind(token)
        self.base_url = base_url.rstrip("/")
        self._host = urlsplit(self.base_url).netloc.lower()
        self.max_attempts = max(1, min(max_attempts, 3))
        self.backoff_base = backoff_base
        self.max_retry_wait = max_retry_wait
        self.rate_limit_floor = rate_limit_floor
        self.max_pages = max_pages
        self.cache_ttl = cache_ttl
        self._sleep = sleep
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self.rate_limit: RateLimit | None = None
        self._login: str | None = None
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    def redact(self, text: str) -> str:
        return redact(text, (self._token,))

    # --- low level ------------------------------------------------------------------------

    def _check_floor(self) -> None:
        rl = self.rate_limit
        if rl is None or rl.remaining is None or rl.remaining >= self.rate_limit_floor:
            return
        wait = rl.seconds_to_reset()
        if wait <= 0:
            return  # the window has reset; the next response refreshes the snapshot
        raise GitHubRateLimited(
            f"GitHub rate limit floor reached ({rl.remaining} < {self.rate_limit_floor} left); "
            f"resets in {wait}s",
            retry_after=wait,
            code="github_rate_limit_floor",
        )

    def _same_host(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme in ("https", "http") and parts.netloc.lower() == self._host

    async def _send(
        self, method: str, url: str, params: dict[str, Any] | None, json: Any
    ) -> httpx.Response:
        """One logical request: same-host redirects only, Authorization added here."""
        headers = {"Authorization": f"Bearer {self._token}"}
        for _ in range(MAX_REDIRECTS + 1):
            resp = await self._http.request(method, url, params=params, json=json, headers=headers)
            if resp.status_code not in (301, 302, 303, 307, 308):
                return resp
            target = urljoin(str(resp.url), resp.headers.get("location", ""))
            if not self._same_host(target):
                raise GitHubError(
                    "GitHub redirected to another host; not followed", code="github_redirect"
                )
            url, params = target, None
            if resp.status_code == 303:
                method, json = "GET", None
        raise GitHubError("too many redirects from GitHub", code="github_redirect")

    def _rate_limit_wait(self, resp: httpx.Response) -> int | None:
        """Seconds to wait if `resp` is a 429 or a (secondary) rate-limit 403, else None."""
        if resp.status_code not in (403, 429):
            return None
        retry_after = resp.headers.get("retry-after")
        if retry_after is not None:
            try:
                return max(1, int(float(retry_after)))
            except ValueError:
                return 60
        rl = RateLimit.from_headers(resp.headers)
        if rl is not None and rl.remaining == 0:
            return rl.seconds_to_reset()
        if resp.status_code == 429 or "secondary rate limit" in resp.text.lower():
            return 60  # GitHub's documented default when no header says otherwise
        return None

    def _error_for(self, resp: httpx.Response) -> GitHubError:
        try:
            body = resp.json()
            msg = body.get("message") if isinstance(body, dict) else None
        except ValueError:
            msg = None
        text = self.redact(str(msg or resp.text or resp.reason_phrase)[:500])
        s = resp.status_code
        if s == 404:
            return GitHubNotFound("not found on GitHub", upstream_status=s)
        if s == 401:
            return GitHubError(
                f"GitHub rejected the token: {text}", code="github_auth_failed", upstream_status=s
            )
        if s == 403:
            return GitHubError(
                f"GitHub refused access: {text}", code="github_forbidden", upstream_status=s
            )
        if s in (409, 422):
            return GitHubError(text, status=s, code="github_conflict", upstream_status=s)
        if s >= 500:
            return GitHubUnavailable(f"GitHub returned {s}: {text}", upstream_status=s)
        return GitHubError(f"GitHub returned {s}: {text}", upstream_status=s)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> GitHubResponse:
        """Send with retry/backoff/rate-limit handling. Raises a GitHubError on failure."""
        attempt = 0
        while True:
            attempt += 1
            self._check_floor()
            try:
                resp = await self._send(method, path, params, json)
            except httpx.TransportError as exc:
                detail = self.redact(f"{type(exc).__name__}: {exc}")
                if attempt >= self.max_attempts:
                    log.warning("github_request_failed", path=self.redact(path), error=detail)
                    raise GitHubUnavailable(f"GitHub unreachable: {detail}") from None
                await self._backoff(attempt, path, detail)
                continue

            rl = RateLimit.from_headers(resp.headers)
            if rl is not None:
                self.rate_limit = rl

            if resp.status_code < 300 or resp.status_code == 304:
                data: Any = None
                if resp.content:
                    try:
                        data = resp.json()
                    except ValueError:
                        data = resp.text
                return GitHubResponse(resp.status_code, data, resp.headers)

            wait = self._rate_limit_wait(resp)
            if wait is not None:
                if attempt >= self.max_attempts or wait > self.max_retry_wait:
                    log.warning("github_rate_limited", path=self.redact(path), retry_after=wait)
                    raise GitHubRateLimited(
                        f"GitHub rate limit hit; retry in {wait}s", retry_after=wait
                    )
                log.info("github_rate_limit_wait", path=self.redact(path), seconds=wait)
                await self._sleep(wait)
                continue

            err = self._error_for(resp)
            if resp.status_code >= 500 and attempt < self.max_attempts:
                await self._backoff(attempt, path, err.message)
                continue
            log.warning(
                "github_request_error",
                path=self.redact(path),
                status=resp.status_code,
                error=err.message,
            )
            raise err

    async def _backoff(self, attempt: int, path: str, detail: str) -> None:
        delay = self.backoff_base * 2 ** (attempt - 1)
        log.info("github_retry", path=self.redact(path), attempt=attempt, delay=delay, error=detail)
        await self._sleep(delay)

    # --- helpers --------------------------------------------------------------------------

    def _cache_key(self, path: str, params: dict[str, Any] | None) -> str:
        items = sorted((params or {}).items())
        return f"{path}?{items!r}"

    def _cache_get(self, key: str) -> Any:
        if self.cache_ttl <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires < time.monotonic():
            self._cache.pop(key, None)
            return None
        self._cache.move_to_end(key)
        return entry.value

    def _cache_put(self, key: str, value: Any) -> None:
        if self.cache_ttl <= 0:
            return
        self._cache[key] = _CacheEntry(time.monotonic() + self.cache_ttl, value)
        while len(self._cache) > CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)

    def clear_cache(self) -> None:
        self._cache.clear()

    async def get_json(
        self, path: str, params: dict[str, Any] | None = None, *, cache: bool = True
    ) -> Any:
        key = self._cache_key(path, params)
        if cache and (hit := self._cache_get(key)) is not None:
            return hit
        data = (await self.request("GET", path, params=params)).data
        if cache:
            self._cache_put(key, data)
        return data

    async def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_pages: int | None = None,
        cache: bool = True,
    ) -> list[Any]:
        """Every item across pages (Link rel="next"), at most `max_pages` pages."""
        items, _complete = await self.paginate_all(path, params, max_pages=max_pages, cache=cache)
        return items

    async def paginate_all(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        max_pages: int | None = None,
        cache: bool = True,
    ) -> tuple[list[Any], bool]:
        """(items, complete): `complete` is False when the page cap cut the listing short."""
        limit = max_pages or self.max_pages
        key = self._cache_key(f"pages:{limit}:{path}", params)
        if cache and (hit := self._cache_get(key)) is not None:
            return list(hit[0]), bool(hit[1])
        items: list[Any] = []
        url: str | None = path
        page_params: dict[str, Any] | None = {"per_page": 100, **(params or {})}
        pages = 0
        while url is not None and pages < limit:
            resp = await self.request("GET", url, params=page_params)
            pages += 1
            data = resp.data
            if isinstance(data, dict):  # e.g. {"total_count":..., "repositories": [...]}
                data = next((v for v in data.values() if isinstance(v, list)), [])
            items.extend(data or [])
            nxt = _next_link(resp.headers)
            if nxt is not None and not self._same_host(nxt):
                log.warning("github_pagination_offhost", path=self.redact(path))
                nxt = None
            url, page_params = nxt, None  # the next link already carries the query
        complete = url is None
        if not complete:
            log.info("github_pagination_capped", path=self.redact(path), pages=pages)
        if cache:
            self._cache_put(key, (items, complete))
        return items, complete

    async def get_user(self) -> tuple[dict[str, Any], list[str]]:
        """GET /user (never cached) -> (user json, x-oauth-scopes). Fine-grained PATs send
        no scope header, so their scope list is empty."""
        resp = await self.request("GET", "/user")
        scopes_h = resp.headers.get("x-oauth-scopes") or ""
        scopes = [s.strip() for s in scopes_h.split(",") if s.strip()]
        data = resp.data if isinstance(resp.data, dict) else {}
        if login := data.get("login"):
            self._login = str(login)
        return data, scopes

    async def login(self) -> str:
        if self._login is None:
            await self.get_user()
        if self._login is None:
            raise GitHubError("GitHub /user returned no login", code="github_auth_failed")
        return self._login

    def safe(self, value: Any) -> Any:
        return redact_obj(value, (self._token,))


def _next_link(headers: httpx.Headers) -> str | None:
    link = str(headers.get("link") or "")
    if not link:
        return None
    for part in link.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().strip("<>")
        if any(p.strip() in ('rel="next"', "rel=next") for p in section[1:]):
            return url
    return None
