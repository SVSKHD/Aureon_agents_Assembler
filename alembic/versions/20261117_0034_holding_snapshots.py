"""Dhan holdings snapshots (docs/37 §6): the portfolio signal map's holdings.

Additive only. Creates trading.holding_snapshots (uuid PK): broker_account_id →
trading.broker_accounts (cascade), instrument_id → trading.instruments (set null), symbol,
isin, quantity, available_quantity, avg_cost, last_price, as_of. Written by the existing
dhan-reconcile job next to positions and funds; read-only market/broker data.

No existing table or column is touched. Downgrade drops only this table.

Revision ID: 0034
Revises: 0033
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.

S = "trading"
PRICE = sa.Numeric(24, 8)
QTY = sa.Numeric(24, 6)


def upgrade() -> None:
    op.create_table(
        "holding_snapshots",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("broker_account_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=True),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("isin", sa.String(16), nullable=True),
        sa.Column("quantity", QTY, nullable=False),
        sa.Column("available_quantity", QTY, nullable=False),
        sa.Column("avg_cost", PRICE, nullable=False),
        sa.Column("last_price", PRICE, nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_holding_snapshots")),
        sa.ForeignKeyConstraint(
            ["broker_account_id"],
            ["trading.broker_accounts.id"],
            name=op.f("fk_holding_snapshots_broker_account_id_broker_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["trading.instruments.id"],
            name=op.f("fk_holding_snapshots_instrument_id_instruments"),
            ondelete="SET NULL",
        ),
        schema=S,
    )
    op.create_index(
        "ix_trading_holding_snapshots_account_as_of",
        "holding_snapshots",
        ["broker_account_id", sa.text("as_of DESC")],
        schema=S,
    )
    op.create_index(
        "ix_trading_holding_snapshots_instrument", "holding_snapshots", ["instrument_id"], schema=S
    )


def downgrade() -> None:
    op.drop_table("holding_snapshots", schema=S)
