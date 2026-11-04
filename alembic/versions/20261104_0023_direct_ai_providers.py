"""Direct AI providers: allow 'anthropic' and 'gemini' on ai.conversations.provider.

docs/26-ai-providers.md. The only schema change the direct Anthropic/Gemini APIs need:

- ai.conversations: the `ck_conversations_provider` check is widened from
  (ollama, openrouter) to (ollama, openrouter, anthropic, gemini). Constraint swap only:
  no column, row or default is changed, and every existing row satisfies the new check.

ai.messages.provider and ai.model_calls.provider are varchar(16) without a check, so they
already accept the new names. Settings, keys and prices live in core.app_settings (0021).

Downgrade puts the narrower check back as NOT VALID: rows written with anthropic/gemini
while this revision was applied are kept untouched (never rewritten or deleted), and new
writes are held to the old list again.

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAME = "ck_conversations_provider"


def upgrade() -> None:
    op.drop_constraint(NAME, "conversations", schema="ai", type_="check")
    op.create_check_constraint(
        NAME,
        "conversations",
        "provider IN ('ollama','openrouter','anthropic','gemini')",
        schema="ai",
    )


def downgrade() -> None:
    op.drop_constraint(NAME, "conversations", schema="ai", type_="check")
    op.execute(
        f"ALTER TABLE ai.conversations ADD CONSTRAINT {NAME} "
        "CHECK (provider IN ('ollama','openrouter')) NOT VALID"
    )
