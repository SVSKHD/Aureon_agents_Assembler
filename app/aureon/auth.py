"""Machine authentication for the Aureon V1.6.3 API (docs/34-aureon-v163.md).

The Windows collector and guard authenticate with a bearer token per role; no browser
session, cookie or CSRF token is involved, and no session authorizes these routes.

Roles (least privilege; a route lists exactly the roles it accepts):
    reader     reports and queries (status, signals, trades, chart, review, risk)
    writer     reader + operator decisions (POST /api/v1/decisions)
    collector  bars, MT5 deal history, collector jobs, + the service routes
    guard      trades, trade events, risk state/events, + the service routes
    service    heartbeats, alerts, control values, shared state, last bar
    admin      reads + privileged controls (risk clear, manual review run), audited
V1.6.3 let the admin token into every route; here it is limited to reads and admin routes.
V1.6.3's single legacy MT5 token (AUREON_API_SVC_TOKEN) acts as collector + guard only
while neither of those tokens is set.

Every token is compared in constant time (hmac.compare_digest, every role checked). A
token is never logged, stored or echoed. Requests per minute are limited per (role, client)
in Redis; failed authentications per client are limited separately, so tokens cannot be
guessed at speed. Bodies above AUREON_API_MAX_BODY_BYTES are refused (413).
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from starlette.responses import Response

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger("aureon.auth")

ROLE_SETTINGS = {
    "reader": "AUREON_API_READ_TOKEN",
    "writer": "AUREON_API_WRITE_TOKEN",
    "collector": "AUREON_API_COLLECTOR_TOKEN",
    "guard": "AUREON_API_GUARD_TOKEN",
    "service": "AUREON_API_SERVICE_TOKEN",
    "admin": "AUREON_API_ADMIN_TOKEN",
}
READ = ("reader", "writer", "admin")
WRITE = ("writer",)
COLLECTOR = ("collector",)
GUARD = ("guard",)
SERVICE = ("collector", "guard", "service")
ADMIN = ("admin",)
FAILED_AUTH_PER_MINUTE = 30


def _secret(settings: Settings, name: str) -> str:
    s = getattr(settings, name, None)
    return s.get_secret_value() if s else ""


def role_tokens(settings: Settings) -> dict[str, str]:
    toks = {role: _secret(settings, name) for role, name in ROLE_SETTINGS.items()}
    svc = _secret(settings, "AUREON_API_SVC_TOKEN")
    if svc and not (toks["collector"] or toks["guard"]):
        toks["collector"] = toks["guard"] = svc
    return toks


def bearer(header: str | None) -> str:
    h = header or ""
    return h[7:] if h.startswith("Bearer ") else ""


def role_of(settings: Settings, token: str) -> str | None:
    """Constant time per role, and every role is compared (no early exit on a match)."""
    found: str | None = None
    for role, t in role_tokens(settings).items():
        if t and token and hmac.compare_digest(token.encode(), t.encode()) and found is None:
            found = role
    return found


def client_of(request: Request) -> str:
    return request.client.host if request.client else "-"


async def _over_limit(request: Request, key: str, limit: int) -> bool:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return False
    bucket = f"aureon:rl:{key}:{int(time.time() // 60)}"
    try:
        n = await redis.incr(bucket)
        if n == 1:
            await redis.expire(bucket, 120)
        return int(n) > limit
    except Exception as exc:  # the limiter never blocks the API when Redis is down
        log.warning("aureon_ratelimit_unavailable", error=type(exc).__name__)
        return False


def require(*roles: str) -> Callable[[Request], Coroutine[Any, Any, str]]:
    allowed = frozenset(roles)

    async def aureon_role(request: Request) -> str:
        settings: Settings = request.app.state.settings
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.AUREON_API_MAX_BODY_BYTES:
            raise HTTPException(413, "payload too large")
        if not settings.aureon_api_enabled:
            raise HTTPException(503, "Aureon API disabled (no AUREON_API_*_TOKEN set)")
        role = role_of(settings, bearer(request.headers.get("authorization")))
        request.state.aureon_role = role
        client = client_of(request)
        if role is None:
            if await _over_limit(request, f"fail:{client}", FAILED_AUTH_PER_MINUTE):
                raise HTTPException(429, "rate limit")
            raise HTTPException(401, "invalid token")
        if role not in allowed:
            raise HTTPException(403, f"role {role} not allowed")
        if await _over_limit(request, f"{role}:{client}", settings.AUREON_API_RATE_PER_MINUTE):
            raise HTTPException(429, "rate limit")
        return role

    return aureon_role


class AuditedRoute(APIRoute):
    """Writes aureon.api_audit for every write and every 401/403/413/429 (V1.6.3 behaviour),
    with the role, method, path, status and client; never a body or a token."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def audited(request: Request) -> Response:
            status = 500
            try:
                resp = await handler(request)
                status = resp.status_code
                return resp
            except HTTPException as exc:
                status = exc.status_code
                raise
            finally:
                if request.method != "GET" or status in (401, 403, 413, 429):
                    await _audit(request, status)

        return audited


async def _audit(request: Request, status: int) -> None:
    from app.aureon import db as adb

    maker = getattr(request.app.state, "sessionmaker", None)
    if maker is None:
        return
    try:
        async with maker() as s, s.begin():
            await adb.api_audit(
                s,
                getattr(request.state, "aureon_role", None),
                request.method,
                request.url.path,
                status,
                client_of(request),
            )
    except Exception as exc:  # auditing must never turn an answer into an error
        log.warning("aureon_audit_failed", error=type(exc).__name__)
