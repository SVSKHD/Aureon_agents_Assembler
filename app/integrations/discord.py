"""Discord notifications for the approval engine (docs/23-approval-engine.md).

One incoming webhook (`DISCORD_WEBHOOK_URL`; unset = disabled). Each notice is "approval
requested / approved / denied / expired" with the kind, target, summary, risk and a deep link
`${PUBLIC_APP_URL}/dev/approvals/{id}`.

By design:

- Only those fields are sent. The approval payload, artifacts, results and actors never are,
  and the text fields are passed through the credential redactor and capped.
- The link only opens the app, where the admin signs in and re-authenticates. There are no
  buttons, no interaction endpoint and no token in the link: approving from Discord is
  impossible.
- Sending is fire-and-forget in a background task with a bounded number of attempts (429 and
  5xx and network errors are retried; other 4xx are not). A failure is logged without the URL
  and never reaches the approval flow.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.github.redact import redact

log = get_logger("integrations.discord")

VERBS = {
    "requested": "Approval requested",
    "approved": "Approved",
    "denied": "Denied",
    "expired": "Expired",
}
COLORS = {"requested": 0xF5A623, "approved": 0x2ECC71, "denied": 0xE74C3C, "expired": 0x95A5A6}
MAX_FIELD = 256
MAX_DESCRIPTION = 500
MAX_RETRY_AFTER = 10.0
MAX_PENDING = 100

_TASKS: set[asyncio.Task[bool]] = set()


def _clean(value: Any, cap: int) -> str:
    text = redact(str(value or "")).replace("@", "@​")  # no pings, even if a mention leaks
    return text if len(text) <= cap else text[: cap - 1] + "…"


def deep_link(public_app_url: str, approval_id: str) -> str:
    return f"{public_app_url.rstrip('/')}/dev/approvals/{approval_id}"


def build_payload(notice: Mapping[str, Any], public_app_url: str) -> dict[str, Any]:
    """The webhook body: an allowlist of fields, never the payload or artifacts."""
    event = str(notice.get("event") or "")
    approval_id = str(notice.get("approval_id") or "")
    link = deep_link(public_app_url, approval_id)
    fields = [
        {"name": "Kind", "value": _clean(notice.get("kind"), MAX_FIELD), "inline": True},
        {"name": "Risk", "value": _clean(notice.get("risk_level"), 32), "inline": True},
        {"name": "Target", "value": _clean(notice.get("target"), MAX_FIELD), "inline": False},
    ]
    required = notice.get("required_approvals")
    if isinstance(required, int) and required > 1:
        received = notice.get("received_approvals")
        fields.append({"name": "Approvals", "value": f"{received or 0}/{required}", "inline": True})
    return {
        "username": "Spasta approvals",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": f"{VERBS.get(event, event)}: {_clean(notice.get('kind'), 200)}",
                "description": _clean(notice.get("summary"), MAX_DESCRIPTION),
                "url": link,
                "color": COLORS.get(event, 0x7F8C8D),
                "fields": fields,
                "footer": {"text": "Open the app to review. Nothing can be approved from Discord."},
            }
        ],
    }


async def post(
    url: str,
    payload: Mapping[str, Any],
    *,
    attempts: int,
    request_seconds: float,
    retry_base_seconds: float,
) -> bool:
    """POST with a capped number of attempts. Never raises; returns whether it was delivered."""
    async with httpx.AsyncClient(timeout=request_seconds, follow_redirects=False) as http:
        for attempt in range(1, attempts + 1):
            delay = retry_base_seconds * 2 ** (attempt - 1)
            try:
                resp = await http.post(url, json=payload)
            except httpx.HTTPError as exc:
                log.warning("discord_post_error", attempt=attempt, error=type(exc).__name__)
            else:
                if resp.status_code < 300:
                    return True
                if resp.status_code == 429:
                    try:
                        retry_after = float(resp.json().get("retry_after", delay))
                    except Exception:
                        retry_after = delay
                    delay = min(max(retry_after, 0.0), MAX_RETRY_AFTER)
                elif resp.status_code < 500:
                    log.warning("discord_post_refused", status=resp.status_code)
                    return False
                log.warning("discord_post_retry", attempt=attempt, status=resp.status_code)
            if attempt < attempts:
                await asyncio.sleep(delay)
    log.warning("discord_post_gave_up", attempts=attempts)
    return False


ALERT_COLORS = {"warning": 0xF5A623, "error": 0xE74C3C, "critical": 0x8E0000}


def build_alert_payload(alert: Mapping[str, Any], public_app_url: str) -> dict[str, Any]:
    """An operational alert (docs/24): severity, kind, title, a short redacted detail and a
    deep link into the app. No payloads, no stack traces, no buttons."""
    severity = str(alert.get("severity") or "warning")
    path = str(alert.get("path") or "/dev")
    if not path.startswith("/"):
        path = "/" + path
    return {
        "username": "Spasta alerts",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": _clean(f"[{severity.upper()}] {alert.get('title') or ''}", 200),
                "description": _clean(alert.get("detail"), MAX_DESCRIPTION),
                "url": f"{public_app_url.rstrip('/')}{path}",
                "color": ALERT_COLORS.get(severity, 0x7F8C8D),
                "fields": [
                    {"name": "Kind", "value": _clean(alert.get("kind"), 64), "inline": True},
                    {"name": "Severity", "value": _clean(severity, 32), "inline": True},
                ],
                "footer": {"text": "Aureon alerts. Open the app for details."},
            }
        ],
    }


async def send_notices(settings: Settings, notices: list[Mapping[str, Any]]) -> list[bool]:
    url = settings.discord_webhook_url
    if not url:
        return []
    out = []
    for n in notices:
        try:
            ok = await post(
                url,
                build_payload(n, settings.PUBLIC_APP_URL),
                attempts=settings.DISCORD_MAX_ATTEMPTS,
                request_seconds=settings.DISCORD_REQUEST_SECONDS,
                retry_base_seconds=settings.DISCORD_RETRY_BASE_SECONDS,
            )
        except Exception as exc:  # defensive: a notifier never raises
            log.warning("discord_notify_failed", error=type(exc).__name__)
            ok = False
        out.append(ok)
    return out


async def _run(settings: Settings, notices: list[Mapping[str, Any]]) -> bool:
    return all(await send_notices(settings, notices))


def notify_background(settings: Settings, notices: list[dict[str, Any]]) -> None:
    """Schedule delivery on the running loop and return at once (no-op when disabled)."""
    if not settings.discord_webhook_url or not notices:
        return
    if len(_TASKS) >= MAX_PENDING:
        log.warning("discord_notify_dropped", reason="too many pending notifications")
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_run(settings, list(notices)))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def drain(timeout_s: float = 5.0) -> None:
    """Wait for pending notifications (tests, shutdown)."""
    if _TASKS:
        await asyncio.wait(list(_TASKS), timeout=timeout_s)


__all__ = [
    "build_alert_payload",
    "build_payload",
    "deep_link",
    "drain",
    "notify_background",
    "post",
    "send_notices",
]
