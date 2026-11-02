"""Imaging: agents.image_jobs, agents.generated_assets.

Additive only: creates schema `agents` (if missing) and two NEW tables. Downgrade drops
only these two tables, then the schema without CASCADE (so it is kept if a later phase
put other tables in it).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS agents")

    op.create_table(
        "image_jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"),
                  nullable=False),
        sa.Column("progress", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("negative_prompt", sa.Text(), nullable=True),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("steps", sa.SmallInteger(), nullable=False),
        sa.Column("width", sa.SmallInteger(), nullable=False),
        sa.Column("height", sa.SmallInteger(), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_image_jobs"),
        sa.ForeignKeyConstraint(["user_id"], ["core.users.id"], ondelete="CASCADE",
                                name="fk_image_jobs_user_id_users"),
        sa.CheckConstraint("status IN ('queued','running','done','failed','cancelled')",
                           name="ck_image_jobs_status"),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="ck_image_jobs_progress"),
        schema="agents",
    )
    op.create_index("ix_agents_image_jobs_status_created_at", "image_jobs",
                    ["status", "created_at"], schema="agents")
    op.create_index("ix_agents_image_jobs_user_id_created_at", "image_jobs",
                    ["user_id", "created_at"], schema="agents")

    op.create_table(
        "generated_assets",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("width", sa.SmallInteger(), nullable=False),
        sa.Column("height", sa.SmallInteger(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_generated_assets"),
        sa.UniqueConstraint("job_id", name="uq_generated_assets_job_id"),
        sa.ForeignKeyConstraint(["job_id"], ["agents.image_jobs.id"], ondelete="CASCADE",
                                name="fk_generated_assets_job_id_image_jobs"),
        sa.ForeignKeyConstraint(["user_id"], ["core.users.id"], ondelete="CASCADE",
                                name="fk_generated_assets_user_id_users"),
        schema="agents",
    )
    op.create_index("ix_agents_generated_assets_user_id_created_at", "generated_assets",
                    ["user_id", "created_at"], schema="agents")


def downgrade() -> None:
    op.drop_table("generated_assets", schema="agents")
    op.drop_table("image_jobs", schema="agents")
    # Kept if another revision still has tables in it (no CASCADE).
    op.execute(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname = 'agents') "
        "THEN DROP SCHEMA IF EXISTS agents; END IF; END $$"
    )
