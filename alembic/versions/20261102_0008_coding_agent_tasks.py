"""Phase 5b: agents.code_tasks (the restricted coding agent's jobs).

Additive only: one NEW table in the existing agents schema. Downgrade drops exactly it.
Generated with `alembic revision --autogenerate` from app/models/agents.py and reviewed.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.


def upgrade() -> None:
    op.create_table('code_tasks',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('repo', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('instruction', sa.Text(), nullable=False),
    sa.Column('paths', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('status', sa.String(length=10), server_default=sa.text("'queued'"), nullable=False),
    sa.Column('base_ref', sa.String(length=100), nullable=False),
    sa.Column('base_sha', sa.String(length=64), nullable=True),
    sa.Column('branch', sa.String(length=100), nullable=True),
    sa.Column('worktree', sa.Text(), nullable=True),
    sa.Column('commit_sha', sa.String(length=64), nullable=True),
    sa.Column('model', sa.String(length=200), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('tests', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('decided_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("kind IN ('analyze','change')", name=op.f('ck_code_tasks_kind')),
    sa.CheckConstraint("status IN ('queued','running','ready','failed','approved','rejected')", name=op.f('ck_code_tasks_status')),
    sa.ForeignKeyConstraint(['created_by'], ['core.users.id'], name=op.f('fk_code_tasks_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['decided_by'], ['core.users.id'], name=op.f('fk_code_tasks_decided_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_code_tasks')),
    schema='agents'
    )
    op.create_index('ix_agents_code_tasks_status_created_at', 'code_tasks', ['status', 'created_at'], unique=False, schema='agents')


def downgrade() -> None:
    op.drop_index('ix_agents_code_tasks_status_created_at', table_name='code_tasks', schema='agents')
    op.drop_table('code_tasks', schema='agents')
