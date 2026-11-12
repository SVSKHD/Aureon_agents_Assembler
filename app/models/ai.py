"""ai.* tables: conversations, messages (user-scoped through conversations), model inventory."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MESSAGE_ROLES = ("system", "user", "assistant")
MESSAGE_STATUSES = ("complete", "cancelled", "error", "timeout")
PROVIDERS = ("ollama", "openrouter", "anthropic", "gemini")
CONVERSATION_CONTEXTS = ("general", "finance", "trading", "research")


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_ai_conversations_user_id_updated_at", "user_id", "updated_at"),
        CheckConstraint(
            "provider IN ('ollama','openrouter','anthropic','gemini')", name="provider"
        ),
        CheckConstraint("context IN ('general','finance','trading','research')", name="context"),
        {"schema": "ai"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("core.users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    # The provider + model picked for this conversation (migration 0020). The one that
    # actually answered a turn is on the message: a turn may fall back to local Ollama.
    model: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(16), server_default=text("'ollama'"))
    # What the conversation is linked to; finance/trading/research force local inference.
    context: Mapped[str] = mapped_column(String(16), server_default=text("'general'"))
    # The Claude agent this conversation talks to (migration 0030); NULL on older rows.
    agent: Mapped[str | None] = mapped_column(String(16))
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
    # Assistant turns (migration 0020): who answered, why it fell back to local, the cost.
    provider: Mapped[str | None] = mapped_column(String(16))
    fallback_reason: Mapped[str | None] = mapped_column(String(64))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    # The exact API content blocks as sent / received (redacted; thinking blocks unchanged),
    # replayed on the next turn (migration 0030). NULL on older rows: `content` is used.
    blocks: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
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


# --- Claude gateway (migration 0030, docs/26-ai-providers.md) ------------------------------

AI_AGENTS = ("investments", "business", "general", "report")
BUDGET_KEYS = ("investments", "business", "chat", "reserve")
TOOL_CALL_STATUSES = ("ok", "error", "refused", "awaiting_approval")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "executed", "failed", "expired")
USAGE_OUTCOMES = ("reserved", "ok", "error", "refused")


class ToolCall(Base):
    """One tool Claude asked for, run locally. Input and the short result summary only."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint("status IN ('ok','error','refused','awaiting_approval')", name="status"),
        CheckConstraint("kind IN ('read','write')", name="kind"),
        Index("ix_ai_tool_calls_conversation_id_at", "conversation_id", "at"),
        {"schema": "ai"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai.conversations.id", ondelete="CASCADE")
    )
    agent: Mapped[str] = mapped_column(String(16))
    tool: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(8))
    tool_use_id: Mapped[str] = mapped_column(String(64))
    input: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(24))
    summary: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Usage(Base):
    """One Claude call: tokens, cost and latency (never content). A row is written as
    `reserved` with the worst-case cost before the call and settled after it."""

    __tablename__ = "usage"
    __table_args__ = (
        CheckConstraint("outcome IN ('reserved','ok','error','refused')", name="outcome"),
        CheckConstraint("budget IN ('investments','business','chat','reserve')", name="budget"),
        Index("ix_ai_usage_at", "at"),
        Index("ix_ai_usage_budget_at", "budget", "at"),
        {"schema": "ai"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    agent: Mapped[str] = mapped_column(String(16))
    budget: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai.conversations.id", ondelete="SET NULL")
    )
    request_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16))
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cache_write_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), server_default=text("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(String(64))


class Approval(Base):
    """A write tool's request. Claude never executes it: an admin approves, then the backend
    runs the action itself (app/ai/approvals.py)."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','approved','rejected','executed','failed','expired')",
            name="status",
        ),
        Index("ix_ai_approvals_status_created_at", "status", "created_at"),
        {"schema": "ai"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai.conversations.id", ondelete="SET NULL")
    )
    tool_call_id: Mapped[int | None] = mapped_column(
        ForeignKey("ai.tool_calls.id", ondelete="SET NULL")
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    agent: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
