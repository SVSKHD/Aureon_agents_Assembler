"""Finance API models. Money is Decimal on the wire (exact strings), never float."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Money = Annotated[Decimal, Field(max_digits=20, decimal_places=8)]
NonNegMoney = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=8)]
RowId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Session = Literal["Asia", "London", "NY"]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- trades ---------------------------------------------------------------------------


class TradeFields(_In):
    ist_date: dt.date
    ist_time: str = Field(default="", max_length=16)
    entry_at: dt.datetime | None = None
    exit_at: dt.datetime | None = None
    ts: dt.datetime | None = None
    broker_offset_minutes: int = Field(default=180, ge=-1440, le=1440)
    symbol: str = Field(default="", max_length=64)
    session: Session = "London"
    side: Literal["buy", "sell"] = "buy"
    lot: Money = Decimal(1)
    entry: Money = Decimal(0)
    exit: Money = Decimal(0)
    move: Money = Decimal(0)
    pl: Money = Decimal(0)
    note: str = Field(default="", max_length=4000)
    signal_id: str | None = Field(default=None, max_length=128)
    time_estimated: bool = False


class TradeIn(TradeFields):
    id: RowId | None = None  # client-minted ids are kept verbatim (replays land on the same row)


class TradePatch(_In):
    ist_date: dt.date | None = None
    ist_time: str | None = Field(default=None, max_length=16)
    entry_at: dt.datetime | None = None
    exit_at: dt.datetime | None = None
    ts: dt.datetime | None = None
    broker_offset_minutes: int | None = Field(default=None, ge=-1440, le=1440)
    symbol: str | None = Field(default=None, max_length=64)
    session: Session | None = None
    side: Literal["buy", "sell"] | None = None
    lot: Money | None = None
    entry: Money | None = None
    exit: Money | None = None
    move: Money | None = None
    pl: Money | None = None
    note: str | None = Field(default=None, max_length=4000)
    signal_id: str | None = Field(default=None, max_length=128)
    time_estimated: bool | None = None


class TradeOut(BaseModel):
    id: str
    ist_date: dt.date
    ist_time: str
    entry_at: dt.datetime | None
    exit_at: dt.datetime | None
    ts: dt.datetime | None
    broker_offset_minutes: int
    symbol: str
    session: str
    side: str
    lot: Decimal
    entry: Decimal
    exit: Decimal
    move: Decimal
    pl: Decimal
    note: str
    signal_id: str | None
    time_estimated: bool
    created_at: dt.datetime
    updated_at: dt.datetime


# --- expenses ---------------------------------------------------------------------------


class _RecurCheck(_In):
    @model_validator(mode="after")
    def _recurring_needs_day(self) -> Any:
        kind = getattr(self, "kind", None)
        day = getattr(self, "recur_day", None)
        if kind == "recurring" and day is None:
            raise ValueError("a recurring expense needs recur_day (1-31)")
        return self


class ExpenseIn(_RecurCheck):
    id: RowId | None = None
    date: dt.date
    amount: NonNegMoney = Decimal(0)
    category: str = Field(default="Other", max_length=64)
    note: str = Field(default="", max_length=4000)
    kind: Literal["one-off", "recurring"] = "one-off"
    recur_day: int | None = Field(default=None, ge=1, le=31)


class ExpensePatch(_In):
    date: dt.date | None = None
    amount: NonNegMoney | None = None
    category: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=4000)
    kind: Literal["one-off", "recurring"] | None = None
    recur_day: int | None = Field(default=None, ge=1, le=31)


class ExpenseOut(BaseModel):
    id: str
    date: dt.date
    amount: Decimal
    category: str
    note: str
    kind: str
    recur_day: int | None
    created_at: dt.datetime
    updated_at: dt.datetime


# --- secured ----------------------------------------------------------------------------


class SecuredIn(_In):
    id: RowId | None = None
    date: dt.date
    amt: NonNegMoney = Decimal(0)
    note: str = Field(default="", max_length=4000)


class SecuredPatch(_In):
    date: dt.date | None = None
    amt: NonNegMoney | None = None
    note: str | None = Field(default=None, max_length=4000)


class SecuredOut(BaseModel):
    id: str
    date: dt.date
    amt: Decimal
    note: str
    created_at: dt.datetime
    updated_at: dt.datetime


# --- documents + portfolio ----------------------------------------------------------------


Namespace = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]


class DocumentIn(_In):
    data: dict[str, Any]
    merge: bool = False  # Firestore setDoc(..., {merge: true}) semantics


class DocumentOut(BaseModel):
    namespace: str
    doc_id: str
    data: dict[str, Any]
    updated_at: dt.datetime


class PortfolioIn(_In):
    content: dict[str, Any]


class PortfolioOut(BaseModel):
    id: str
    content: dict[str, Any]
    updated_at: dt.datetime


# --- totals + claim -------------------------------------------------------------------------


class FinanceTotals(BaseModel):
    """Exact sums for a date range (what the month screens show)."""

    date_from: dt.date | None
    date_to: dt.date | None
    trades: int
    trade_pl: Decimal
    expenses: Decimal
    expenses_count: int
    secured: Decimal
    secured_count: int


class ClaimIn(_In):
    legacy_user_id: str = Field(min_length=1, max_length=128)
    user_id: uuid.UUID
    apply: bool = False  # dry run by default: counts only


class ClaimOut(BaseModel):
    legacy_user_id: str
    user_id: uuid.UUID
    applied: bool
    rows: dict[str, int]
