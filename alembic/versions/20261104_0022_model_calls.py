"""Agent model calls: ai.model_calls (provider, model, tokens, latency, cost; no content).

Additive only: one NEW table in the existing ai schema. Downgrade drops exactly it.
See docs/model-routing.md.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_calls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("route_reason", sa.String(length=40), nullable=True),
        sa.Column("fallback", sa.Boolean(), nullable=False),
        sa.Column("local_only", sa.Boolean(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=14, scale=8), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_calls")),
        schema="ai",
    )
    op.create_index("ix_model_calls_agent_at", "model_calls", ["agent", "at"], schema="ai")
    op.create_index("ix_model_calls_at", "model_calls", ["at"], schema="ai")


def downgrade() -> None:
    op.drop_index("ix_model_calls_at", table_name="model_calls", schema="ai")
    op.drop_index("ix_model_calls_agent_at", table_name="model_calls", schema="ai")
    op.drop_table("model_calls", schema="ai")
