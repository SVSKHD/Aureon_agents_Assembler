"""Models, chat (SSE) and conversation endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.agents.ollama.client import OllamaClient, OllamaUnavailable
from app.agents.ollama.limiter import GenerationLimiter, QueueFull
from app.auth.deps import CsrfProtected, Current, DbSession, SettingsDep
from app.models.ai import Conversation, ModelInventory
from app.schemas.ai import (
    ChatRequest,
    ConversationDetail,
    ConversationOut,
    ConversationPage,
    MessageOut,
    ModelList,
    ModelOut,
    ModelStatus,
    ModelStatusList,
)
from app.services.chat import (
    ChatJob,
    ChatStream,
    ChatStreamingResponse,
    ConversationService,
    sync_inventory,
)

router = APIRouter(tags=["ai"])


def _ollama(request: Request) -> OllamaClient:
    client: OllamaClient = request.app.state.ollama
    return client


async def _models_synced(request: Request, db: DbSession) -> list[Any]:
    """Installed models (cached); upsert ai.model_inventory once per cache refresh."""
    client = _ollama(request)
    models = await client.list_models()
    if getattr(request.app.state, "inventory_generation", None) != client.generation:
        await sync_inventory(db, models)
        await db.commit()
        request.app.state.inventory_generation = client.generation
    return models


@router.get("/models", response_model=ModelList)
async def list_models(request: Request, db: DbSession, _: Current) -> ModelList:
    try:
        models = await _models_synced(request, db)
    except OllamaUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.message) from exc
    return ModelList(
        models=[
            ModelOut(
                name=m.name,
                size=m.size,
                family=m.family,
                parameter_size=m.parameter_size,
                quantization=m.quantization,
                modified_at=m.modified_at,
            )
            for m in models
        ]
    )


@router.get("/models/status", response_model=ModelStatusList)
async def models_status(request: Request, db: DbSession, _: Current) -> ModelStatusList:
    try:
        await _models_synced(request, db)
        running = {m.get("name"): m for m in await _ollama(request).running()}
        reachable = True
    except OllamaUnavailable:
        running, reachable = {}, False
    inventory = (await db.scalars(select(ModelInventory).order_by(ModelInventory.name))).all()
    return ModelStatusList(
        reachable=reachable,
        models=[
            ModelStatus(
                name=row.name,
                size=row.size_bytes,
                loaded=row.name in running,
                size_vram=(running.get(row.name) or {}).get("size_vram"),
                expires_at=(running.get(row.name) or {}).get("expires_at"),
                last_used_at=row.last_used_at,
            )
            for row in inventory
        ],
    )


@router.post(
    "/chat",
    response_class=ChatStreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}, 404: {}, 429: {}, 503: {}},
)
async def chat(
    body: ChatRequest,
    request: Request,
    current: CsrfProtected,
    db: DbSession,
    settings: SettingsDep,
) -> Response:
    num_ctx = body.num_ctx or settings.CHAT_DEFAULT_NUM_CTX
    if num_ctx > settings.CHAT_MAX_NUM_CTX:
        raise HTTPException(
            422,
            f"num_ctx may not exceed {settings.CHAT_MAX_NUM_CTX}",
        )

    ollama = _ollama(request)
    try:
        installed = [m.name for m in await _models_synced(request, db)]
    except OllamaUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.message) from exc
    if body.model not in installed:
        return JSONResponse(
            {"detail": f"model {body.model!r} is not installed", "available": installed},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    limiter: GenerationLimiter = request.app.state.chat_limiter
    try:
        ticket = limiter.admit()
    except QueueFull as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "generation queue is full",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    try:
        started = await ConversationService(db, current.user.id).start_turn(
            body.conversation_id,
            body.model,
            [{"role": m.role, "content": m.content} for m in body.messages],
        )
        if started is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        conv, prompt, message_ids = started
        await db.commit()

        options: dict[str, Any] = {"num_ctx": num_ctx}
        if body.temperature is not None:
            options["temperature"] = body.temperature
        if body.max_tokens is not None:
            options["num_predict"] = body.max_tokens
        stream = ChatStream(
            ChatJob(
                conversation_id=conv.id,
                user_message_ids=message_ids,
                model=body.model,
                prompt=prompt,
                options=options,
            ),
            ollama=ollama,
            maker=request.app.state.sessionmaker,
            ticket=ticket,
            timeout=settings.CHAT_TIMEOUT_SECONDS,
            heartbeat=settings.CHAT_HEARTBEAT_SECONDS,
        )
    except BaseException:
        ticket.release()
        raise
    return ChatStreamingResponse(stream, on_close=ticket.release)


def _conv_out(c: Conversation) -> ConversationOut:
    return ConversationOut(
        id=c.id, title=c.title, model=c.model, created_at=c.created_at, updated_at=c.updated_at
    )


@router.get("/conversations", response_model=ConversationPage)
async def list_conversations(
    db: DbSession,
    current: Current,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationPage:
    items, total = await ConversationService(db, current.user.id).page(limit, offset)
    return ConversationPage(
        items=[_conv_out(c) for c in items], total=total, limit=limit, offset=offset
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, db: DbSession, current: Current
) -> ConversationDetail:
    svc = ConversationService(db, current.user.id)
    conv = await svc.get(conversation_id)
    if conv is None:  # someone else's id looks exactly like a missing one
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    msgs = await svc.messages(conv.id)
    return ConversationDetail(
        **_conv_out(conv).model_dump(),
        messages=[
            MessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                model=m.model,
                status=m.status,
                error=m.error,
                prompt_tokens=m.prompt_tokens,
                completion_tokens=m.completion_tokens,
                duration_ms=m.duration_ms,
                created_at=m.created_at,
            )
            for m in msgs
        ],
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: uuid.UUID, db: DbSession, current: CsrfProtected
) -> None:
    if not await ConversationService(db, current.user.id).delete(conversation_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    await db.commit()
