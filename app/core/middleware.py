"""Request-ID + access-log middleware (pure ASGI, so it does not buffer SSE streams later)."""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
log = structlog.get_logger("access")


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER.encode(), b"").decode("latin-1")
        # Accept a caller-supplied ID only if it is short and safe to log.
        request_id = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.encode(), request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            log.info(
                "request",
                method=scope["method"],
                path=scope["path"],
                status=status,
                duration_ms=round((time.perf_counter() - start) * 1000, 2),
            )
            structlog.contextvars.unbind_contextvars("request_id")


# Defaults for every API response; a header the route already set is left alone (e.g. the
# image file's Cache-Control). HSTS is set by Nginx/Cloudflare, which terminate TLS.
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"),
)
# Swagger UI (dev only) loads its own scripts and styles, so it gets no CSP.
_NO_CSP_PATHS = ("/api/docs",)


class SecurityHeadersMiddleware:
    """Pure ASGI (no buffering, so SSE streams are untouched)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        skip_csp = str(scope.get("path", "")).startswith(_NO_CSP_PATHS)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for name, value in SECURITY_HEADERS:
                    if name in present or (skip_csp and name == b"content-security-policy"):
                        continue
                    headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class SchemaHeaderMiddleware:
    """Adds `X-API-Schema: <hash>` (app.state.schema_hash, docs/ui-manifest.md) to every
    /api response, so the frontend notices a new API right after a deploy. Pure ASGI."""

    def __init__(self, app: ASGIApp, state: Any) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope.get("path", "")).startswith("/api"):
            await self.app(scope, receive, send)
            return
        value = str(getattr(self.state, "schema_hash", "") or "").encode()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and value:
                headers = list(message.get("headers", []))
                headers.append((b"x-api-schema", value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
