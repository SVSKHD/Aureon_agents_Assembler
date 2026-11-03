"""AI providers: per-conversation provider + context, per-message provider/fallback/cost.

docs/25-openrouter.md. Additive only:

- ai.conversations: `provider` varchar(16) NOT NULL DEFAULT 'ollama' (check: ollama |
  openrouter) and `context` varchar(16) NOT NULL DEFAULT 'general' (check: general | finance
  | trading | research). The existing `model` column keeps the picked model, so no new model
  column. Existing rows become ollama / general, which is what they were.
- ai.messages: nullable `provider` varchar(16), `fallback_reason` varchar(64) and
  `cost_usd` numeric(12,6), filled for assistant turns from now on.

No existing column is altered. Downgrade drops only what this revision added.

Revision ID: 0020
Revises: 0019 (0018 MT5 ingest, 0019 coding git ops)
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("provider", sa.String(16), server_default=sa.text("'ollama'"), nullable=False),
        schema="ai",
    )
    op.add_column(
        "conversations",
        sa.Column("context", sa.String(16), server_default=sa.text("'general'"), nullable=False),
        schema="ai",
    )
    op.create_check_constraint(
        "ck_conversations_provider",
        "conversations",
        "provider IN ('ollama','openrouter')",
        schema="ai",
    )
    op.create_check_constraint(
        "ck_conversations_context",
        "conversations",
        "context IN ('general','finance','trading','research')",
        schema="ai",
    )
    op.add_column("messages", sa.Column("provider", sa.String(16), nullable=True), schema="ai")
    op.add_column(
        "messages", sa.Column("fallback_reason", sa.String(64), nullable=True), schema="ai"
    )
    op.add_column("messages", sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True), schema="ai")


def downgrade() -> None:
    op.drop_column("messages", "cost_usd", schema="ai")
    op.drop_column("messages", "fallback_reason", schema="ai")
    op.drop_column("messages", "provider", schema="ai")
    op.drop_constraint("ck_conversations_context", "conversations", schema="ai", type_="check")
    op.drop_constraint("ck_conversations_provider", "conversations", schema="ai", type_="check")
    op.drop_column("conversations", "context", schema="ai")
    op.drop_column("conversations", "provider", schema="ai")
