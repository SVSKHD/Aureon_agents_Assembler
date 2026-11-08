"""Optional Cloudflare Access JWT verification in the app (docs/24, Phase 22).

Off unless both CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD are set. When on, every HTTP
request must carry a valid Access token in the `Cf-Access-Jwt-Assertion` header (cloudflared
adds it for an Access-protected hostname), except the CF_ACCESS_BYPASS_PATHS prefixes: the
GitHub webhook receiver (HMAC-verified itself), `/api/public/*` (rate-limited, read-only) and
`/api/health` (the on-box container healthchecks, which never pass through Cloudflare).
One machine-to-machine route is also exempt, by EXACT path only (MACHINE_PATHS): the MT5 bot's
`POST /api/trading/mt5/ingest`, which arrives over Tailscale and carries its own bearer token.

Verification: RS256 only; the signing key is looked up by `kid` in the team's JWKS at
`https://<team>/cdn-cgi/access/certs` (cached for CF_ACCESS_CERTS_TTL_SECONDS; an unknown
`kid` refetches at most once a minute, for key rotation); `aud` must contain one of
CF_ACCESS_AUD; `iss` must be `https://<team>`; `exp` (and `nbf`, `iat` when present) with
CF_ACCESS_LEEWAY_SECONDS. A failure answers 403 with a fixed body and logs only the reason.

This is defence in depth: cloudflared already enforces Access at the edge (and, with
`access.required` in deploy/cloudflare/config.yml.example, at the tunnel too), and the app's
own session + CSRF checks still apply to every route behind it.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger("cf_access")

HEADER = b"cf-access-jwt-assertion"
REFETCH_MIN_SECONDS = 60.0
MAX_TOKEN_BYTES = 8192

JwksFetcher = Callable[[str], Awaitable[dict[str, Any]]]


class AccessDenied(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def team_origin(team_domain: str) -> str:
    d = team_domain.strip().removeprefix("https://").removeprefix("http://").strip("/")
    return f"https://{d}"


# Exact paths (no prefix match, no trailing slash) that authenticate themselves and never
# carry an Access JWT: the MT5 bot's push (bearer MT5_INGEST_TOKEN, docs/12-mt5-adapter.md).
MACHINE_PATHS: frozenset[str] = frozenset({"/api/trading/mt5/ingest"})
# The Aureon V1.6.3 database API (docs/34-aureon-v163.md): every route under these patterns
# authenticates itself with a role bearer token (app/aureon/auth.py) and is reached by the
# Windows collector/guard over Tailscale, without an Access JWT. Full-path regexes, anchored.
MACHINE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"^/api/v1/ingest/[a-z_]+$",
        r"^/api/v1/ingest/trade/\d+$",
        r"^/api/v1/ingest/jobs/(claim|\d+/(renew|finish))$",
        r"^/api/v1/(status|signals|decisions|trades|chart|review|risk)$",
        r"^/api/v1/signals/\d+$",
        r"^/api/v1/review/(latest|reports)$",
        r"^/api/v1/review/reports/\d+(/files/[a-z_]+\.(csv|json|html))?$",
        r"^/api/v1/admin/(risk/clear|review/run)$",
        r"^/api/v1/aureon/(healthz|readyz)$",
    )
)


def is_machine_path(path: str) -> bool:
    return path in MACHINE_PATHS or any(p.match(path) for p in MACHINE_PATTERNS)


def is_bypassed(path: str, prefixes: Sequence[str]) -> bool:
    for p in prefixes:
        base = p.rstrip("/")
        if base and (path == base or path.startswith(base + "/")):
            return True
    return False


async def fetch_jwks_http(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as http:
        r = await http.get(url)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data


class AccessVerifier:
    def __init__(
        self,
        team_domain: str,
        audiences: Sequence[str],
        *,
        ttl_s: float = 3600.0,
        leeway_s: float = 30.0,
        fetcher: JwksFetcher = fetch_jwks_http,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.issuer = team_origin(team_domain)
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self.audiences = [a.strip() for a in audiences if a.strip()]
        self.ttl_s = ttl_s
        self.leeway_s = leeway_s
        self.fetcher = fetcher
        self.clock = clock
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()
        self.fetches = 0

    async def _refresh(self, *, force: bool) -> None:
        async with self._lock:
            now = self.clock()
            if self._fetched_at is not None:
                age = now - self._fetched_at
                if (not force and age < self.ttl_s) or (force and age < REFETCH_MIN_SECONDS):
                    return
            self.fetches += 1
            data = await self.fetcher(self.certs_url)
            keys: dict[str, Any] = {}
            for jwk in data.get("keys", []):
                if jwk.get("kty") == "RSA" and jwk.get("kid"):
                    keys[str(jwk["kid"])] = RSAAlgorithm.from_jwk(json.dumps(jwk))
            if not keys:
                raise AccessDenied("jwks_empty")
            self._keys = keys
            self._fetched_at = now

    async def key_for(self, kid: str) -> Any:
        try:
            await self._refresh(force=False)
            if kid not in self._keys:
                await self._refresh(force=True)  # rotation
        except AccessDenied:
            raise
        except Exception as exc:
            log.warning("cf_access_jwks_unavailable", error=type(exc).__name__)
            if not self._keys:
                raise AccessDenied("jwks_unavailable") from exc
        key = self._keys.get(kid)
        if key is None:
            raise AccessDenied("unknown_kid")
        return key

    async def verify(self, token: str) -> dict[str, Any]:
        if not token or len(token) > MAX_TOKEN_BYTES:
            raise AccessDenied("missing_token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AccessDenied("malformed") from exc
        if header.get("alg") != "RS256":
            raise AccessDenied("bad_alg")
        key = await self.key_for(str(header.get("kid") or ""))
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.audiences,
                issuer=self.issuer,
                leeway=self.leeway_s,
                options={"require": ["exp", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AccessDenied("expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise AccessDenied("bad_audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise AccessDenied("bad_issuer") from exc
        except jwt.PyJWTError as exc:
            raise AccessDenied("invalid") from exc
        return claims


_DENY_BODY = json.dumps(
    {"detail": {"code": "cf_access_required", "detail": "Cloudflare Access token required"}}
).encode()


class CloudflareAccessMiddleware:
    """Pure ASGI (does not buffer SSE)."""

    def __init__(
        self, app: ASGIApp, settings: Settings, verifier: AccessVerifier | None = None
    ) -> None:
        self.app = app
        self.bypass = list(settings.CF_ACCESS_BYPASS_PATHS)
        self.verifier = verifier or AccessVerifier(
            settings.CF_ACCESS_TEAM_DOMAIN or "",
            settings.CF_ACCESS_AUD,
            ttl_s=settings.CF_ACCESS_CERTS_TTL_SECONDS,
            leeway_s=settings.CF_ACCESS_LEEWAY_SECONDS,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or is_machine_path(path) or is_bypassed(path, self.bypass):
            await self.app(scope, receive, send)
            return
        token = ""
        for k, v in scope.get("headers", []):
            if k == HEADER:
                token = v.decode("latin-1").strip()
                break
        try:
            claims = await self.verifier.verify(token)
        except AccessDenied as exc:
            log.info("cf_access_denied", reason=exc.reason, path=scope.get("path", "")[:200])
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(_DENY_BODY)).encode()),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _DENY_BODY})
            return
        scope.setdefault("state", {})["cf_access"] = {
            "email": claims.get("email"),
            "sub": claims.get("sub"),
            "service_token": bool(claims.get("common_name")) and not claims.get("email"),
        }
        await self.app(scope, receive, send)
