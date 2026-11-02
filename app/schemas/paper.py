"""Paper trading (Phase F) request/response models. Money and quantities are Decimal."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.trading import ExecutionOut, RiskDecisionOut


class OrderIntentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: uuid.UUID
    instrument_id: int
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    quantity: Decimal = Field(gt=0, max_digits=20, decimal_places=8)
    limit_price: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=8)
    product: str | None = Field(default=None, max_length=16)
    signal_id: uuid.UUID | None = None
    correlation_id: str | None = Field(
        default=None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$"
    )


class OrderEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    correlation_id: str | None
    payload: dict[str, Any]
    occurred_at: datetime


class OrderIntentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    broker_account_id: uuid.UUID
    instrument_id: int
    signal_id: uuid.UUID | None
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None
    product: str | None
    validity: str
    mode: str
    status: str
    source: str
    created_by: uuid.UUID | None
    correlation_id: str
    created_at: datetime
    updated_at: datetime


class OrderIntentDetail(OrderIntentOut):
    events: list[OrderEventOut] = []
    risk_decisions: list[RiskDecisionOut] = []
    executions: list[ExecutionOut] = []


class ReconciliationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    broker_account_id: uuid.UUID
    ok: bool
    drift: list[dict[str, Any]]
    summary: dict[str, Any]
    created_at: datetime
