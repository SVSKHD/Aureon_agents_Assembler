"""Baseline: no schema changes.

Running `alembic upgrade head` against the existing `spasta` database only creates
Alembic's own version table (public.aureon_alembic_version). Existing tables are untouched.

Revision ID: 0001
Revises:
Create Date: 2026-11-01
"""
from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
