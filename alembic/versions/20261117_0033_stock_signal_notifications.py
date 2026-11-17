"""Stock Signals notifications (docs/37 §5): per-user notifications and preferences.

Additive only. Creates in schema `trading`:

- stock_signal_notifications (bigint identity PK = the stream cursor): user_id → core.users
  (cascade), kind, event_id → stock_signal_events (set null), instrument_id, timeframe,
  title, body, payload jsonb, dedup_key, created_at, read_at. Unique (user_id, dedup_key).
- stock_signal_prefs (user_id PK → core.users, cascade): enabled, kinds[], timeframes[],
  all_market, confirmed_only, updated_at.

DevOps notifications (devops.*) and Discord alerts are not touched. Downgrade drops only
these two tables.

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.

S = "trading"
TS = sa.DateTime(timezone=True)
KINDS = (
    "('bullish_cross','bearish_cross','vwap_confirmed','holding_reversal',"
    "'portfolio_trend_change','replacement_candidate')"
)


def upgrade() -> None:
    op.create_table(
        "stock_signal_notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=True),
        sa.Column("instrument_id", sa.BigInteger(), nullable=True),
        sa.Column("timeframe", sa.String(4), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("dedup_key", sa.String(160), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("read_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_signal_notifications")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["core.users.id"],
            name=op.f("fk_stock_signal_notifications_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["trading.stock_signal_events.id"],
            name=op.f("fk_stock_signal_notifications_event_id_stock_signal_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["trading.instruments.id"],
            name=op.f("fk_stock_signal_notifications_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", "dedup_key", name="uq_stock_signal_notifications_dedup"),
        sa.CheckConstraint(f"kind IN {KINDS}", name=op.f("ck_stock_signal_notifications_kind")),
        schema=S,
    )
    op.create_index(
        "ix_trading_stock_signal_notifications_user_id",
        "stock_signal_notifications",
        ["user_id", sa.text("id DESC")],
        schema=S,
    )
    op.create_index(
        "ix_trading_stock_signal_notifications_unread",
        "stock_signal_notifications",
        ["user_id"],
        schema=S,
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.create_index(
        "ix_trading_stock_signal_notifications_created",
        "stock_signal_notifications",
        ["created_at"],
        schema=S,
    )

    op.create_table(
        "stock_signal_prefs",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "kinds", postgresql.ARRAY(sa.String(32)), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "timeframes",
            postgresql.ARRAY(sa.String(4)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("all_market", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("confirmed_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_stock_signal_prefs")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["core.users.id"],
            name=op.f("fk_stock_signal_prefs_user_id_users"),
            ondelete="CASCADE",
        ),
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("stock_signal_prefs", schema=S)
    op.drop_table("stock_signal_notifications", schema=S)
