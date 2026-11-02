"""Trading Phase F: trading.paper_ledger and trading.reconciliations (paper trading).

Additive only: two NEW tables in the existing trading schema. Downgrade drops exactly them.
Generated with `alembic revision --autogenerate` and reviewed.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.


def upgrade() -> None:
    op.create_table('reconciliations',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.Column('drift', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_reconciliations_broker_account_id_broker_accounts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_reconciliations')),
    schema='trading'
    )
    op.create_index('ix_trading_reconciliations_broker_account_id_created_at', 'reconciliations', ['broker_account_id', 'created_at'], unique=False, schema='trading')
    op.create_table('paper_ledger',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('execution_id', sa.Uuid(), nullable=True),
    sa.Column('order_intent_id', sa.Uuid(), nullable=True),
    sa.Column('instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('realized','fees')", name=op.f('ck_paper_ledger_kind')),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_paper_ledger_broker_account_id_broker_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['execution_id'], ['trading.executions.id'], name=op.f('fk_paper_ledger_execution_id_executions'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['instrument_id'], ['trading.instruments.id'], name=op.f('fk_paper_ledger_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['order_intent_id'], ['trading.order_intents.id'], name=op.f('fk_paper_ledger_order_intent_id_order_intents'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_paper_ledger')),
    schema='trading'
    )
    op.create_index('ix_trading_paper_ledger_broker_account_id_created_at', 'paper_ledger', ['broker_account_id', 'created_at'], unique=False, schema='trading')


def downgrade() -> None:
    op.drop_index('ix_trading_paper_ledger_broker_account_id_created_at', table_name='paper_ledger', schema='trading')
    op.drop_table('paper_ledger', schema='trading')
    op.drop_index('ix_trading_reconciliations_broker_account_id_created_at', table_name='reconciliations', schema='trading')
    op.drop_table('reconciliations', schema='trading')
