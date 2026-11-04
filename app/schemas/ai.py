from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ModelOut(BaseModel):
    name: str
    size: int | None
    family: str | None
    parameter_size: str | None
    quantization: str | None
    modified_at: datetime | None


class ModelList(BaseModel):
    models: list[ModelOut]
    # CHAT_DEFAULT_MODEL when it is installed (it is also listed first), else None.
    default_model: str | None = None


class ModelStatus(BaseModel):
    name: str
    size: int | None
    loaded: bool
    size_vram: int | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None


class ModelStatusList(BaseModel):
    reachable: bool
    models: list[ModelStatus]


class ChatMessageIn(BaseModel):
    role: Literal["system", "user"]
    content: str = Field(min_length=1, max_length=32_000)


Provider = Literal["ollama", "openrouter", "anthropic", "gemini"]
ConversationContext = Literal["general", "finance", "trading", "research"]


class ChatRequest(BaseModel):
    """`messages` are the NEW turns only; for an existing conversation the server prepends
    its stored history before calling the model.

    `provider`/`model` pick who should answer (docs/25-openrouter.md, docs/26). Both may be
    omitted: an existing conversation keeps its own, a new one gets the server default
    (AI_DEFAULT_EXTERNAL_PROVIDER's default model when it is usable, else the local default).
    A `model` without a `provider` means an external provider only when it is one of that
    provider's allowed ids (OpenRouter ids contain a "/").
    `context` links a conversation to finance/trading/research (forces local inference);
    a link only ever gets stricter."""

    conversation_id: uuid.UUID | None = None
    provider: Provider | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    context: ConversationContext | None = None
    messages: list[ChatMessageIn] = Field(min_length=1, max_length=20)
    temperature: float | None = Field(default=None, ge=0, le=2)
    num_ctx: int | None = Field(default=None, ge=256)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)

    @model_validator(mode="after")
    def _ends_with_user(self) -> ChatRequest:
        if self.messages[-1].role != "user":
            raise ValueError("the last message must have role 'user'")
        return self


class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    model: str | None
    status: str
    error: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    duration_ms: int | None
    created_at: datetime
    # Assistant turns: who answered, why it was not OpenRouter (when that was picked), cost.
    provider: str | None = None
    fallback_reason: str | None = None
    cost_usd: Decimal | None = None


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    model: str
    provider: str = "ollama"
    context: str = "general"
    created_at: datetime
    updated_at: datetime


class ConversationPage(BaseModel):
    items: list[ConversationOut]
    total: int
    limit: int
    offset: int


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class CatalogModel(BaseModel):
    provider: Provider
    id: str
    label: str
    # False: listed for visibility but not selectable (e.g. an unpriced direct-API model).
    available: bool = True
    unavailable_reason: str | None = None
    capabilities: list[str] = Field(default_factory=lambda: ["chat", "streaming"])
    # "local" (never leaves the server) | "external" (only after the privacy gate).
    privacy: Literal["local", "external"] = "local"


class CatalogTarget(BaseModel):
    provider: Provider
    model: str | None


class OpenRouterStatus(BaseModel):
    enabled: bool
    # None = usable. Else disabled | not_configured | no_budget (why every turn stays local).
    unavailable_reason: str | None
    research_external_allowed: bool


class ProviderStatus(BaseModel):
    """One provider's state for the picker and the settings page. Never a key."""

    provider: Provider
    label: str
    enabled: bool
    # A key is set (never the key itself).
    configured: bool
    usable: bool
    # None = usable. disabled | not_configured | no_budget (why every turn stays local).
    unavailable_reason: str | None
    default_model: str | None
    allowed_models: list[str]
    daily_limit_usd: Decimal | None
    # OpenRouter reports the cost; direct APIs are charged at the configured price.
    cost_source: Literal["provider", "configured_price", "none"]
    privacy: Literal["local", "external"]


class ModelCatalog(BaseModel):
    """Everything the SPA's picker offers. External ids come from the server's allowlists;
    the browser never talks to a provider."""

    default: CatalogTarget
    local_fallback_model: str | None
    ollama_reachable: bool
    models: list[CatalogModel]
    openrouter: OpenRouterStatus
    # Every provider, local first (docs/26). Added without changing the fields above.
    providers: list[ProviderStatus] = Field(default_factory=list)
    default_external_provider: str | None = None
    global_daily_limit_usd: Decimal | None = None


class AiUsage(BaseModel):
    """Today's (UTC) spend of one provider (OpenRouter unless ?provider=) against its cap,
    plus the shared cap across providers."""

    day: date
    provider: Literal["openrouter", "anthropic", "gemini"] = "openrouter"
    spent_usd: Decimal
    limit_usd: Decimal
    remaining_usd: Decimal
    prompt_tokens: int
    completion_tokens: int
    calls: int
    failures: int
    cap_reached: bool
    # False when the counter could not be read (external calls then stay local).
    available: bool = True
    # AI_EXTERNAL_DAILY_LIMIT_USD (None = per-provider caps only) and today's total across
    # providers, open reservations included.
    global_limit_usd: Decimal | None = None
    global_spent_usd: Decimal | None = None
