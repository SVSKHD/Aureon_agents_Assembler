"""MT5 records API (docs/36-mt5-records.md): what the 1.7.x bot records, and long-history bars.

Additive only. Uses the existing schema `mt5` (created in 0006, extended in 0018) and creates:

- mt5.records (record_id uuid PK = the bot's idempotency key): kind, symbol, account_login,
  utc_time, server_time (broker clock, no zone), bot_version, bot_id, payload jsonb (every key
  of the row that is not one of the seven envelope keys), batch_id, received_at.
  Indexes: (kind, utc_time desc), (symbol, utc_time desc), (account_login, utc_time desc),
  GIN (payload jsonb_path_ops), (received_at).
- mt5.bars (symbol, timeframe, ts PK; ts = bar open time UTC): server_ts, OHLC numeric(18,6),
  tick_volume, spread, ema8/ema32/ema200/atr14 as the bot computed them, source,
  received_at. Index (timeframe, ts). Never purged. mt5.candles (0018) is not reused: it is
  push/v1's table and is purged after MT5_CANDLE_RETENTION_DAYS.

No existing table or column is touched. Downgrade drops only these two tables.
Grants: schema `mt5` is in deploy/sql/roles.sql's DML schemas (default privileges), so the
app role gets SELECT/INSERT/UPDATE/DELETE on both tables without a grant here.

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.

S = "mt5"
PRICE = sa.Numeric(18, 6)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS mt5")
    op.create_table(
        "records",
        sa.Column("record_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=True),
        sa.Column("account_login", sa.BigInteger(), nullable=True),
        sa.Column("utc_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("server_time", sa.DateTime(timezone=False), nullable=True),
        sa.Column("bot_version", sa.Text(), nullable=False),
        sa.Column("bot_id", sa.Text(), server_default=sa.text("'aureon_mt5_2026'"), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("record_id", name=op.f("pk_records")),
        schema=S,
    )
    op.create_index(
        "ix_mt5_records_kind_utc_time",
        "records",
        ["kind", sa.text("utc_time DESC")],
        schema=S,
    )
    op.create_index(
        "ix_mt5_records_symbol_utc_time",
        "records",
        ["symbol", sa.text("utc_time DESC")],
        schema=S,
    )
    op.create_index(
        "ix_mt5_records_account_login_utc_time",
        "records",
        ["account_login", sa.text("utc_time DESC")],
        schema=S,
    )
    op.create_index(
        "ix_mt5_records_payload",
        "records",
        ["payload"],
        schema=S,
        postgresql_using="gin",
        postgresql_ops={"payload": "jsonb_path_ops"},
    )
    op.create_index("ix_mt5_records_received_at", "records", ["received_at"], schema=S)

    op.create_table(
        "bars",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("timeframe", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("server_ts", sa.DateTime(timezone=False), nullable=True),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("tick_volume", sa.BigInteger(), nullable=True),
        sa.Column("spread", sa.Integer(), nullable=True),
        sa.Column("ema8", PRICE, nullable=True),
        sa.Column("ema32", PRICE, nullable=True),
        sa.Column("ema200", PRICE, nullable=True),
        sa.Column("atr14", PRICE, nullable=True),
        sa.Column("source", sa.Text(), server_default=sa.text("'bot'"), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("symbol", "timeframe", "ts", name="pk_bars"),
        schema=S,
    )
    op.create_index("ix_mt5_bars_timeframe_ts", "bars", ["timeframe", "ts"], schema=S)


def downgrade() -> None:
    op.drop_index("ix_mt5_bars_timeframe_ts", table_name="bars", schema=S)
    op.drop_table("bars", schema=S)
    op.drop_index("ix_mt5_records_received_at", table_name="records", schema=S)
    op.drop_index("ix_mt5_records_payload", table_name="records", schema=S)
    op.drop_index("ix_mt5_records_account_login_utc_time", table_name="records", schema=S)
    op.drop_index("ix_mt5_records_symbol_utc_time", table_name="records", schema=S)
    op.drop_index("ix_mt5_records_kind_utc_time", table_name="records", schema=S)
    op.drop_table("records", schema=S)
