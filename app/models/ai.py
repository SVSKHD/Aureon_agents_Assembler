"""ai.* tables: conversations, messages (user-scoped through conversations), model inventory."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
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


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_ai_conversations_user_id_updated_at", "user_id", "updated_at"),
        {"schema": "ai"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    model: Mapped[str] = mapped_column(String(200))
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
