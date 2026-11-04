"""GitHub webhook receiver (docs/20-github-actions-webhooks.md).

    POST /api/v1/github/webhooks     public: no session, no CSRF; HMAC-verified instead

- 503 `webhook_not_configured` without GITHUB_WEBHOOK_SECRET
- 415 unless Content-Type is application/json; 413 over GITHUB_WEBHOOK_MAX_BYTES
- 401 for a missing or wrong X-Hub-Signature-256 (constant-time compare). Nothing is stored;
  only a Redis counter (`github:webhooks:rejected`) and a log line.
- 400 for a missing X-GitHub-Event / X-GitHub-Delivery or a body that is not a JSON object
- 202 `accepted` (stored + queued), 202 `ignored` (stored only: repo off the allowlist or an
  event this service does not handle), 200 `duplicate` (that delivery id was seen before)

The work happens in the `github-webhooks` worker, so the answer is fast.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.ui import ui_module
from app.auth.deps import SettingsDep
from app.schemas.github_actions import WebhookAck
from app.services import github_webhooks as hooks

UI_MODULE = ui_module("github", label="GitHub", group="dev", icon="github", order=52)
router = APIRouter(prefix="/v1/github", tags=["github-webhooks"])


def _err(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "detail": detail}}, status_code=status)


async def _body(request: Request, cap: int) -> bytes | None:
    """The raw body, or None once it is over `cap` bytes (reading stops there)."""
    length = request.headers.get("content-length")
    if length is not None and length.strip().isdigit() and int(length) > cap:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > cap:
            return None
    return bytes(buf)


@router.post(
    "/webhooks",
    response_model=WebhookAck,
    status_code=202,
    responses={200: {"model": WebhookAck, "description": "duplicate delivery"}},
)
async def receive(request: Request, settings: SettingsDep) -> JSONResponse:
    redis: Redis | None = getattr(request.app.state, "redis", None)
    if not settings.github_webhook_configured or settings.GITHUB_WEBHOOK_SECRET is None:
        await hooks.count_rejected(redis, "not_configured")
        return _err(503, "webhook_not_configured", "GITHUB_WEBHOOK_SECRET is not set")
    ctype = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        await hooks.count_rejected(redis, "content_type")
        return _err(415, "unsupported_media_type", "content type must be application/json")
    body = await _body(request, settings.GITHUB_WEBHOOK_MAX_BYTES)
    if body is None:
        await hooks.count_rejected(redis, "too_large")
        return _err(413, "payload_too_large", "webhook body is too large")
    secret = settings.GITHUB_WEBHOOK_SECRET.get_secret_value()
    if not hooks.verify_signature(secret, body, request.headers.get("x-hub-signature-256")):
        await hooks.count_rejected(redis, "bad_signature")
        return _err(401, "invalid_signature", "missing or invalid X-Hub-Signature-256")
    event = request.headers.get("x-github-event", "")
    delivery = request.headers.get("x-github-delivery", "")
    if not hooks.EVENT_RE.fullmatch(event) or not hooks.DELIVERY_RE.fullmatch(delivery):
        await hooks.count_rejected(redis, "bad_headers")
        return _err(400, "invalid_headers", "X-GitHub-Event / X-GitHub-Delivery missing")
    try:
        payload: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        payload = None
    if not isinstance(payload, dict):
        await hooks.count_rejected(redis, "bad_json")
        return _err(400, "invalid_json", "body must be a JSON object")
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    stored = await hooks.store(
        maker, redis, settings, delivery_id=delivery, event=event, payload=payload
    )
    ack = WebhookAck(status=stored.status, delivery_id=delivery)
    return JSONResponse(ack.model_dump(), status_code=200 if stored.status == "duplicate" else 202)
