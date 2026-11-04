"""Trading Phase F: paper order intents, approval and the paper book.

    POST /api/trading/order-intents                    create (idempotent on correlation_id)
    GET  /api/trading/order-intents                    list (visible accounts only)
    GET  /api/trading/order-intents/{id}               intent + events + risk decisions + fills
    POST /api/trading/order-intents/{id}/approve       admin: through the approval engine
                                                       (kind trading.paper_intent): 202 +
                                                       approval id, or with {"password"} and
                                                       a single-approval policy, approve now
    POST /api/trading/order-intents/{id}/cancel        creator or admin, while still open
    GET  /api/trading/accounts/{id}/paper              paper book: equity, P&L, positions
    POST /api/trading/accounts/{id}/paper/reconcile    admin: positions vs ledger, now
    GET  /api/trading/accounts/{id}/paper/reconciliations

Simulated fills are listed by the existing GET /api/trading/executions (source=simulated).
Nothing here can place a broker order: there is no such code path in this build.

Approval (docs/23-approval-engine.md): approving a paper intent is an approval request of kind
`trading.paper_intent`. Its artifacts are the intent's inputs (account, instrument, side,
type, quantity, prices, product, mode, status) and its latest risk decision; any change
between filing and approval invalidates it. Approving executes the unchanged
`paper.approve_and_execute` (risk re-run, then the simulated fill). PAPER only: a LIVE intent
or account is refused before anything is filed, and there is no live kind.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agents.trading.base import BrokerAdapter
from app.api.ui import ui_module
from app.api.v1.approvals import approval_deps
from app.auth.deps import AdminRequired, CsrfProtected, Current, DbSession, SettingsDep
from app.models.approvals import ApprovalRequest
from app.models.trading import (
    BrokerAccount,
    Execution,
    OrderEvent,
    OrderIntent,
    Reconciliation,
    RiskDecision,
)
from app.schemas.approvals import ApprovalFiledOut
from app.schemas.common import Page
from app.schemas.paper import (
    OrderEventOut,
    OrderIntentDetail,
    OrderIntentIn,
    OrderIntentOut,
    PaperSummaryOut,
    ReconciliationOut,
)
from app.schemas.trading import ExecutionOut, RiskDecisionOut
from app.services import audit, paper
from app.services.approvals import (
    REGISTRY,
    ApprovalError,
    ApprovalGate,
    ExecContext,
    ExecutorSpec,
)
from app.services.audit import ClientInfo
from app.services.trading import count, get_visible_account, is_admin, visible_account_ids

UI_MODULE = ui_module(
    "paper", label="Paper trading", group="markets", icon="flask-conical", order=22
)
router = APIRouter(prefix="/trading", tags=["paper trading"])

Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]


def _adapters(request: Request) -> dict[str, BrokerAdapter]:
    adapters: dict[str, BrokerAdapter] = getattr(request.app.state, "trading_adapters", {})
    return adapters


def _refused(exc: paper.IntentRefused) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "detail": exc.message})


async def _visible_intent(
    db: DbSession, current: Any, intent_id: uuid.UUID, lock: bool = False
) -> OrderIntent:
    intent = await db.get(OrderIntent, intent_id, with_for_update=lock)
    if intent is None or intent.broker_account_id not in await visible_account_ids(db, current):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "order intent not found")
    return intent


def _execution_out(e: Execution) -> ExecutionOut:
    return ExecutionOut.model_validate(
        {
            **{c: getattr(e, c) for c in ExecutionOut.model_fields if c != "source"},
            "source": "simulated" if e.is_simulated else "broker",
        }
    )


async def _detail(db: DbSession, intent: OrderIntent) -> OrderIntentDetail:
    events = (
        await db.scalars(
            select(OrderEvent)
            .where(OrderEvent.order_intent_id == intent.id)
            .order_by(OrderEvent.occurred_at, OrderEvent.id)
        )
    ).all()
    decisions = (
        await db.scalars(
            select(RiskDecision)
            .where(RiskDecision.order_intent_id == intent.id)
            .order_by(RiskDecision.id)
        )
    ).all()
    fills = (
        await db.scalars(
            select(Execution)
            .where(Execution.order_intent_id == intent.id)
            .order_by(Execution.executed_at)
        )
    ).all()
    return OrderIntentDetail.model_validate(
        {
            **OrderIntentOut.model_validate(intent).model_dump(),
            "events": [OrderEventOut.model_validate(e) for e in events],
            "risk_decisions": [RiskDecisionOut.model_validate(d) for d in decisions],
            "executions": [_execution_out(e) for e in fills],
        }
    )


@router.post("/order-intents", response_model=OrderIntentDetail)
async def create_order_intent(
    body: OrderIntentIn,
    request: Request,
    response: Response,
    db: DbSession,
    current: CsrfProtected,
    settings: SettingsDep,
) -> OrderIntentDetail:
    """201 with the new intent (already risk-checked: PENDING_RISK or REJECTED), or 200 with
    the existing one when this correlation_id was already used for the same request."""
    user_id = current.user.id  # read before any rollback expires the ORM objects
    account = await get_visible_account(db, current, body.account_id)
    req = paper.IntentRequest(
        account_id=account.id,
        instrument_id=body.instrument_id,
        side=body.side,
        order_type=body.order_type,
        quantity=body.quantity,
        limit_price=body.limit_price,
        product=body.product,
        signal_id=body.signal_id,
        correlation_id=body.correlation_id,
    )
    try:
        intent, created = await paper.create_intent(
            db,
            request.app.state.redis,
            _adapters(request),
            settings,
            account,
            req,
            user_id,
        )
        if created:
            audit.record(
                db,
                "order_intent_created",
                ClientInfo.from_request(request),
                actor_user_id=user_id,
                details={
                    "intent": str(intent.id),
                    "account": str(account.id),
                    "status": intent.status,
                    "correlation_id": intent.correlation_id,
                },
            )
        await db.commit()
    except paper.IntentRefused as exc:
        await db.rollback()
        raise _refused(exc) from exc
    except IntegrityError:
        # Lost a race on the same correlation_id: answer like the idempotent repeat.
        await db.rollback()
        # Visibility was checked above; reload by id (the rollback expired the ORM objects).
        reloaded = await db.get(BrokerAccount, req.account_id)
        assert reloaded is not None
        account = reloaded
        try:
            intent, created = await paper.create_intent(
                db, None, {}, settings, account, req, user_id
            )
        except paper.IntentRefused as exc:
            raise _refused(exc) from exc
        if created:  # pragma: no cover - the unique row exists, so this cannot happen
            await db.rollback()
            raise HTTPException(status.HTTP_409_CONFLICT, "retry the request") from None
    await db.refresh(intent)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return await _detail(db, intent)


@router.get("/order-intents", response_model=Page[OrderIntentOut])
async def list_order_intents(
    db: DbSession,
    current: Current,
    account_id: uuid.UUID | None = None,
    status_: Annotated[str | None, Query(alias="status", max_length=16)] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[OrderIntentOut]:
    allowed = await visible_account_ids(db, current)
    if account_id is not None:
        if account_id not in allowed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
        allowed = [account_id]
    stmt = select(OrderIntent).where(OrderIntent.broker_account_id.in_(allowed))
    if status_:
        stmt = stmt.where(OrderIntent.status == status_.upper())
    rows = (
        await db.scalars(
            stmt.order_by(OrderIntent.created_at.desc(), OrderIntent.id).limit(limit).offset(offset)
        )
    ).all()
    return Page[OrderIntentOut](
        items=[OrderIntentOut.model_validate(r) for r in rows],
        total=await count(db, stmt),
        limit=limit,
        offset=offset,
    )


@router.get("/order-intents/{intent_id}", response_model=OrderIntentDetail)
async def get_order_intent(
    intent_id: uuid.UUID, db: DbSession, current: Current
) -> OrderIntentDetail:
    return await _detail(db, await _visible_intent(db, current, intent_id))


# --- approval through the engine -----------------------------------------------------------

PAPER_KIND = "trading.paper_intent"


class IntentApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The admin's password. With it (and a single-approval policy) the request is approved in
    # this call; without it the call only files the request (202).
    password: str | None = Field(default=None, min_length=1, max_length=1024)


async def intent_artifacts(db: Any, intent: OrderIntent) -> dict[str, dict[str, Any]]:
    """What the approver is shown: the intent's inputs and its latest risk decision."""
    latest = await db.scalar(
        select(RiskDecision)
        .where(RiskDecision.order_intent_id == intent.id)
        .order_by(RiskDecision.id.desc())
        .limit(1)
    )
    return {
        "intent": {
            "intent_id": str(intent.id),
            "account_id": str(intent.broker_account_id),
            "instrument_id": intent.instrument_id,
            "side": intent.side,
            "order_type": intent.order_type,
            "quantity": str(intent.quantity),
            "limit_price": None if intent.limit_price is None else str(intent.limit_price),
            "stop_price": None if intent.stop_price is None else str(intent.stop_price),
            "product": intent.product,
            "mode": intent.mode,
            "status": intent.status,
        },
        "risk_decision": {
            "id": latest.id if latest is not None else None,
            "approved": latest.approved if latest is not None else None,
        },
    }


async def _current_paper_artifacts(
    ctx: ExecContext, req: ApprovalRequest
) -> dict[str, dict[str, Any]]:
    async with ctx.maker() as db:
        intent = await db.get(OrderIntent, uuid.UUID(str(req.payload["intent_id"])))
        if intent is None:
            return {"intent": {"missing": True}}
        return await intent_artifacts(db, intent)


async def _execute_paper(ctx: ExecContext, req: ApprovalRequest) -> dict[str, Any]:
    """The approved request runs the unchanged Phase F path: risk re-run, then the simulated
    fill. A refusal is audited as before and recorded as the request's failure."""
    intent_id = uuid.UUID(str(req.payload["intent_id"]))
    adapters: dict[str, BrokerAdapter] = dict(ctx.deps.get("trading_adapters") or {})
    redis = ctx.deps.get("redis")
    async with ctx.maker() as db:
        intent = await db.get(OrderIntent, intent_id, with_for_update=True)
        if intent is None:
            raise paper.IntentRefused("not_found", "order intent not found", 404)
        try:
            await paper.approve_and_execute(
                db, redis, adapters, ctx.settings, intent, ctx.actor_user_id
            )
        except paper.IntentRefused as exc:
            await db.rollback()
            audit.record(
                db,
                "order_intent_approve_denied",
                ctx.client,
                actor_user_id=ctx.actor_user_id,
                details={"intent": str(intent_id), "code": exc.code, "approval_id": str(req.id)},
            )
            await db.commit()
            raise
        status_ = intent.status
        audit.record(
            db,
            "order_intent_approved" if status_ != "REJECTED" else "order_intent_rejected",
            ctx.client,
            actor_user_id=ctx.actor_user_id,
            details={"intent": str(intent_id), "status": status_, "approval_id": str(req.id)},
        )
        await db.commit()
        return {"intent_id": str(intent_id), "intent_status": status_}


REGISTRY.register(
    ExecutorSpec(
        kind=PAPER_KIND,
        execute=_execute_paper,
        current_artifacts=_current_paper_artifacts,
    )
)


async def _pending_request(db: DbSession, intent_id: uuid.UUID) -> ApprovalRequest | None:
    req: ApprovalRequest | None = await db.scalar(
        select(ApprovalRequest)
        .where(
            ApprovalRequest.kind == PAPER_KIND,
            ApprovalRequest.status == "pending",
            ApprovalRequest.expires_at > datetime.now(UTC),
            ApprovalRequest.payload["intent_id"].astext == str(intent_id),
        )
        .order_by(ApprovalRequest.created_at.desc())
        .limit(1)
    )
    return req


def _filed(req: ApprovalRequest) -> JSONResponse:
    out = ApprovalFiledOut(
        approval_id=req.id,
        status=req.status,
        kind=req.kind,
        target=req.target,
        artifact_sha256=req.artifact_sha256,
        expires_at=req.expires_at,
        required_approvals=req.required_approvals,
        received_approvals=req.received_approvals,
    )
    return JSONResponse(out.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)


@router.post(
    "/order-intents/{intent_id}/approve",
    response_model=OrderIntentDetail,
    responses={202: {"model": ApprovalFiledOut}},
)
async def approve_order_intent(
    intent_id: uuid.UUID,
    request: Request,
    db: DbSession,
    admin: AdminRequired,
    settings: SettingsDep,
    body: IntentApproveIn | None = None,
) -> Any:
    """Backward-compatible wrapper over the approval engine. Files (or reuses) the pending
    `trading.paper_intent` request for this intent. With {"password"} and a policy needing a
    single approval, approves it now: 200 with the intent (FILLED, SUBMITTED or REJECTED by
    risk). Otherwise 202 with the approval id, to approve from the inbox."""
    admin_id = admin.user.id  # read before any rollback expires the ORM objects
    intent = await _visible_intent(db, admin, intent_id)
    client = ClientInfo.from_request(request)
    account = await db.get(BrokerAccount, intent.broker_account_id)
    refusal: paper.IntentRefused | None = None
    if intent.mode == "LIVE" or account is None or account.mode == "LIVE":
        refusal = paper.LiveUnreachable()
    elif intent.status != "PENDING_RISK":
        refusal = paper.IntentRefused("not_pending", f"intent is {intent.status}")
    if refusal is not None:
        audit.record(
            db,
            "order_intent_approve_denied",
            client,
            actor_user_id=admin_id,
            details={"intent": str(intent_id), "code": refusal.code},
        )
        await db.commit()
        raise _refused(refusal)
    gate = ApprovalGate(request.app.state.sessionmaker, settings)
    req = await _pending_request(db, intent.id)
    if req is None:
        qty, price = intent.quantity, intent.limit_price
        req = await gate.request(
            PAPER_KIND,
            f"intent:{intent.id}",
            {"intent_id": str(intent.id), "account_id": str(intent.broker_account_id)},
            None,
            artifacts=await intent_artifacts(db, intent),
            requested_by=intent.created_by,
            client=client,
            db=db,
            summary=(
                f"Paper {intent.side} {qty} of instrument {intent.instrument_id} "
                f"({intent.order_type}{'' if price is None else f' @ {price}'})"
            ),
        )
    await db.commit()
    if body is None or body.password is None or req.required_approvals > 1:
        return _filed(req)
    try:
        done = await gate.approve(
            req.id, admin.user, body.password, deps=approval_deps(request), client=client
        )
    except ApprovalError as exc:
        raise HTTPException(
            exc.status, {"code": exc.code, "detail": exc.message, "approval_id": str(req.id)}
        ) from None
    if done.status == "failed":
        res = done.result or {}
        raise HTTPException(
            int(res.get("http_status") or 409),
            {
                "code": str(res.get("code") or "approval_failed"),
                "detail": str(res.get("error") or "execution failed"),
                "approval_id": str(req.id),
            },
        )
    if done.status != "executed":  # quorum not reached (cannot happen for a single approval)
        return _filed(done)
    await db.refresh(intent)
    return await _detail(db, intent)


@router.post("/order-intents/{intent_id}/cancel", response_model=OrderIntentDetail)
async def cancel_order_intent(
    intent_id: uuid.UUID, request: Request, db: DbSession, current: CsrfProtected
) -> OrderIntentDetail:
    intent = await _visible_intent(db, current, intent_id, lock=True)
    if intent.created_by != current.user.id and not is_admin(current):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only the creator or an admin can cancel")
    try:
        await paper.cancel(db, intent, current.user.id)
    except paper.IntentRefused as exc:
        raise _refused(exc) from exc
    audit.record(
        db,
        "order_intent_cancelled",
        ClientInfo.from_request(request),
        actor_user_id=current.user.id,
        details={"intent": str(intent.id)},
    )
    await db.commit()
    await db.refresh(intent)
    return await _detail(db, intent)


@router.get("/accounts/{account_id}/paper", response_model=PaperSummaryOut)
async def paper_summary(
    account_id: uuid.UUID, db: DbSession, current: Current, settings: SettingsDep
) -> PaperSummaryOut:
    account = await get_visible_account(db, current, account_id)
    return PaperSummaryOut.model_validate(await paper.summary(db, settings, account))


@router.post("/accounts/{account_id}/paper/reconcile", response_model=ReconciliationOut)
async def paper_reconcile(
    account_id: uuid.UUID, request: Request, db: DbSession, admin: AdminRequired
) -> ReconciliationOut:
    account = await get_visible_account(db, admin, account_id)
    row = await paper.reconcile(db, account)
    audit.record(
        db,
        "paper_reconciled",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"account": str(account.id), "ok": row.ok, "drift": len(row.drift)},
    )
    await db.commit()
    await db.refresh(row)
    return ReconciliationOut.model_validate(row)


@router.get("/accounts/{account_id}/paper/reconciliations", response_model=Page[ReconciliationOut])
async def paper_reconciliations(
    account_id: uuid.UUID,
    db: DbSession,
    current: Current,
    limit: Limit = 20,
    offset: Offset = 0,
) -> Page[ReconciliationOut]:
    account = await get_visible_account(db, current, account_id)
    stmt = select(Reconciliation).where(Reconciliation.broker_account_id == account.id)
    rows = (
        await db.scalars(stmt.order_by(Reconciliation.id.desc()).limit(limit).offset(offset))
    ).all()
    return Page[ReconciliationOut](
        items=[ReconciliationOut.model_validate(r) for r in rows],
        total=await count(db, stmt),
        limit=limit,
        offset=offset,
    )
