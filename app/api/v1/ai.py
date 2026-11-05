"""Models, chat (SSE) and conversation endpoints."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.agents.ollama.client import OllamaClient, OllamaUnavailable
from app.agents.ollama.limiter import GenerationLimiter, QueueFull, Ticket
from app.agents.providers.base import EXTERNAL_PROVIDERS, PROVIDER_LABELS
from app.api.ui import ui_module
from app.auth.deps import CsrfProtected, Current, DbSession, SettingsDep
from app.core.config import Settings
from app.models.ai import Conversation, ModelInventory
from app.schemas.ai import (
    AiUsage,
    CatalogModel,
    CatalogTarget,
    ChatRequest,
    ConversationDetail,
    ConversationOut,
    ConversationPage,
    MessageOut,
    ModelCatalog,
    ModelList,
    ModelOut,
    ModelStatus,
    ModelStatusList,
    OpenRouterStatus,
    ProviderStatus,
)
from app.services import audit, interactive
from app.services.ai_usage import MICRO, DailyUsage, UsageTracker, to_micro, utc_today
from app.services.chat import (
    ChatJob,
    ChatStream,
    ChatStreamingResponse,
    ConversationService,
    chat_options,
    sync_inventory,
    trim_history,
)
from app.services.inference import (
    Decision,
    ExternalGate,
    choose,
    external_clients,
    gate_for,
    local_fallback_model,
)

UI_MODULE = ui_module("chat", label="AI chat", group="ai", icon="message-square", order=40)
router = APIRouter(tags=["ai"])


def _ollama(request: Request) -> OllamaClient:
    client: OllamaClient = request.app.state.ollama
    return client


def _gate(request: Request, settings: Settings, provider: str = "openrouter") -> ExternalGate:
    state = request.app.state
    return gate_for(settings, provider, external_clients(state), getattr(state, "ai_usage", None))


def _gates(request: Request, settings: Settings) -> dict[str, ExternalGate]:
    return {p: _gate(request, settings, p) for p in EXTERNAL_PROVIDERS}


def _default_external(settings: Settings, gates: dict[str, ExternalGate]) -> ExternalGate | None:
    """The first usable gate in the chain: AI_DEFAULT_EXTERNAL_PROVIDER, then
    AI_EXTERNAL_FALLBACK_PROVIDERS (docs/30). None = local."""
    for p in settings.external_chain(settings.AI_DEFAULT_EXTERNAL_PROVIDER):
        gate = gates.get(p)
        if gate is not None and gate.usable:
            return gate
    return None


def _provider_status(settings: Settings, gate: ExternalGate) -> ProviderStatus:
    cfg = settings.external(gate.provider)
    reason = gate.unusable_reason()
    return ProviderStatus(
        provider=gate.provider,
        label=PROVIDER_LABELS[gate.provider],
        enabled=cfg.enabled,
        configured=bool(cfg.api_key),
        usable=reason is None,
        unavailable_reason=reason,
        default_model=cfg.default_model or None,
        allowed_models=list(cfg.models),
        daily_limit_usd=cfg.daily_limit_usd,
        cost_source="provider" if cfg.reports_cost else "configured_price",
        privacy="external",
    )


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
async def list_models(
    request: Request, db: DbSession, _: Current, settings: SettingsDep
) -> ModelList:
    try:
        models = await _models_synced(request, db)
    except OllamaUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.message) from exc
    default = settings.CHAT_DEFAULT_MODEL
    if default not in {m.name for m in models}:
        default = ""
    # The default first: the SPA preselects the first model when none is chosen yet.
    models = sorted(models, key=lambda m: m.name != default)
    return ModelList(
        default_model=default or None,
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
        ],
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


@router.get("/models/catalog", response_model=ModelCatalog)
async def model_catalog(
    request: Request, db: DbSession, _: Current, settings: SettingsDep
) -> ModelCatalog:
    """The picker's list: installed Ollama models plus the allowed OpenRouter ids."""
    try:
        installed = [m.name for m in await _models_synced(request, db)]
        reachable = True
    except OllamaUnavailable:
        installed, reachable = [], False
    gates = _gates(request, settings)
    local = local_fallback_model(settings, installed if reachable else None)
    models = [CatalogModel(provider="ollama", id=m, label=m) for m in installed]
    for p, g in gates.items():
        if not g.usable:
            continue
        for m in g.models:
            priced = g.priced(m)
            models.append(
                CatalogModel(
                    provider=p,
                    id=m,
                    label=m,
                    available=priced,
                    unavailable_reason=None if priced else "no_pricing",
                    privacy="external",
                )
            )
    default_gate = _default_external(settings, gates)
    default = (
        CatalogTarget(
            provider=default_gate.provider,
            model=default_gate.default_model,
        )
        if default_gate is not None
        else CatalogTarget(provider="ollama", model=local)
    )
    local_status = ProviderStatus(
        provider="ollama",
        label=PROVIDER_LABELS["ollama"],
        enabled=True,
        configured=True,
        usable=reachable,
        unavailable_reason=None if reachable else "unreachable",
        default_model=local,
        allowed_models=installed,
        daily_limit_usd=None,
        cost_source="none",
        privacy="local",
    )
    return ModelCatalog(
        default=default,
        local_fallback_model=local,
        ollama_reachable=reachable,
        models=models,
        openrouter=OpenRouterStatus(
            enabled=settings.OPENROUTER_ENABLED,
            unavailable_reason=gates["openrouter"].unusable_reason(),
            research_external_allowed=settings.AI_RESEARCH_EXTERNAL_ALLOWED,
        ),
        providers=[local_status, *(_provider_status(settings, g) for g in gates.values())],
        default_external_provider=settings.AI_DEFAULT_EXTERNAL_PROVIDER,
        global_daily_limit_usd=settings.AI_EXTERNAL_DAILY_LIMIT_USD,
    )


@router.get("/ai/usage", response_model=AiUsage)
async def ai_usage(
    request: Request,
    _: Current,
    settings: SettingsDep,
    provider: Annotated[str, Query(pattern="^(openrouter|anthropic|gemini)$")] = "openrouter",
) -> AiUsage:
    """Today's (UTC) spend, tokens and calls of one provider (default OpenRouter) against its
    daily cap, plus today's total across providers against the shared cap."""
    limit = settings.external(provider).daily_limit_usd
    base: UsageTracker | None = getattr(request.app.state, "ai_usage", None)
    tracker = base.for_provider(provider) if base is not None else None
    available = True
    global_spent = None
    try:
        usage = await tracker.today() if tracker else DailyUsage(utc_today())
        if tracker is not None:
            global_spent = Decimal(await tracker.total_today_micro()) / MICRO
    except Exception:
        usage, available = DailyUsage(utc_today()), False
    spent = usage.spent_usd
    return AiUsage(
        day=usage.day,
        spent_usd=spent,
        limit_usd=limit,
        remaining_usd=max(limit - spent, 0),
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        calls=usage.calls,
        failures=usage.failures,
        cap_reached=limit <= 0 or usage.cost_micro >= to_micro(limit),
        available=available,
        provider=provider,
        global_limit_usd=settings.AI_EXTERNAL_DAILY_LIMIT_USD,
        global_spent_usd=global_spent,
    )


def _target(
    body: ChatRequest,
    conv: Conversation | None,
    gates: dict[str, ExternalGate],
    settings: Settings,
) -> tuple[str, str]:
    """(provider, model) the user asked for; model "" = the local default."""
    if body.provider is not None:
        provider = body.provider
        model = body.model or (conv.model if conv is not None and conv.provider == provider else "")
        if provider in gates and not model:
            model = gates[provider].default_model
        return provider, model
    if body.model:
        for p in EXTERNAL_PROVIDERS:
            if p == "openrouter" and "/" not in body.model:
                continue
            if body.model in settings.external(p).models:
                return p, body.model
        return "ollama", body.model
    if conv is not None:
        return conv.provider, conv.model
    # A new conversation: the first *enabled* provider in the chain, usable or not. It is
    # stored on the conversation, so once the provider is fixed (key, price, budget) later
    # turns use it; meanwhile the gate's refusal moves on down the chain with its reason
    # shown, instead of the conversation silently becoming local for good.
    for name in settings.external_chain(settings.AI_DEFAULT_EXTERNAL_PROVIDER):
        if name in gates and settings.external(name).enabled:
            return name, gates[name].default_model
    return "ollama", ""


@router.post(
    "/chat",
    response_class=ChatStreamingResponse,
    status_code=200,  # explicit: the streaming class has no status_code default to inspect
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

    svc = ConversationService(db, current.user.id)
    conv: Conversation | None = None
    history: list[dict[str, str]] = []
    if body.conversation_id is not None:
        conv = await svc.get(body.conversation_id)
        if conv is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
        history = await svc.history(conv)
    new = [{"role": m.role, "content": m.content} for m in body.messages]
    prompt = trim_history(history, settings.CHAT_HISTORY_MAX_MESSAGES) + new
    context = conv.context if conv is not None else (body.context or "general")
    if conv is not None and body.context and body.context != "general" and context == "general":
        context = body.context  # becoming stricter applies to this turn already

    gates = _gates(request, settings)
    provider, model = _target(body, conv, gates, settings)
    gate = gates.get(provider)
    ollama = _ollama(request)
    paused = bool(request.app.state.agent_paused.get("ollama"))
    first_provider, first_model = provider, model
    route_reason: str | None = None
    remaining: tuple[str, ...] = ()

    if gate is not None:
        if body.model and not gate.allowed(model):
            label = PROVIDER_LABELS[provider]
            return JSONResponse(
                {
                    "detail": f"model {model!r} is not an allowed {label} model",
                    "available": gate.models,
                },
                status_code=status.HTTP_404_NOT_FOUND,
            )
        try:
            installed: list[str] | None = [m.name for m in await _models_synced(request, db)]
        except OllamaUnavailable:
            installed = None
        fallback = None if paused else local_fallback_model(settings, installed)
        pick = await choose(
            gates,
            settings.external_chain(provider),
            model,
            prompt,
            context=context,
            max_tokens=body.max_tokens,
        )
        decision = pick.decision
        if pick.gate is not None:
            # Possibly a later provider in the chain; the first one's refusal is the reason.
            gate, remaining = pick.gate, pick.remaining
            first_provider, first_model = gate.provider, pick.model
            route_reason = pick.skipped_reason
        else:
            route_reason = decision.reason
    else:
        try:
            installed = [m.name for m in await _models_synced(request, db)]
        except OllamaUnavailable as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, exc.message) from exc
        if not model:
            model = local_fallback_model(settings, installed) or ""
        if model not in installed:
            return JSONResponse(
                {"detail": f"model {model!r} is not installed", "available": installed},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        fallback = model
        decision = Decision(False)

    if gate is not None and not decision.external:
        if not fallback:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"{PROVIDER_LABELS[provider]} is not used for this turn ({decision.reason}) "
                "and no local model is available",
            )
        first_provider, first_model = "ollama", fallback

    ticket: Ticket | None = None
    if first_provider == "ollama":
        if paused:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "the ollama agent is paused by an admin"
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
        conv, message_ids = await svc.store_turn(
            conv, new, model=model, provider=provider, context=context
        )
        await db.commit()

        options = chat_options(num_ctx, settings.CHAT_NUM_THREAD)
        if body.temperature is not None:
            options["temperature"] = body.temperature
        if body.max_tokens is not None:
            options["num_predict"] = body.max_tokens
        stream = ChatStream(
            ChatJob(
                conversation_id=conv.id,
                user_message_ids=message_ids,
                model=first_model,
                prompt=prompt,
                options=options,
                provider=first_provider,
                requested_provider=provider,
                requested_model=model,
                fallback_model=fallback,
                route_reason=route_reason,
                filter_categories=tuple(decision.categories),
                context=context,
                alternates=remaining,
                temperature=body.temperature,
                max_tokens=body.max_tokens,
                user_id=current.user.id,
                client=audit.ClientInfo.from_request(request),
                reserved_micro=decision.reserved_micro,
            ),
            ollama=ollama,
            maker=request.app.state.sessionmaker,
            ticket=ticket,
            timeout=settings.CHAT_TIMEOUT_SECONDS,
            heartbeat=settings.CHAT_HEARTBEAT_SECONDS,
            registry=request.app.state.registry,
            limiter=request.app.state.chat_limiter,
            external=gate.client if gate is not None else None,
            usage=getattr(request.app.state, "ai_usage", None),
            gates=gates,
        )
    except BaseException:
        if ticket is not None:
            ticket.release()
        if gate is not None:
            await gate.release(decision)
        raise
    # Image generation yields to a running chat (app/services/interactive.py).
    redis = request.app.state.redis
    token = await interactive.mark(redis, "chat", settings.CHAT_TIMEOUT_SECONDS)
    return ChatStreamingResponse(stream, on_finish=lambda: interactive.clear(redis, token))


def _conv_out(c: Conversation) -> ConversationOut:
    return ConversationOut(
        id=c.id,
        title=c.title,
        model=c.model,
        provider=c.provider or "ollama",
        context=c.context or "general",
        created_at=c.created_at,
        updated_at=c.updated_at,
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
                provider=m.provider,
                fallback_reason=m.fallback_reason,
                cost_usd=m.cost_usd,
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
