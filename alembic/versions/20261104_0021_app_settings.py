"""Runtime settings and secrets editable from Spasta: core.app_settings.

Additive only: one NEW table in the existing core schema. Downgrade drops exactly it.
Values are Fernet ciphertext (SETTINGS_MASTER_KEY); see docs/settings.md.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("is_secret", sa.Boolean(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["core.users.id"],
            name=op.f("fk_app_settings_updated_by_users"), ondelete="SET NULL",
        ),  # fmt: skip
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_settings")),
        schema="core",
    )


def downgrade() -> None:
    op.drop_table("app_settings", schema="core")
