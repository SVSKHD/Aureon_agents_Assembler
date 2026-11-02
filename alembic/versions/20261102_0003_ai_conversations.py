"""AI: ai.conversations, ai.messages, ai.model_inventory.

Additive only: one NEW schema (ai) and three NEW tables. Downgrade drops only these and
the schema (no CASCADE).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS ai")

    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.ForeignKeyConstraint(["user_id"], ["core.users.id"], ondelete="CASCADE",
                                name="fk_conversations_user_id_users"),
        schema="ai",
    )
    op.create_index("ix_ai_conversations_user_id_updated_at", "conversations",
                    ["user_id", "updated_at"], schema="ai")

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model", sa.String(200), nullable=True),
        sa.Column("status", sa.String(16), server_default=sa.text("'complete'"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
        sa.ForeignKeyConstraint(["conversation_id"], ["ai.conversations.id"],
                                ondelete="CASCADE",
                                name="fk_messages_conversation_id_conversations"),
        sa.CheckConstraint("role IN ('system','user','assistant')", name="ck_messages_role"),
        sa.CheckConstraint("status IN ('complete','cancelled','error','timeout')",
                           name="ck_messages_status"),
        schema="ai",
    )
    op.create_index("ix_ai_messages_conversation_id_id", "messages",
                    ["conversation_id", "id"], schema="ai")

    op.create_table(
        "model_inventory",
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("digest", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("family", sa.String(100), nullable=True),
        sa.Column("parameter_size", sa.String(32), nullable=True),
        sa.Column("quantization", sa.String(32), nullable=True),
        sa.Column("modified_at", TS, nullable=True),
        sa.Column("first_seen_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("name", name="pk_model_inventory"),
        schema="ai",
    )


def downgrade() -> None:
    op.drop_table("model_inventory", schema="ai")
    op.drop_table("messages", schema="ai")
    op.drop_table("conversations", schema="ai")
    op.execute("DROP SCHEMA IF EXISTS ai")
