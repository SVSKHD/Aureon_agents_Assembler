"""Every model call an agent makes (app/services/model_router.py, docs/model-routing.md).

Metadata only: never a prompt, an answer or any content. One row per attempt that reached a
provider, plus refusals (the gate kept it local) recorded on the local row as `route_reason`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ModelCall(Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        Index("ix_model_calls_agent_at", "agent", "at"),
        Index("ix_model_calls_at", "at"),
        {"schema": "ai"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    agent: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(16))  # ollama | openrouter | rules
    model: Mapped[str] = mapped_column(String(200))
    # ok | error | timeout | busy | cap — the attempt's own outcome
    outcome: Mapped[str] = mapped_column(String(16))
    # Why this provider: auto | forced_local | filter | context | daily_cap_reached | ...
    route_reason: Mapped[str | None] = mapped_column(String(40))
    # True when this attempt is the local fallback of a failed/refused external one.
    fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    local_only: Mapped[bool] = mapped_column(Boolean, default=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
