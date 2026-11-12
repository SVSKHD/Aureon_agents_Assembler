"""Aureon AI over Claude (docs/26-ai-providers.md). Every route needs a signed-in session.

POST /api/ai/chat                       stream one turn (SSE); CSRF; rate-limited per user
GET  /api/ai/conversations              the caller's conversations, newest first
GET  /api/ai/conversations/{id}         one conversation's readable messages
GET  /api/ai/approvals?status=pending   write requests from the agents (admin)
POST /api/ai/approvals/{id}             {action: approve|reject, password?} (admin + CSRF;
                                        approve also needs the admin's password). On approve
                                        the backend executes the action, never Claude.
GET  /api/ai/usage                      month-to-date spend, total and per budget (admin)

SSE events on /chat: `meta` {conversation_id, agent}, `text` {delta}, `tool` {name, status},
`approval` {id, action, summary}, `done` {conversation_id, message_id, cost_usd, steps,
stop_reason, approvals, text}, `error` {code, message}. A `: ping` comment is sent while idle.
The turn finishes and is saved even if the client disconnects.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.agents.registry import CLAUDE
from app.ai import approvals, chat
from app.ai.agents import CHAT_AGENTS, AgentName
from app.ai.budget import IST, month_start, next_month_start
from app.ai.gateway import ClaudeGateway, GatewayError
from app.ai.tools import default_registry
from app.ai.tools.registry import ToolContext
from app.ai.workspace import WorkspaceIn
from app.ai.workspace import render as workspace_text
from app.api.ui import ui_module
from app.auth.deps import AdminRequired, CsrfProtected, Current, DbSession, SettingsDep
from app.auth.security import verify_password
from app.core.logging import get_logger
from app.models.ai import Approval, Conversation, Message, Usage
from app.models.auth import ROLE_ADMIN
from app.schemas.common import Page
from app.services import audit, interactive
from app.services import chat_attachments as files
from app.services.health import check_data_root

UI_MODULE = ui_module("chat", label="AI chat", group="ai", icon="message-square", order=40)
router = APIRouter(prefix="/ai", tags=["ai"])
log = get_logger("api.ai")
PING_SECONDS = 15.0
_RUNNING: set[asyncio.Task[Any]] = set()


def _gateway(request: Request) -> ClaudeGateway:
    if getattr(request.app.state, "agent_paused", {}).get(CLAUDE.id):
        raise HTTPException(
            503, {"code": "ai_paused", "message": "the claude agent is paused by an admin"}
        )
    gw: ClaudeGateway | None = getattr(request.app.state, "ai", None)
    if gw is None or not gw.configured:
        raise HTTPException(
            503, {"code": "ai_not_configured", "message": "ANTHROPIC_API_KEY is not set"}
        )
    return gw


async def _rate_limit(request: Request, user_id: uuid.UUID, per_minute: int) -> None:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return
    key = f"rl:ai:{user_id}:{int(datetime.now(UTC).timestamp()) // 60}"
    try:
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, 70)
    except Exception as exc:  # pragma: no cover - fail open on a Redis outage
        log.warning("ai_rate_limit_unavailable", error=type(exc).__name__)
        return
    if n > per_minute:
        raise HTTPException(429, {"code": "rate_limited", "message": "too many AI requests"})


# --- chat -------------------------------------------------------------------------------


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=20_000)
    agent: Literal["investments", "business", "general"] | None = None
    # The user's open todos, tasks, reminders and note titles, from the app (read-only
    # context; the backend has no access to the workspace itself). app/ai/workspace.py.
    workspace: WorkspaceIn | None = None
    # Files uploaded with POST /attachments (photos, PDFs, spreadsheets), at most
    # AI_ATTACHMENTS_PER_MESSAGE; only the caller's own.
    attachments: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class ChatAttachmentOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: Literal["image", "pdf", "table"]
    media_type: str
    size: int
    pages: int | None = None
    rows: int | None = None


@router.post(
    "/attachments",
    response_model=ChatAttachmentOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {}, 413: {}, 415: {}, 422: {}, 503: {}},
)
async def upload_attachment(
    current: CsrfProtected,
    settings: SettingsDep,
    file: Annotated[UploadFile, File()],
) -> ChatAttachmentOut:
    """Attach a photo (PNG, JPEG, WebP, GIF, 5 MB), a PDF or a spreadsheet (.xlsx, CSV,
    TSV) to the next chat message: send the returned id in `attachments`. The type is read
    from the file's bytes. A spreadsheet is converted to a text table here; images and PDFs
    reach Claude as they are (they cannot be redacted)."""
    try:
        await asyncio.to_thread(check_data_root, settings)
    except RuntimeError as exc:
        detail = {"code": "storage_unavailable", "message": "attachment storage is not mounted"}
        raise HTTPException(503, detail) from exc
    try:
        data = await files.read_upload(file, settings.AI_ATTACHMENT_MAX_BYTES)
        att = await asyncio.to_thread(files.save, settings, current.user.id, file.filename, data)
    except files.AttachmentError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": exc.message}) from exc
    log.info("ai_attachment_saved", kind=att.kind, size=att.size)
    return ChatAttachmentOut(
        id=att.id, name=att.name, kind=att.kind, media_type=att.media_type, size=att.size,
        pages=att.pages, rows=att.rows,
    )  # fmt: skip


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.post(
    "/chat",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}, 404: {}, 429: {}, 503: {}},
)
async def chat_turn(
    body: ChatIn,
    request: Request,
    db: DbSession,
    current: CsrfProtected,
    settings: SettingsDep,
) -> StreamingResponse:
    gateway = _gateway(request)
    await _rate_limit(request, current.user.id, settings.AI_CHAT_REQUESTS_PER_MINUTE)
    if len(body.attachments) > settings.AI_ATTACHMENTS_PER_MESSAGE:
        raise HTTPException(
            422,
            {"code": "too_many_attachments",
             "message": f"at most {settings.AI_ATTACHMENTS_PER_MESSAGE} files per message"},
        )  # fmt: skip
    refs: list[dict[str, Any]] = []
    for att_id in dict.fromkeys(body.attachments):
        found = await asyncio.to_thread(files.load, settings, current.user.id, att_id)
        if found is None:  # missing, expired, or another user's
            raise HTTPException(
                404, {"code": "attachment_not_found", "message": "an attached file is gone"}
            )
        refs.append(files.reference(found))
    existing: Conversation | None = None
    if body.conversation_id is not None:
        try:
            existing = await chat.get_conversation(db, current.user.id, body.conversation_id)
        except chat.ConversationNotFound as exc:
            raise HTTPException(404, {"code": exc.code, "message": str(exc)}) from None
    state = request.app.state
    maker = state.sessionmaker
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    async def emit(event: str, data: dict[str, Any]) -> None:
        await queue.put((event, data))

    async def work() -> None:
        token = await interactive.mark(state.redis, "chat", settings.AI_TIMEOUT_SECONDS * 4)
        try:
            agent: AgentName | None = body.agent
            if agent is None and existing is not None and existing.agent in CHAT_AGENTS:
                agent = existing.agent
            if agent is None:
                agent = await chat.route(gateway, body.message, current.user.id)
            assert agent is not None
            conv, history = await chat.start(
                maker,
                user_id=current.user.id,
                conversation_id=body.conversation_id,
                agent=agent,
                title=body.message.strip().splitlines()[0][:80],
                model=settings.AI_MODEL_MAIN,
            )
            await emit("meta", {"conversation_id": str(conv.id), "agent": agent})
            ctx = ToolContext(
                settings=settings,
                maker=maker,
                user_id=current.user.id,
                is_admin=current.user.role.name == ROLE_ADMIN,
                agent=agent,
                conversation_id=conv.id,
                redis=state.redis,
                adapters=dict(getattr(state, "trading_adapters", {})),
            )
            res = await chat.turn(
                gateway,
                default_registry(),
                ctx,
                conversation=conv,
                history=history,
                prompt=body.message,
                agent=agent,
                on_event=emit,
                context=(
                    workspace_text(body.workspace, datetime.now(IST).date())
                    if body.workspace is not None
                    else None
                ),
                attachments=refs,
            )
            await emit(
                "done",
                {
                    "conversation_id": str(res.conversation_id),
                    "message_id": res.message_id,
                    "cost_usd": str(res.cost_usd),
                    "steps": res.steps,
                    "stop_reason": res.stop_reason,
                    "approvals": [str(a) for a in res.approvals],
                    # The whole answer once more: a client that missed text events
                    # (a proxy that drops them, a reconnect) still shows it.
                    "text": res.text,
                },
            )
        except (GatewayError, chat.ConversationError) as exc:
            await emit("error", {"code": exc.code, "message": str(exc)})
        except Exception as exc:
            log.error("ai_chat_failed", error=type(exc).__name__)
            await emit("error", {"code": "ai_error", "message": "the AI request failed"})
        finally:
            await interactive.clear(state.redis, token)
            await queue.put(None)

    task = asyncio.create_task(work(), name="ai-chat-turn")
    _RUNNING.add(task)  # keeps running (and saving) if the client goes away
    task.add_done_callback(_RUNNING.discard)

    async def events() -> AsyncIterator[str]:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), PING_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                return
            yield _sse(*item)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- conversations ----------------------------------------------------------------------


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    agent: str | None
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime
    cost_usd: Decimal | None


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


def _conv_out(c: Conversation) -> ConversationOut:
    return ConversationOut(
        id=c.id, title=c.title, agent=c.agent, created_at=c.created_at, updated_at=c.updated_at
    )


@router.get("/conversations", response_model=Page[ConversationOut])
async def list_conversations(
    db: DbSession,
    current: Current,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ConversationOut]:
    mine = Conversation.user_id == current.user.id
    total = await db.scalar(select(func.count()).select_from(Conversation).where(mine))
    rows = (
        await db.scalars(
            select(Conversation)
            .where(mine)
            .order_by(Conversation.updated_at.desc(), Conversation.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page(
        items=[_conv_out(c) for c in rows], total=int(total or 0), limit=limit, offset=offset
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, db: DbSession, current: Current
) -> ConversationDetail:
    try:
        conv = await chat.get_conversation(db, current.user.id, conversation_id)
    except chat.ConversationNotFound as exc:
        raise HTTPException(404, {"code": exc.code, "message": str(exc)}) from None
    rows: list[Message] = await chat.messages_of(db, conv.id)
    return ConversationDetail(
        **_conv_out(conv).model_dump(),
        messages=[
            MessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                created_at=m.created_at,
                cost_usd=m.cost_usd,
            )
            for m in rows
            if chat.visible(m)
        ],
    )


# --- approvals --------------------------------------------------------------------------


class ApprovalOut(BaseModel):
    id: uuid.UUID
    agent: str
    action: str
    summary: str
    payload: dict[str, Any]
    status: str
    conversation_id: uuid.UUID | None
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    result: dict[str, Any] | None
    error: str | None


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve", "reject"]
    password: str | None = Field(default=None, max_length=1024)


def _approval_out(a: Approval) -> ApprovalOut:
    return ApprovalOut(
        id=a.id,
        agent=a.agent,
        action=a.action,
        summary=a.summary,
        payload=a.payload,
        status=a.status,
        conversation_id=a.conversation_id,
        created_at=a.created_at,
        expires_at=a.expires_at,
        decided_at=a.decided_at,
        result=a.result,
        error=a.error,
    )


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    db: DbSession,
    _: AdminRequired,
    status: Literal["pending", "approved", "rejected", "executed", "failed", "expired"]
    | None = "pending",
    limit: int = Query(50, ge=1, le=200),
) -> list[ApprovalOut]:
    await approvals.expire(db)
    await db.commit()
    q = select(Approval).order_by(Approval.created_at.desc()).limit(limit)
    if status is not None:
        q = q.where(Approval.status == status)
    return [_approval_out(a) for a in (await db.scalars(q)).all()]


@router.post("/approvals/{approval_id}", response_model=ApprovalOut)
async def decide_approval(
    approval_id: uuid.UUID,
    body: DecisionIn,
    request: Request,
    db: DbSession,
    admin: AdminRequired,
    settings: SettingsDep,
) -> ApprovalOut:
    if body.action == "approve":
        ok = bool(body.password) and await asyncio.to_thread(
            verify_password, admin.user.password_hash, body.password or ""
        )
        if not ok:
            raise HTTPException(
                403, {"code": "reauth_failed", "message": "password re-authentication failed"}
            )
    state = request.app.state
    deps = approvals.ExecDeps(
        settings=settings,
        redis=getattr(state, "redis", None),
        adapters=dict(getattr(state, "trading_adapters", {})),
    )
    try:
        row = await approvals.decide(
            state.sessionmaker, deps, approval_id, admin.user.id, body.action
        )
    except approvals.ApprovalError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from None
    audit.record(
        db,
        "ai_approval_decided",
        audit.ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"approval_id": str(row.id), "action": row.action, "status": row.status},
    )
    await db.commit()
    return _approval_out(row)


# --- usage ------------------------------------------------------------------------------


class BudgetOut(BaseModel):
    budget: str
    spent_usd: Decimal
    limit_usd: Decimal
    remaining_usd: Decimal


class UsageOut(BaseModel):
    month_start: datetime
    resets_at: datetime
    spent_usd: Decimal
    limit_usd: Decimal
    remaining_usd: Decimal
    calls: int
    budgets: list[BudgetOut]
    configured: bool


@router.get("/usage", response_model=UsageOut)
async def usage(
    request: Request, db: DbSession, _: AdminRequired, settings: SettingsDep
) -> UsageOut:
    start = month_start()
    rows = (
        await db.execute(
            select(Usage.budget, func.coalesce(func.sum(Usage.cost_usd), 0), func.count())
            .where(Usage.at >= start)
            .group_by(Usage.budget)
        )
    ).all()
    spent = {b: Decimal(v) for b, v, _n in rows}
    calls = sum(int(n) for _b, _v, n in rows)
    total = sum(spent.values(), Decimal(0))
    cap = settings.AI_MONTHLY_BUDGET_USD
    budgets = [
        BudgetOut(
            budget=b,
            spent_usd=spent.get(b, Decimal(0)),
            limit_usd=lim,
            remaining_usd=max(lim - spent.get(b, Decimal(0)), Decimal(0)),
        )
        for b, lim in settings.AI_AGENT_BUDGETS_USD.items()
    ]
    gw = getattr(request.app.state, "ai", None)
    return UsageOut(
        month_start=start,
        resets_at=next_month_start(),
        spent_usd=total,
        limit_usd=cap,
        remaining_usd=max(cap - total, Decimal(0)),
        calls=calls,
        budgets=budgets,
        configured=bool(gw is not None and gw.configured),
    )


__all__ = ["router"]
