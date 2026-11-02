"""Finance: the existing Spasta tables (public.*) plus Aureon ownership.

The five legacy tables were created by ASTRA-FRONTEND's Supabase migrations
(supabase/migrations/2026092*_*.sql, 20261011_portfolio_profile.sql). They are NOT
owned by this app's metadata: they are declared on a separate `LEGACY` MetaData, so
Alembic autogenerate never compares or alters them. Migration 0010 only *adds*:

- a nullable `aureon_user_id uuid` column (FK core.users, ON DELETE SET NULL) and an
  index on trades, expenses, secured and astra_documents;
- the new table core.finance_identities (Aureon user ↔ legacy text owner id).

The legacy `user_id text` column (Firebase / Supabase uid) and the composite primary
keys stay exactly as they are. Money columns are `double precision` in the legacy
schema; the API speaks Decimal and converts at the boundary (docs/decisions.md D-A3).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

LEGACY = MetaData(schema="public")


def _owner() -> Column[uuid.UUID]:
    return Column("aureon_user_id", UUID(as_uuid=True), nullable=True)


TS_COL = DateTime(timezone=True)

trades = Table(
    "trades",
    LEGACY,
    Column("user_id", Text, primary_key=True),
    Column("id", Text, primary_key=True),
    Column("ist_date", Date, nullable=False),
    Column("ist_time", Text, nullable=False),
    Column("entry_at", TS_COL),
    Column("exit_at", TS_COL),
    Column("ts", TS_COL),
    Column("broker_offset_minutes", Integer, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("session", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("lot", Double, nullable=False),
    Column("entry", Double, nullable=False),
    Column("exit", Double, nullable=False),
    Column("move", Double, nullable=False),
    Column("pl", Double, nullable=False),
    Column("note", Text, nullable=False),
    Column("signal_id", Text),
    Column("time_estimated", Boolean, nullable=False),
    Column("created_at", TS_COL, nullable=False),
    Column("updated_at", TS_COL, nullable=False),
    _owner(),
)

expenses = Table(
    "expenses",
    LEGACY,
    Column("user_id", Text, primary_key=True),
    Column("id", Text, primary_key=True),
    Column("date", Date, nullable=False),
    Column("amount", Double, nullable=False),
    Column("category", Text, nullable=False),
    Column("note", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("recur_day", SmallInteger),
    Column("created_at", TS_COL, nullable=False),
    Column("updated_at", TS_COL, nullable=False),
    _owner(),
)

secured = Table(
    "secured",
    LEGACY,
    Column("user_id", Text, primary_key=True),
    Column("id", Text, primary_key=True),
    Column("date", Date, nullable=False),
    Column("amt", Double, nullable=False),
    Column("note", Text, nullable=False),
    Column("created_at", TS_COL, nullable=False),
    Column("updated_at", TS_COL, nullable=False),
    _owner(),
)

astra_documents = Table(
    "astra_documents",
    LEGACY,
    Column("namespace", Text, primary_key=True),
    Column("doc_id", Text, primary_key=True),
    Column("user_id", Text),
    Column("data", JSONB, nullable=False),
    Column("updated_at", TS_COL, nullable=False),
    _owner(),
)

portfolio_profiles = Table(
    "portfolio_profiles",
    LEGACY,
    Column("id", Text, primary_key=True),
    Column("content", JSONB, nullable=False),
    Column("updated_at", TS_COL, nullable=False),
)

OWNED_TABLES = ("trades", "expenses", "secured", "astra_documents")


class FinanceIdentity(Base):
    """Which legacy owner id (Firebase / Supabase uid, text) belongs to an Aureon user.
    New rows written through the API use it as their `user_id`, so the old app keeps
    seeing them; without a link the Aureon user id (as text) is used."""

    __tablename__ = "finance_identities"
    __table_args__ = ({"schema": "core"},)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.users.id", ondelete="CASCADE"), unique=True
    )
    legacy_user_id: Mapped[str] = mapped_column(Text, unique=True)
    linked_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core.users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(TS_COL, server_default=func.now())


class FinanceTombstone(Base):
    """A deleted finance row or document, so GET /api/finance/changes can report deletes.
    `ref` is the row id, or "namespace/doc_id" for a document. Pruned after
    FINANCE_TOMBSTONE_RETENTION_DAYS by the purge-agent-history job."""

    __tablename__ = "finance_tombstones"
    __table_args__ = (
        Index("ix_finance_tombstones_user_deleted", "aureon_user_id", "deleted_at"),
        {"schema": "core"},
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    aureon_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.users.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)  # trades | expenses | secured | document
    ref: Mapped[str] = mapped_column(Text)
    deleted_at: Mapped[datetime] = mapped_column(TS_COL, server_default=func.now())


class FinanceAttachment(Base):
    """A receipt file on the HDD, attached to one of the owner's trades/expenses/secured rows.
    `storage_path` is relative to DATA_ROOT and built from UUIDs only."""

    __tablename__ = "finance_attachments"
    __table_args__ = (
        Index("ix_finance_attachments_owner_ref", "aureon_user_id", "kind", "ref"),
        {"schema": "core"},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    aureon_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core.users.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)  # trades | expenses | secured
    ref: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Text)
    storage_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS_COL, server_default=func.now())
