"""Approval requests (docs/19-github-issues-prs.md). Admin only; POSTs need CSRF.

GET  /api/v1/approvals?status=&kind=&limit=&offset=
GET  /api/v1/approvals/{id}                 with its events
POST /api/v1/approvals/{id}/approve         {"password": ...}: re-auth, then execute
POST /api/v1/approvals/{id}/deny            {"reason": ...}
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request

# Importing the GitHub ops / actions modules registers the github.* executors.
import app.services.github_actions
import app.services.github_ops  # noqa: F401
from app.auth.deps import AdminRequired, SettingsDep
from app.schemas.approvals import (
    ApprovalDetail,
    ApprovalEventOut,
    ApprovalOut,
    ApproveIn,
    DenyIn,
)
from app.schemas.common import Page
from app.services.approvals import ApprovalError, ApprovalGate
from app.services.audit import ClientInfo

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])

Status = Literal["pending", "approved", "denied", "expired", "invalidated", "executed", "failed"]


def _gate(request: Request, settings: SettingsDep) -> ApprovalGate:
    return ApprovalGate(request.app.state.sessionmaker, settings)


def _http(exc: ApprovalError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "detail": exc.message})


@router.get("", response_model=Page[ApprovalOut])
async def list_approvals(
    _admin: AdminRequired,
    request: Request,
    settings: SettingsDep,
    status: Status | None = None,
    kind: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ApprovalOut]:
    rows, total = await _gate(request, settings).list_requests(
        status=status, kind=kind, limit=limit, offset=offset
    )
    return Page(
        items=[ApprovalOut.model_validate(x) for x in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{approval_id}", response_model=ApprovalDetail)
async def get_approval(
    _admin: AdminRequired, request: Request, settings: SettingsDep, approval_id: uuid.UUID
) -> ApprovalDetail:
    try:
        req, events = await _gate(request, settings).get(approval_id)
    except ApprovalError as exc:
        raise _http(exc) from None
    return ApprovalDetail(
        **ApprovalOut.model_validate(req).model_dump(),
        events=[ApprovalEventOut.model_validate(e) for e in events],
    )


@router.post("/{approval_id}/approve", response_model=ApprovalOut)
async def approve(
    admin: AdminRequired,
    request: Request,
    settings: SettingsDep,
    approval_id: uuid.UUID,
    body: ApproveIn,
) -> ApprovalOut:
    """Re-verifies the admin's password, then executes. 200 with status `executed` or
    `failed`; 409 when expired, already used or invalidated by drift; 403 on a wrong
    password (audited) or a refused self-approval."""
    deps = {"github": getattr(request.app.state, "github", None)}
    try:
        req = await _gate(request, settings).approve(
            approval_id,
            admin.user,
            body.password,
            deps=deps,
            client=ClientInfo.from_request(request),
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
