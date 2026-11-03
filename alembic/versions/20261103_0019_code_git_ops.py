"""Coding agent git ops: agents.code_git_ops (clone / pull / push + PR / task approval queue).

Additive only: one NEW table in the existing agents schema. Downgrade drops exactly it.
Numbered 0019 on top of 0018 (another branch adds 0018 first).

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "code_git_ops",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("op", sa.String(length=8), nullable=False),
        sa.Column("repo", sa.String(length=201), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("branch", sa.String(length=200), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=10), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column(
            "result",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("op IN ('clone','pull','push','task')", name=op.f("ck_code_git_ops_op")),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed')", name=op.f("ck_code_git_ops_status")
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["core.users.id"],
            name=op.f("fk_code_git_ops_requested_by_users"), ondelete="SET NULL",
        ),  # fmt: skip
        sa.ForeignKeyConstraint(
            ["task_id"], ["agents.code_tasks.id"],
            name=op.f("fk_code_git_ops_task_id_code_tasks"), ondelete="SET NULL",
        ),  # fmt: skip
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_git_ops")),
        schema="agents",
    )
    op.create_index(
        "ix_agents_code_git_ops_status_created_at",
        "code_git_ops",
        ["status", "created_at"],
        unique=False,
        schema="agents",
    )


def downgrade() -> None:
    op.drop_index("ix_agents_code_git_ops_status_created_at", table_name="code_git_ops", schema="agents")
    op.drop_table("code_git_ops", schema="agents")
