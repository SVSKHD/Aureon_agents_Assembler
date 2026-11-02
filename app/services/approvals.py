"""ApprovalGate: the minimal approval engine (docs/19-github-issues-prs.md).

A gated operation never runs directly. The caller files a request:

    req = await gate.request(kind, target, payload, artifact, requested_by=..., client=..., db=db)

which is stored as `pending` in audit.approval_requests with `expires_at` (APPROVALS_TTL_MINUTES)
and `artifact_sha256`, the sha256 of what the approver is shown (for a merge: the head sha plus
the diff hash). An admin then calls

    await gate.approve(id, admin, reauth_password, deps=..., client=...)   # executes
    await gate.deny(id, admin, client=...)

`approve` re-verifies the admin's password server-side (a session cookie alone is not enough),
moves the request out of `pending` (single use: a second approve is a 409), re-computes the
artifact with the kind's `current_artifact` and, on any drift, marks it `invalidated` without
running anything. Only then the executor registered for the kind runs; the request ends
`executed` or `failed` with its `result`. Every step is an audit.approval_events row and an
audit.audit_events row.

Phase 20 grows this into the full engine (policies, artifacts tables, notifications) behind the
same `request` / `approve` / `deny` interface and the same executor registry.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import verify_password
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.approvals import ApprovalEvent, ApprovalRequest
from app.models.auth import ROLE_ADMIN, Role, User
from app.services import audit
from app.services.audit import ClientInfo

log = get_logger("services.approvals")

TERMINAL = frozenset({"denied", "expired", "invalidated", "executed", "failed"})
MAX_RESULT_TEXT = 2000


class ApprovalError(Exception):
    """An approval call that cannot proceed. `status` is the HTTP status to answer."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ExecContext:
    """What an executor gets: the DB, settings, named dependencies (e.g. "github") and who
    approved."""

    maker: async_sessionmaker[AsyncSession]
    settings: Settings
    deps: Mapping[str, Any]
    actor_user_id: uuid.UUID
    client: ClientInfo


ArtifactFn = Callable[[ExecContext, ApprovalRequest], Awaitable[bytes | str | Mapping[str, Any]]]
ExecuteFn = Callable[[ExecContext, ApprovalRequest], Awaitable[dict[str, Any]]]
StatusHook = Callable[[AsyncSession, ApprovalRequest], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ExecutorSpec:
    kind: str
    execute: ExecuteFn
    # Re-computes the artifact right before execution; a different sha256 invalidates.
    current_artifact: ArtifactFn | None = None
    # Called inside the transaction of every status change (e.g. to mirror the status into
    # github.tracked_operations).
    on_status: StatusHook | None = None
    # deps keys that must be present; checked before the request is consumed.
    requires: tuple[str, ...] = ()


@dataclass(slots=True)
class ExecutorRegistry:
    _specs: dict[str, ExecutorSpec] = field(default_factory=dict)

    def register(self, spec: ExecutorSpec) -> None:
        self._specs[spec.kind] = spec

    def get(self, kind: str) -> ExecutorSpec | None:
        return self._specs.get(kind)

    def kinds(self) -> list[str]:
        return sorted(self._specs)


REGISTRY = ExecutorRegistry()


def artifact_sha256(artifact: bytes | str | Mapping[str, Any] | None) -> str | None:
    """sha256 hex of an artifact. Mappings are hashed as canonical JSON (sorted keys)."""
    if artifact is None:
        return None
    if isinstance(artifact, Mapping):
        data = json.dumps(artifact, sort_keys=True, separators=(",", ":"), default=str).encode()
    elif isinstance(artifact, str):
        data = artifact.encode()
    else:
        data = bytes(artifact)
    return hashlib.sha256(data).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _error_text(exc: BaseException) -> str:
    """Never str() an arbitrary exception into the DB: only already-redacted messages."""
    msg = getattr(exc, "message", None)
    code = getattr(exc, "code", None)
    text = f"{code}: {msg}" if code and isinstance(msg, str) else type(exc).__name__
    return text[:MAX_RESULT_TEXT]


class ApprovalGate:
    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        settings: Settings,
        registry: ExecutorRegistry = REGISTRY,
    ) -> None:
        self.maker = maker
        self.settings = settings
        self.registry = registry

    # --- helpers ----------------------------------------------------------------------------

    def _event(
        self,
        db: AsyncSession,
        req: ApprovalRequest,
        event: str,
        actor: uuid.UUID | None,
        client: ClientInfo,
        details: dict[str, Any] | None = None,
    ) -> None:
        db.add(ApprovalEvent(request_id=req.id, event=event, actor=actor, details=details or {}))
        audit.record(
            db,
            f"approval_{event}",
            client,
            actor_user_id=actor,
            details={"approval_id": str(req.id), "kind": req.kind, "target": req.target}
            | (details or {}),
        )

    async def _set_status(
        self,
        db: AsyncSession,
        req: ApprovalRequest,
        status: str,
        actor: uuid.UUID | None,
        client: ClientInfo,
        details: dict[str, Any] | None = None,
    ) -> None:
        req.status = status
        self._event(db, req, status, actor, client, details)
        await db.flush()
        spec = self.registry.get(req.kind)
        if spec is not None and spec.on_status is not None:
            await spec.on_status(db, req)

    async def _expire_if_due(self, db: AsyncSession, req: ApprovalRequest) -> bool:
        if req.status == "pending" and req.expires_at <= _now():
            await self._set_status(db, req, "expired", None, ClientInfo())
            return True
        return False

    async def _only_admin(self, db: AsyncSession, user_id: uuid.UUID) -> bool:
        ids = (
            await db.scalars(
                select(User.id)
                .join(Role, User.role_id == Role.id)
                .where(Role.name == ROLE_ADMIN, User.is_active.is_(True))
                .limit(2)
            )
        ).all()
        return list(ids) == [user_id]

    # --- interface --------------------------------------------------------------------------

    async def request(
        self,
        kind: str,
        target: str,
        payload: dict[str, Any],
        artifact: bytes | str | Mapping[str, Any] | None,
        *,
        requested_by: uuid.UUID | None,
        client: ClientInfo,
        db: AsyncSession,
    ) -> ApprovalRequest:
        """File a pending request in the caller's transaction (the caller commits)."""
        if self.registry.get(kind) is None:
            raise ApprovalError(500, "unknown_approval_kind", f"no executor for {kind!r}")
        req = ApprovalRequest(
            kind=kind,
            target=target[:300],
            payload=payload,
            artifact_sha256=artifact_sha256(artifact),
            status="pending",
            requested_by=requested_by,
            expires_at=_now() + timedelta(minutes=self.settings.APPROVALS_TTL_MINUTES),
        )
        db.add(req)
        await db.flush()
        self._event(
            db,
            req,
            "requested",
            requested_by,
            client,
            {"artifact_sha256": req.artifact_sha256, "expires_at": req.expires_at.isoformat()},
        )
        log.info("approval_requested", approval_id=str(req.id), kind=kind, target=target)
        return req

    async def approve(
        self,
        request_id: uuid.UUID,
        admin: User,
        reauth_password: str,
        *,
        deps: Mapping[str, Any],
        client: ClientInfo,
    ) -> ApprovalRequest:
        """Re-auth, consume, re-check the artifact, execute. Raises ApprovalError (403/404/409/
        503); an executor failure is not raised but recorded as status `failed`."""
        outcome: ApprovalError | None = None
        async with self.maker() as db, db.begin():
            req = await db.get(ApprovalRequest, request_id, with_for_update=True)
            if req is None:
                raise ApprovalError(404, "not_found", "approval request not found")
            spec = self.registry.get(req.kind)
            ok = await asyncio.to_thread(verify_password, admin.password_hash, reauth_password)
            if not ok:
                self._event(db, req, "reauth_failed", admin.id, client)
                await db.flush()
                failures = await db.scalar(
                    select(func.count())
                    .select_from(ApprovalEvent)
                    .where(
                        ApprovalEvent.request_id == req.id, ApprovalEvent.event == "reauth_failed"
                    )
                )
                if req.status == "pending" and (failures or 0) >= (
                    self.settings.APPROVALS_MAX_REAUTH_FAILURES
                ):
                    req.decided_by, req.decided_at = admin.id, _now()
                    await self._set_status(
                        db, req, "denied", admin.id, client, {"reason": "too many re-auth failures"}
                    )
                outcome = ApprovalError(403, "reauth_failed", "password re-authentication failed")
            elif await self._expire_if_due(db, req):
                outcome = ApprovalError(409, "approval_expired", "approval request has expired")
            elif req.status != "pending":
                code = (
                    "approval_already_used"
                    if req.status in ("approved", "executed", "failed")
                    else "approval_not_pending"
                )
                outcome = ApprovalError(409, code, f"approval request is {req.status}")
            elif spec is None:
                outcome = ApprovalError(409, "unknown_approval_kind", f"no executor for {req.kind}")
            elif missing := [k for k in spec.requires if deps.get(k) is None]:
                outcome = ApprovalError(
                    503, f"{missing[0]}_not_configured", f"{missing[0]} is not configured"
                )
            elif req.requested_by == admin.id and not (
                self.settings.APPROVALS_ALLOW_SELF_APPROVAL and await self._only_admin(db, admin.id)
            ):
                self._event(db, req, "self_approval_refused", admin.id, client)
                outcome = ApprovalError(
                    403, "self_approval_not_allowed", "another admin must approve this request"
                )
            else:
                req.decided_by, req.decided_at = admin.id, _now()
                await self._set_status(db, req, "approved", admin.id, client)
        if outcome is not None:
            raise outcome
        assert spec is not None

        ctx = ExecContext(self.maker, self.settings, deps, admin.id, client)
        status, result, details = "executed", {}, {}
        try:
            if spec.current_artifact is not None and req.artifact_sha256 is not None:
                current = artifact_sha256(await spec.current_artifact(ctx, req))
                if current != req.artifact_sha256:
                    status = "invalidated"
                    result = {"reason": "artifact_drift"}
                    details = {"expected": req.artifact_sha256, "actual": current}
            if status == "executed":
                result = await spec.execute(ctx, req)
        except Exception as exc:  # recorded as `failed`; the raw text never leaves
            status, result = "failed", {"error": _error_text(exc)}
            log.warning("approval_execution_failed", approval_id=str(req.id), error=result["error"])

        async with self.maker() as db, db.begin():
            row = await db.get(ApprovalRequest, req.id, with_for_update=True)
            assert row is not None
            row.result = result
            if status == "executed":
                row.executed_at = _now()
            await self._set_status(db, row, status, admin.id, client, details)
            req = row
        if status == "invalidated":
            raise ApprovalError(
                409,
                "approval_invalidated",
                "the artifact changed since the request was made; file a new request",
            )
        return req

    async def deny(
        self,
        request_id: uuid.UUID,
        admin: User,
        *,
        client: ClientInfo,
        reason: str | None = None,
    ) -> ApprovalRequest:
        async with self.maker() as db, db.begin():
            req = await db.get(ApprovalRequest, request_id, with_for_update=True)
            if req is None:
                raise ApprovalError(404, "not_found", "approval request not found")
            expired = await self._expire_if_due(db, req)
            if not expired and req.status == "pending":
                req.decided_by, req.decided_at = admin.id, _now()
                await self._set_status(
                    db, req, "denied", admin.id, client, {"reason": reason} if reason else None
                )
                return req
        raise ApprovalError(409, "approval_not_pending", f"approval request is {req.status}")

    async def expire_due(self, db: AsyncSession, limit: int = 500) -> int:
        rows = (
            await db.scalars(
                select(ApprovalRequest)
                .where(ApprovalRequest.status == "pending", ApprovalRequest.expires_at <= _now())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        ).all()
        for req in rows:
            await self._set_status(db, req, "expired", None, ClientInfo())
        return len(rows)

    async def get(self, request_id: uuid.UUID) -> tuple[ApprovalRequest, list[ApprovalEvent]]:
        async with self.maker() as db, db.begin():
            req = await db.get(ApprovalRequest, request_id, with_for_update=True)
            if req is None:
                raise ApprovalError(404, "not_found", "approval request not found")
            await self._expire_if_due(db, req)
            events = (
                await db.scalars(
                    select(ApprovalEvent)
                    .where(ApprovalEvent.request_id == req.id)
                    .order_by(ApprovalEvent.id)
                )
            ).all()
            return req, list(events)

    async def list_requests(
        self, *, status: str | None, kind: str | None, limit: int, offset: int
    ) -> tuple[list[ApprovalRequest], int]:
        async with self.maker() as db, db.begin():
            await self.expire_due(db)
        async with self.maker() as db:
            q = select(ApprovalRequest)
            if status:
                q = q.where(ApprovalRequest.status == status)
            if kind:
                q = q.where(ApprovalRequest.kind == kind)
            total = await db.scalar(select(func.count()).select_from(q.subquery()))
            rows = (
                await db.scalars(
                    q.order_by(ApprovalRequest.created_at.desc()).limit(limit).offset(offset)
                )
            ).all()
            return list(rows), total or 0


__all__ = [
    "REGISTRY",
    "TERMINAL",
    "ApprovalError",
    "ApprovalGate",
    "ExecContext",
    "ExecutorRegistry",
    "ExecutorSpec",
    "artifact_sha256",
]
