"""ai.* tables: conversations, messages (user-scoped through conversations), model inventory."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MESSAGE_ROLES = ("system", "user", "assistant")
MESSAGE_STATUSES = ("complete", "cancelled", "error", "timeout")
PROVIDERS = ("ollama", "openrouter")
CONVERSATION_CONTEXTS = ("general", "finance", "trading", "research")


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_ai_conversations_user_id_updated_at", "user_id", "updated_at"),
        CheckConstraint("provider IN ('ollama','openrouter')", name="provider"),
        CheckConstraint("context IN ('general','finance','trading','research')", name="context"),
        {"schema": "ai"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    # The provider + model picked for this conversation (migration 0021). The one that
    # actually answered a turn is on the message: a turn may fall back to local Ollama.
    model: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(16), server_default=text("'ollama'"))
    # What the conversation is linked to; finance/trading/research force local inference.
    context: Mapped[str] = mapped_column(String(16), server_default=text("'general'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('system','user','assistant')", name="role"),
        CheckConstraint("status IN ('complete','cancelled','error','timeout')", name="status"),
        Index("ix_ai_messages_conversation_id_id", "conversation_id", "id"),
        {"schema": "ai"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai.conversations.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), server_default=text("'complete'"))
    error: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # Assistant turns (migration 0021): who answered, why it fell back to local, the cost.
    provider: Mapped[str | None] = mapped_column(String(16))
    fallback_reason: Mapped[str | None] = mapped_column(String(64))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModelInventory(Base):
    __tablename__ = "model_inventory"
    __table_args__ = ({"schema": "ai"},)

    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    digest: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    family: Mapped[str | None] = mapped_column(String(100))
    parameter_size: Mapped[str | None] = mapped_column(String(32))
    quantization: Mapped[str | None] = mapped_column(String(32))
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
