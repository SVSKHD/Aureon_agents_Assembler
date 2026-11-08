"""GitHub webhooks: signature check, idempotent storage, the Redis hand-off and the event
processing the `github-webhooks` worker runs (docs/20-github-actions-webhooks.md).

Receiver (`POST /api/v1/github/webhooks`, public): HMAC-SHA256 of the raw body with
GITHUB_WEBHOOK_SECRET, compared in constant time. An invalid or missing signature stores
nothing (a Redis counter and a log line only). A valid delivery is inserted into
github.webhook_events keyed by X-GitHub-Delivery (a redelivery is a no-op `duplicate`) and
its id pushed on the Redis list `github:webhooks`; repos outside the allowlist, and events
this service does not handle, are stored as `ignored` and never queued.

Processing (`process_next`): one pending, due event is locked (FOR UPDATE SKIP LOCKED) and
handled inside a savepoint, so a failure rolls back its partial writes but keeps the row:
`attempts` goes up and the event is retried after a backoff, or marked `failed` once it has
used GITHUB_WEBHOOK_MAX_ATTEMPTS (a poison event never blocks the queue).

`on_workflow_failed(fn)` subscribes `fn` to completed workflow runs that failed. Phase 18
(self-repair) uses it; nothing subscribes yet. Callbacks run after the event is committed;
one that raises is logged and never retried, and never fails the event.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.logging import get_logger
from app.integrations.github.redact import redact, redact_obj
from app.models.github import GitHubConnection, GitHubRepository, GitHubWebhookEvent
from app.services import devops
from app.services import github as gh
from app.services import github_actions as actions

log = get_logger("services.github_webhooks")

QUEUE_KEY = "github:webhooks"
REJECTED_KEY = "github:webhooks:rejected"
QUEUE_MAX = 10_000  # the list is trimmed; anything dropped is still found by the DB scan
HANDLED = frozenset(
    {
        "workflow_run",
        "workflow_job",
        "pull_request",
        "issues",
        "push",
        "ping",
        "deployment",
        "deployment_status",
    }
)
EVENT_RE = re.compile(r"^[a-z_]{1,64}$")
DELIVERY_RE = re.compile(r"^[A-Za-z0-9-]{1,100}$")
MAX_BACKOFF_SECONDS = 3600
MAX_ERROR = 2000
# Dropped first (in this order) when a payload is over GITHUB_WEBHOOK_STORE_MAX_BYTES.
HEAVY_KEYS = (
    "commits",
    "head_commit",
    "pull_request",
    "issue",
    "comment",
    "sender",
    "organization",
    "enterprise",
    "installation",
    "repository",
)
KEEP_KEYS = (
    "action",
    "repository",
    "workflow_run",
    "workflow_job",
    "deployment",
    "deployment_status",
    "ref",
    "number",
    "zen",
)
_SUMMARY_FIELDS = (
    "id",
    "number",
    "full_name",
    "name",
    "login",
    "state",
    "title",
    "html_url",
    "sha",
    "ref",
    "merged",
)


# --- signature ----------------------------------------------------------------------------


def signature_for(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time check of X-Hub-Signature-256 (`sha256=<hex>`)."""
    if not header or not secret:
        return False
    expected = signature_for(secret, body).encode()
    given = header.strip().encode("latin-1", "replace")
    return hmac.compare_digest(expected, given)


async def count_rejected(redis: Redis | None, reason: str) -> None:
    log.warning("github_webhook_rejected", reason=reason)
    if redis is None:
        return
    with contextlib.suppress(Exception):
        await redis.hincrby(REJECTED_KEY, reason, 1)


# --- payload ------------------------------------------------------------------------------


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str).encode())


def _minimal(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: value[k] for k in _SUMMARY_FIELDS if isinstance(value.get(k), str | int | bool)}
    if isinstance(value, list):
        return []
    return value


def trim_payload(payload: dict[str, Any], cap: int) -> tuple[dict[str, Any], bool]:
    """The payload to store (token-redacted), at most `cap` bytes as JSON -> (value, trimmed).
    The big objects are cut down to a summary first; workflow_run / workflow_job are kept
    whole as long as possible because the worker needs them."""
    out: dict[str, Any] = redact_obj(payload)
    if _size(out) <= cap:
        return out, False
    for k in HEAVY_KEYS:
        if k in out:
            out[k] = _minimal(out[k])
            if _size(out) <= cap:
                return out, True
    out = {k: out[k] for k in KEEP_KEYS if k in out}
    if _size(out) <= cap:
        return out, True
    small = {k: _minimal(out.get(k)) for k in KEEP_KEYS if k in out}
    return (small if _size(small) <= cap else {"action": payload.get("action")}), True


def repo_of(payload: dict[str, Any]) -> str | None:
    repo = payload.get("repository")
    name = repo.get("full_name") if isinstance(repo, dict) else None
    return str(name)[:201] if isinstance(name, str) and "/" in name else None


async def repo_allowed(db: AsyncSession, settings: Settings, full: str | None) -> bool:
    """The Phase 15 allowlist. With neither list set it means the token user's own repos:
    the login comes from the last successful connection check (no GitHub call here)."""
    if not full:
        return False
    allow = gh.Allowlist.from_settings(settings)
    login = None
    if not allow.repos and not allow.owners:
        login = await db.scalar(
            select(GitHubConnection.login)
            .where(GitHubConnection.ok.is_(True), GitHubConnection.login.is_not(None))
            .order_by(GitHubConnection.last_checked_at.desc().nulls_last())
            .limit(1)
        )
    return allow.allows(full, login)


# --- receiver -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stored:
    status: Literal["accepted", "duplicate", "ignored"]
    event_id: int | None


async def store(
    maker: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    settings: Settings,
    *,
    delivery_id: str,
    event: str,
    payload: dict[str, Any],
) -> Stored:
    full = repo_of(payload)
    action = payload.get("action")
    stored, trimmed = trim_payload(payload, settings.GITHUB_WEBHOOK_STORE_MAX_BYTES)
    async with maker() as db, db.begin():
        allowed = event in HANDLED and await repo_allowed(db, settings, full)
        values: dict[str, Any] = {
            "delivery_id": delivery_id,
            "event": event,
            "action": str(action)[:64] if isinstance(action, str) else None,
            "repo": full,
            "payload": stored,
            "payload_truncated": trimmed,
            "signature_valid": True,
            "status": "pending" if allowed else "ignored",
        }
        if not allowed:
            values["processed_at"] = func.now()
            values["error"] = (
                f"event {event} is not handled" if event not in HANDLED else "repo not allowlisted"
            )
        event_id = await db.scalar(
            insert(GitHubWebhookEvent)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[GitHubWebhookEvent.delivery_id])
            .returning(GitHubWebhookEvent.id)
        )
    if event_id is None:
        log.info("github_webhook_duplicate", delivery_id=delivery_id, gh_event=event)
        return Stored("duplicate", None)
    if not allowed:
        log.info("github_webhook_ignored", delivery_id=delivery_id, gh_event=event, repo=full)
        return Stored("ignored", event_id)
    if redis is not None:
        try:
            await redis.lpush(QUEUE_KEY, delivery_id)
            await redis.ltrim(QUEUE_KEY, 0, QUEUE_MAX - 1)
        except Exception as exc:  # the worker's DB scan still finds it
            log.warning("github_webhook_enqueue_failed", error=type(exc).__name__)
    log.info("github_webhook_accepted", delivery_id=delivery_id, gh_event=event, repo=full)
    return Stored("accepted", event_id)


# --- callbacks (Phase 18 hook) ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowFailure:
    """A completed workflow run whose conclusion is failure / timed_out / startup_failure.

    `branch` and `event` let a subscriber skip runs it caused itself (the Phase 18
    recursion guard: a repair PR whose CI fails must not start another repair)."""

    delivery_id: str
    repo: str
    run_id: int
    workflow_id: int | None
    name: str | None
    path: str | None
    branch: str | None
    head_sha: str | None
    event: str | None
    conclusion: str
    run_attempt: int | None
    html_url: str | None


FailureCallback = Callable[[WorkflowFailure], Awaitable[None]]
_FAILURE_CALLBACKS: list[FailureCallback] = []


def on_workflow_failed(fn: FailureCallback) -> FailureCallback:
    """Subscribe `fn` (usable as a decorator). Subscribing twice is a no-op."""
    if fn not in _FAILURE_CALLBACKS:
        _FAILURE_CALLBACKS.append(fn)
    return fn


def remove_workflow_failed(fn: FailureCallback) -> None:
    with contextlib.suppress(ValueError):
        _FAILURE_CALLBACKS.remove(fn)


async def _fire(failure: WorkflowFailure) -> None:
    for fn in list(_FAILURE_CALLBACKS):
        try:
            await fn(failure)
        except Exception as exc:
            log.warning(
                "github_workflow_failed_callback_error",
                callback=getattr(fn, "__name__", "?"),
                error=type(exc).__name__,
            )


# --- processing ---------------------------------------------------------------------------


class WebhookPayloadError(ValueError):
    """The stored payload lacks what its event needs."""


def _obj(payload: dict[str, Any], key: str) -> dict[str, Any]:
    v = payload.get(key)
    if not isinstance(v, dict) or "id" not in v:
        raise WebhookPayloadError(f"payload has no {key}")
    return v


async def _handle(
    db: AsyncSession, ev: GitHubWebhookEvent, settings: Settings
) -> WorkflowFailure | None:
    p = ev.payload or {}
    full = ev.repo or repo_of(p)
    if ev.event == "workflow_run":
        if not full:
            raise WebhookPayloadError("payload has no repository")
        run = _obj(p, "workflow_run")
        values = actions.run_values(full, run)
        stored = await actions.upsert_run(db, values)
        if stored is not None:  # applied (not an older, out-of-order delivery)
            await devops.on_workflow_run(db, settings, stored)
        if p.get("action") == "completed" and values["conclusion"] in actions.FAILED_CONCLUSIONS:
            return WorkflowFailure(
                delivery_id=ev.delivery_id,
                repo=full,
                run_id=values["id"],
                workflow_id=values["workflow_id"],
                name=values["name"],
                path=values["path"],
                branch=values["branch"],
                head_sha=values["head_sha"],
                event=values["event"],
                conclusion=values["conclusion"],
                run_attempt=values["run_attempt"],
                html_url=values["html_url"],
            )
    elif ev.event == "workflow_job":
        job = _obj(p, "workflow_job")
        if not isinstance(job.get("run_id"), int):
            raise WebhookPayloadError("workflow_job has no run_id")
        await actions.upsert_job(db, actions.job_values(full, job))
    elif ev.event == "push" and full:
        # Recorded; the synced repo row learns it was pushed (the API keeps no cache to drop).
        await db.execute(
            update(GitHubRepository)
            .where(func.lower(GitHubRepository.full_name) == full.lower())
            .values(pushed_at=func.now())
        )
    elif ev.event == "pull_request" and full:
        pr = p.get("pull_request")
        if isinstance(pr, dict):
            action = p.get("action")
            await devops.on_pull_request(
                db, settings, full, pr, action if isinstance(action, str) else None,
                delivery_id=ev.delivery_id,
            )  # fmt: skip
    elif ev.event in ("deployment", "deployment_status") and full:
        dep = _obj(p, "deployment")
        status = p.get("deployment_status") if ev.event == "deployment_status" else None
        await devops.on_github_deployment(
            db, settings, full, dep, status if isinstance(status, dict) else None,
            delivery_id=ev.delivery_id,
        )  # fmt: skip
    # issues, ping: recorded only (the stored event is the record).
    return None


def _now() -> datetime:
    return datetime.now(UTC)


def backoff(settings: Settings, attempts: int) -> timedelta:
    seconds = settings.GITHUB_WEBHOOK_RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1)
    return timedelta(seconds=min(seconds, MAX_BACKOFF_SECONDS))


@dataclass(frozen=True, slots=True)
class Processed:
    delivery_id: str
    event: str
    status: str  # "processed" | "pending" (will retry) | "failed"
    attempts: int
    error: str | None = None


def _error_text(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:MAX_ERROR]


async def process_next(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    delivery_id: str | None = None,
) -> Processed | None:
    """Process one due pending event (that delivery, or the oldest due). None = nothing to
    do (not found, not due, already done, or locked by another worker)."""
    failure: WorkflowFailure | None = None
    async with maker() as db, db.begin():
        q = (
            select(GitHubWebhookEvent)
            .where(
                GitHubWebhookEvent.status == "pending",
                GitHubWebhookEvent.next_attempt_at <= func.now(),
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if delivery_id is not None:
            q = q.where(GitHubWebhookEvent.delivery_id == delivery_id)
        else:
            q = q.order_by(GitHubWebhookEvent.next_attempt_at, GitHubWebhookEvent.id)
        ev = await db.scalar(q)
        if ev is None:
            return None
        # Plain values: the handler's savepoint may roll back, and the row is then
        # updated with a core statement (no ORM refresh of an expired object).
        ev_id, did, name = ev.id, ev.delivery_id, ev.event
        attempts = ev.attempts + 1
        error: str | None = None
        try:
            async with db.begin_nested():
                failure = await _handle(db, ev, settings)
        except Exception as exc:
            error = _error_text(exc)
            failure = None
        values: dict[str, Any] = {"attempts": attempts, "error": error}
        if error is None:
            values |= {"status": "processed", "processed_at": func.now()}
        elif attempts >= settings.GITHUB_WEBHOOK_MAX_ATTEMPTS:
            values |= {"status": "failed", "processed_at": func.now()}
        else:
            values["next_attempt_at"] = _now() + backoff(settings, attempts)
        await db.execute(
            update(GitHubWebhookEvent).where(GitHubWebhookEvent.id == ev_id).values(**values)
        )
        result = Processed(did, name, values.get("status", "pending"), attempts, error)
    if error is not None:
        log.warning(
            "github_webhook_failed" if result.status == "failed" else "github_webhook_retry",
            delivery_id=result.delivery_id,
            gh_event=result.event,
            attempts=result.attempts,
            error=error,
        )
    else:
        log.info("github_webhook_processed", delivery_id=result.delivery_id, gh_event=result.event)
    if failure is not None:
        await _fire(failure)
    return result


async def drain(
    maker: async_sessionmaker[AsyncSession], settings: Settings, limit: int = 50
) -> list[Processed]:
    """The DB fallback scan: process up to `limit` due events."""
    out: list[Processed] = []
    for _ in range(limit):
        res = await process_next(maker, settings)
        if res is None:
            break
        out.append(res)
    return out


__all__ = [
    "HANDLED",
    "QUEUE_KEY",
    "REJECTED_KEY",
    "FailureCallback",
    "Processed",
    "WorkflowFailure",
    "drain",
    "on_workflow_failed",
    "process_next",
    "remove_workflow_failed",
    "repo_allowed",
    "repo_of",
    "signature_for",
    "store",
    "trim_payload",
    "verify_signature",
]
