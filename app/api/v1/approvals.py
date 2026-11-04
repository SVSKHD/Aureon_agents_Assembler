"""The approval engine's API (docs/19-github-issues-prs.md, docs/23-approval-engine.md). Admin
only; unsafe methods need CSRF.

GET    /api/v1/approvals?status=&kind=&limit=&offset=   page + counts by status
GET    /api/v1/approvals/inbox                         pending items this admin can act on
GET    /api/v1/approvals/stream                        SSE of approval events
GET    /api/v1/approvals/policies                      the policy table
PUT    /api/v1/approvals/policies                      upsert policies {password, policies}
POST   /api/v1/approvals/reauth                        {password}: stamp this session
DELETE /api/v1/approvals/reauth                        drop the stamp
GET    /api/v1/approvals/{id}?check_drift=             request + events, artifacts, policy,
                                                       drift and hash-chain status
GET    /api/v1/approvals/{id}/events                   events + chain verification
POST   /api/v1/approvals/{id}/approve                  {"password"} | {"use_reauth": true}
POST   /api/v1/approvals/{id}/deny                     {"reason"}
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

# Importing the GitHub ops / actions modules registers the github.* executors.
import app.services.github_actions
import app.services.github_ops  # noqa: F401
from app.api.ui import ui_module
from app.auth.deps import AdminRequired, DbSession, SettingsDep
from app.auth.security import verify_password
from app.models.approvals import ApprovalPolicy
from app.schemas.approvals import (
    ApprovalArtifactOut,
    ApprovalDetail,
    ApprovalEventOut,
    ApprovalEventsOut,
    ApprovalOut,
    ApprovalPage,
    ApproveIn,
    ChainOut,
    DenyIn,
    DriftOut,
    InboxItem,
    InboxOut,
    PoliciesPutIn,
    PolicyOut,
    ReauthIn,
    ReauthOut,
    StoredPolicyOut,
)
from app.services import approval_reauth, approval_stream, audit
from app.services.approvals import ApprovalError, ApprovalGate, Policy
from app.services.audit import ClientInfo

UI_MODULE = ui_module("approvals", label="Approvals", group="admin", icon="circle-check", order=60)
router = APIRouter(prefix="/v1/approvals", tags=["approvals"])

Status = Literal["pending", "approved", "denied", "expired", "invalidated", "executed", "failed"]


def _gate(request: Request, settings: SettingsDep) -> ApprovalGate:
    return ApprovalGate(request.app.state.sessionmaker, settings)


def _http(exc: ApprovalError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "detail": exc.message})


def approval_deps(request: Request) -> dict[str, Any]:
    """What executors may use: the GitHub client, Redis and the read-only trading adapters."""
    state = request.app.state
    return {
        "github": getattr(state, "github", None),
        "redis": getattr(state, "redis", None),
        "trading_adapters": getattr(state, "trading_adapters", {}),
    }


def _policy_out(p: Policy) -> PolicyOut:
    return PolicyOut(**p.snapshot())


@router.get("", response_model=ApprovalPage)
async def list_approvals(
    _admin: AdminRequired,
    request: Request,
    settings: SettingsDep,
    status: Status | None = None,
    kind: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApprovalPage:
    rows, total, counts = await _gate(request, settings).list_with_counts(
        status=status, kind=kind, limit=limit, offset=offset
    )
    return ApprovalPage(
        items=[ApprovalOut.model_validate(x) for x in rows],
        total=total,
        limit=limit,
        offset=offset,
        counts=counts,
    )


@router.get("/inbox", response_model=InboxOut)
async def inbox(admin: AdminRequired, request: Request, settings: SettingsDep) -> InboxOut:
    """Pending requests the current admin can approve or deny, newest first, with what
    approving each one needs right now."""
    rows = await _gate(request, settings).inbox(admin.user.id)
    redis = request.app.state.redis
    stamped = await approval_reauth.get_stamp(redis, admin.session.id, admin.user.id)
    now = datetime.now(UTC)
    items = []
    for req in rows:
        p = Policy.from_snapshot(req.policy)
        need: Literal["password", "session_stamp", "none"] = "password"
        if not p.require_reauth:
            need = "none"
        elif (
            stamped is not None
            and p.reauth_max_age_seconds > 0
            and now - stamped <= timedelta(seconds=p.reauth_max_age_seconds)
        ):
            need = "session_stamp"
        items.append(InboxItem(**ApprovalOut.model_validate(req).model_dump(), reauth=need))
    ttl = await approval_reauth.stamp_expires_in(redis, admin.session.id) if stamped else -2
    return InboxOut(
        items=items,
        total=len(items),
        reauth_stamp_expires_at=now + timedelta(seconds=ttl) if ttl > 0 else None,
    )


@router.get(
    "/stream",
    response_class=StreamingResponse,
    status_code=200,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream(
    admin: AdminRequired,
    request: Request,
    settings: SettingsDep,
    last_event_id: Annotated[str | None, Header(max_length=64)] = None,
    cursor: Annotated[str | None, Query(alias="last_event_id", max_length=64)] = None,
) -> StreamingResponse:
    """SSE: one `approval` event per approval state change (see app/services/
    approval_stream.py). Resumes after `Last-Event-ID` or `?last_event_id=`."""
    gen = approval_stream.events(
        request.app.state.sessionmaker,
        admin.session.id,
        admin.user.id,
        last_event_id=last_event_id or cursor,
        poll_seconds=settings.APPROVALS_STREAM_POLL_SECONDS,
        heartbeat_seconds=settings.APPROVALS_STREAM_HEARTBEAT_SECONDS,
        max_seconds=settings.APPROVALS_STREAM_MAX_SECONDS,
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/policies", response_model=list[StoredPolicyOut])
async def list_policies(_admin: AdminRequired, db: DbSession) -> list[StoredPolicyOut]:
    rows = (await db.scalars(select(ApprovalPolicy).order_by(ApprovalPolicy.kind_pattern))).all()
    return [StoredPolicyOut.model_validate(r) for r in rows]


_POLICY_FIELDS = (
    "required_approvals",
    "expiry_seconds",
    "allow_self_approval",
    "require_reauth",
    "reauth_max_age_seconds",
    "notify_channels",
    "risk_level",
    "description",
    "enabled",
)


@router.put("/policies", response_model=list[StoredPolicyOut])
async def put_policies(
    admin: AdminRequired, request: Request, db: DbSession, body: PoliciesPutIn
) -> list[StoredPolicyOut]:
    """Upsert policies by kind_pattern (audited with before/after). Needs the admin's
    password: a policy can switch re-auth off. Pending requests keep the policy they were
    filed under."""
    client = ClientInfo.from_request(request)
    user = admin.user
    if not await asyncio.to_thread(verify_password, user.password_hash, body.password):
        audit.record(db, "approval_policies_update_refused", client, actor_user_id=user.id)
        await db.commit()
        raise HTTPException(
            403, {"code": "reauth_failed", "detail": "password re-authentication failed"}
        )
    patterns = [p.kind_pattern for p in body.policies]
    if len(set(patterns)) != len(patterns):
        raise HTTPException(422, {"code": "duplicate_pattern", "detail": "patterns must differ"})
    existing = {
        r.kind_pattern: r
        for r in (
            await db.scalars(
                select(ApprovalPolicy)
                .where(ApprovalPolicy.kind_pattern.in_(patterns))
                .with_for_update()
            )
        ).all()
    }
    changes = []
    now = datetime.now(UTC)
    for p in body.policies:
        row = existing.get(p.kind_pattern)
        after = {k: getattr(p, k) for k in _POLICY_FIELDS}
        before = {k: getattr(row, k) for k in _POLICY_FIELDS} if row is not None else None
        if row is None:
            row = ApprovalPolicy(kind_pattern=p.kind_pattern)
            db.add(row)
        for k, v in after.items():
            setattr(row, k, v)
        row.updated_by, row.updated_at = user.id, now
        if before != after:
            changes.append({"kind_pattern": p.kind_pattern, "before": before, "after": after})
    audit.record(
        db, "approval_policies_updated", client, actor_user_id=user.id, details={"changes": changes}
    )
    await db.commit()
    rows = (await db.scalars(select(ApprovalPolicy).order_by(ApprovalPolicy.kind_pattern))).all()
    return [StoredPolicyOut.model_validate(r) for r in rows]


@router.post("/reauth", response_model=ReauthOut)
async def reauth(
    admin: AdminRequired, request: Request, db: DbSession, settings: SettingsDep, body: ReauthIn
) -> ReauthOut:
    """Verify the password once and stamp this login session (server-side, in Redis). Requests
    whose policy has reauth_max_age_seconds > 0 can then be approved with
    {"use_reauth": true} while the stamp is young enough."""
    redis = request.app.state.redis
    client = ClientInfo.from_request(request)
    sid, user = admin.session.id, admin.user
    if await approval_reauth.failures(redis, sid) >= settings.APPROVALS_MAX_REAUTH_FAILURES:
        raise HTTPException(429, {"code": "too_many_reauth_failures", "detail": "try again later"})
    ttl = await approval_reauth.stamp_ttl(db, settings)
    if not await asyncio.to_thread(verify_password, user.password_hash, body.password):
        await approval_reauth.count_failure(redis, sid)
        audit.record(db, "approval_reauth_stamp_failed", client, actor_user_id=user.id)
        await db.commit()
        raise HTTPException(
            403, {"code": "reauth_failed", "detail": "password re-authentication failed"}
        )
    if ttl <= 0:
        raise HTTPException(
            409,
            {"code": "reauth_stamp_disabled", "detail": "no policy accepts a session re-auth"},
        )
    at = await approval_reauth.set_stamp(redis, sid, user.id, ttl)
    audit.record(
        db, "approval_reauth_stamped", client, actor_user_id=user.id, details={"ttl_seconds": ttl}
    )
    await db.commit()
    return ReauthOut(stamped_at=at, expires_at=at + timedelta(seconds=ttl), ttl_seconds=ttl)


@router.delete("/reauth", status_code=204)
async def clear_reauth(admin: AdminRequired, request: Request) -> None:
    await approval_reauth.clear_stamp(request.app.state.redis, admin.session.id)


@router.get("/{approval_id}", response_model=ApprovalDetail)
async def get_approval(
    admin: AdminRequired,
    request: Request,
    settings: SettingsDep,
    approval_id: uuid.UUID,
    check_drift: bool = False,
) -> ApprovalDetail:
    """With `check_drift=true` the artifacts of a pending request are recomputed now (this may
    call GitHub) and compared with what was filed."""
    try:
        d = await _gate(request, settings).detail(
            approval_id,
            deps=approval_deps(request),
            actor_user_id=admin.user.id,
            check_drift=check_drift,
        )
    except ApprovalError as exc:
        raise _http(exc) from None
    return ApprovalDetail(
        **ApprovalOut.model_validate(d.request).model_dump(),
        events=[ApprovalEventOut.model_validate(e) for e in d.events],
        artifacts=[ApprovalArtifactOut.model_validate(a) for a in d.artifacts],
        policy=_policy_out(d.policy),
        drift=DriftOut(status=d.drift.status, drifted=d.drift.drifted, detail=d.drift.detail),
        chain=ChainOut(**vars_of(d.chain)),
        approvers=d.approvers,
    )


def vars_of(obj: Any) -> dict[str, Any]:
    return {k: getattr(obj, k) for k in obj.__dataclass_fields__}


@router.get("/{approval_id}/events", response_model=ApprovalEventsOut)
async def get_events(
    _admin: AdminRequired, request: Request, settings: SettingsDep, approval_id: uuid.UUID
) -> ApprovalEventsOut:
    """The events in order and the hash-chain verification (`chain.status` ok | broken |
    legacy | empty)."""
    try:
        d = await _gate(request, settings).detail(approval_id)
    except ApprovalError as exc:
        raise _http(exc) from None
    return ApprovalEventsOut(
        approval_id=d.request.id,
        events=[ApprovalEventOut.model_validate(e) for e in d.events],
        chain=ChainOut(**vars_of(d.chain)),
    )


@router.post("/{approval_id}/approve", response_model=ApprovalOut)
async def approve(
    admin: AdminRequired,
    request: Request,
    settings: SettingsDep,
    approval_id: uuid.UUID,
    body: ApproveIn,
) -> ApprovalOut:
    """Re-authenticates (the password, or this session's re-auth stamp when the policy accepts
    one), counts the approval and, once the policy's quorum is reached, executes. 200 with
    status `pending` (more approvals needed), `executed` or `failed`; 409 when expired,
    already used, already approved by you or invalidated by drift; 403 on a wrong or missing
    re-auth (audited) or a refused self-approval."""
    stamped = None
    if body.password is None and body.use_reauth:
        stamped = await approval_reauth.get_stamp(
            request.app.state.redis, admin.session.id, admin.user.id
        )
    try:
        req = await _gate(request, settings).approve(
            approval_id,
            admin.user,
            body.password,
            deps=approval_deps(request),
            client=ClientInfo.from_request(request),
            reauth_stamped_at=stamped,
        )
    except ApprovalError as exc:
        raise _http(exc) from None
    return ApprovalOut.model_validate(req)


@router.post("/{approval_id}/deny", response_model=ApprovalOut)
async def deny(
    admin: AdminRequired,
    request: Request,
    settings: SettingsDep,
    approval_id: uuid.UUID,
    body: DenyIn | None = None,
) -> ApprovalOut:
    try:
        req = await _gate(request, settings).deny(
            approval_id,
            admin.user,
            client=ClientInfo.from_request(request),
            reason=body.reason if body else None,
        )
    except ApprovalError as exc:
        raise _http(exc) from None
    return ApprovalOut.model_validate(req)
