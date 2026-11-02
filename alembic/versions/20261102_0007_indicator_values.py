"""Trading Phase D: trading.indicator_values (technical-analysis output).

Additive only: one NEW table in the existing trading schema. Downgrade drops exactly it.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0007'
down_revision: str | None = '0006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.


def upgrade() -> None:
    op.create_table('indicator_values',
    sa.Column('instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('timeframe', sa.String(length=4), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('values', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('computed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("timeframe IN ('1m','5m','15m','30m','1h','4h','1d')", name=op.f('ck_indicator_values_timeframe')),
    sa.ForeignKeyConstraint(['instrument_id'], ['trading.instruments.id'], name=op.f('fk_indicator_values_instrument_id_instruments'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('instrument_id', 'timeframe', 'name', 'ts', name='pk_indicator_values'),
    schema='trading'
    )


def downgrade() -> None:
    op.drop_table('indicator_values', schema='trading')
