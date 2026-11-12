"""Claude gateway tables (docs/26-ai-providers.md).

Additive only:
- ai.conversations.agent: the Claude agent a conversation talks to (NULL on older rows).
- ai.messages.blocks: the exact API content blocks as sent / received, replayed next turn.
- ai.tool_calls: every tool Claude asked for (input + short summary, never raw tables).
- ai.usage: every Claude call (tokens, cost, latency; never content). Budgets read it.
- ai.approvals: write-tool requests; only the backend executes them, after an admin approves.

The provider-era tables (ai.model_calls, ai.model_inventory) are kept as history.
Grants: schema `ai` is in deploy/sql/roles.sql's DML schemas (default privileges).

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.


def upgrade() -> None:
    op.add_column("conversations", sa.Column("agent", sa.String(16)), schema="ai")
    op.add_column("messages", sa.Column("blocks", postgresql.JSONB()), schema="ai")

    op.create_table(
        "tool_calls",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid,
            sa.ForeignKey("ai.conversations.id", ondelete="CASCADE"),
        ),
        sa.Column("agent", sa.String(16), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("tool_use_id", sa.String(64), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('ok','error','refused','awaiting_approval')", name=op.f("ck_tool_calls_status")
        ),
        sa.CheckConstraint("kind IN ('read','write')", name=op.f("ck_tool_calls_kind")),
        schema="ai",
    )
    op.create_index(
        "ix_ai_tool_calls_conversation_id_at",
        "tool_calls",
        ["conversation_id", "at"],
        schema="ai",
    )

    op.create_table(
        "usage",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("agent", sa.String(16), nullable=False),
        sa.Column("budget", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column(
            "conversation_id",
            sa.Uuid,
            sa.ForeignKey("ai.conversations.id", ondelete="SET NULL"),
        ),
        sa.Column("request_id", sa.String(64)),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column("stop_reason", sa.String(32)),
        sa.Column("error", sa.String(64)),
        sa.CheckConstraint(
            "outcome IN ('reserved','ok','error','refused')", name=op.f("ck_usage_outcome")
        ),
        sa.CheckConstraint(
            "budget IN ('investments','business','chat','reserve')", name=op.f("ck_usage_budget")
        ),
        schema="ai",
    )
    op.create_index("ix_ai_usage_at", "usage", ["at"], schema="ai")
    op.create_index("ix_ai_usage_budget_at", "usage", ["budget", "at"], schema="ai")

    op.create_table(
        "approvals",
        sa.Column(
            "id", sa.Uuid, primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column(
            "conversation_id",
            sa.Uuid,
            sa.ForeignKey("ai.conversations.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "tool_call_id",
            sa.BigInteger,
            sa.ForeignKey("ai.tool_calls.id", ondelete="SET NULL"),
        ),
        sa.Column("requested_by", sa.Uuid, sa.ForeignKey("core.users.id", ondelete="SET NULL")),
        sa.Column("agent", sa.String(16), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.Uuid, sa.ForeignKey("core.users.id", ondelete="SET NULL")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("error", sa.Text),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','executed','failed','expired')",
            name=op.f("ck_approvals_status"),
        ),
        schema="ai",
    )
    op.create_index(
        "ix_ai_approvals_status_created_at",
        "approvals",
        ["status", "created_at"],
        schema="ai",
    )


def downgrade() -> None:
    op.drop_index("ix_ai_approvals_status_created_at", table_name="approvals", schema="ai")
    op.drop_table("approvals", schema="ai")
    op.drop_index("ix_ai_usage_budget_at", table_name="usage", schema="ai")
    op.drop_index("ix_ai_usage_at", table_name="usage", schema="ai")
    op.drop_table("usage", schema="ai")
    op.drop_index("ix_ai_tool_calls_conversation_id_at", table_name="tool_calls", schema="ai")
    op.drop_table("tool_calls", schema="ai")
    op.drop_column("messages", "blocks", schema="ai")
    op.drop_column("conversations", "agent", schema="ai")
