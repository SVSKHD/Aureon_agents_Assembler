from __future__ import annotations

import uuid
from datetime import datetime
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


class ChatRequest(BaseModel):
    """`messages` are the NEW turns only; for an existing conversation the server prepends
    its stored history before calling Ollama."""

    conversation_id: uuid.UUID | None = None
    model: str = Field(min_length=1, max_length=200)
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


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    model: str
    created_at: datetime
    updated_at: datetime


class ConversationPage(BaseModel):
    items: list[ConversationOut]
    total: int
    limit: int
    offset: int


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]
