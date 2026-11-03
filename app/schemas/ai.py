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


Provider = Literal["ollama", "openrouter"]
ConversationContext = Literal["general", "finance", "trading", "research"]


class ChatRequest(BaseModel):
    """`messages` are the NEW turns only; for an existing conversation the server prepends
    its stored history before calling the model.

    `provider`/`model` pick who should answer (docs/25-openrouter.md). Both may be omitted:
    an existing conversation keeps its own, a new one gets the server default (OpenRouter's
    default model when it is usable, else the local default). A `model` without a
    `provider` means OpenRouter only when it is one of the allowed OpenRouter ids.
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


class CatalogTarget(BaseModel):
    provider: Provider
    model: str | None


class OpenRouterStatus(BaseModel):
    enabled: bool
    # None = usable. Else disabled | not_configured | no_budget (why every turn stays local).
    unavailable_reason: str | None
    research_external_allowed: bool


class ModelCatalog(BaseModel):
    """Everything the SPA's picker offers. OpenRouter ids come from the server's allowlist;
    the browser never talks to OpenRouter."""

    default: CatalogTarget
    local_fallback_model: str | None
    ollama_reachable: bool
    models: list[CatalogModel]
    openrouter: OpenRouterStatus


class AiUsage(BaseModel):
    """Today's (UTC) OpenRouter spend against OPENROUTER_DAILY_LIMIT_USD."""

    day: date
    provider: Literal["openrouter"] = "openrouter"
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
