"""Agents: agents.agents, agent_runs, agent_logs, scheduled_jobs.

Additive only: four NEW tables in the existing `agents` schema (created by 0004).
Downgrade drops only these four tables.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
JSON_EMPTY = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS agents")

    op.create_table(
        "agents",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), server_default=sa.text("'idle'"), nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("version", sa.String(64), nullable=True),
        sa.Column("host", sa.String(255), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default=JSON_EMPTY, nullable=False),
        sa.Column("registered_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("last_heartbeat_at", TS, nullable=True),
        sa.Column("last_activity_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_agents"),
        sa.CheckConstraint("state IN ('idle','busy','error','stopped')", name="ck_agents_state"),
        schema="agents",
    )

    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("task", sa.String(100), nullable=False),
        sa.Column("cron", sa.String(100), nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("max_retries", sa.SmallInteger(), server_default=sa.text("3"), nullable=False),
        sa.Column("retry_backoff_seconds", sa.Integer(), server_default=sa.text("60"),
                  nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default=sa.text("300"),
                  nullable=False),
        sa.Column("attempt", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"),
                  nullable=False),
        sa.Column("next_run_at", TS, nullable=True),
        sa.Column("last_run_at", TS, nullable=True),
        sa.Column("last_status", sa.String(16), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("locked_until", TS, nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_scheduled_jobs"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agents.id"], ondelete="CASCADE",
                                name="fk_scheduled_jobs_agent_id_agents"),
        schema="agents",
    )
    op.create_index("ix_agents_scheduled_jobs_next_run_at", "scheduled_jobs",
                    ["next_run_at"], schema="agents")

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("scheduled_job_id", sa.String(64), nullable=True),
        sa.Column("task", sa.String(100), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'running'"), nullable=False),
        sa.Column("attempt", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("started_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default=JSON_EMPTY, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agents.id"], ondelete="CASCADE",
                                name="fk_agent_runs_agent_id_agents"),
        sa.ForeignKeyConstraint(["scheduled_job_id"], ["agents.scheduled_jobs.id"],
                                ondelete="SET NULL",
                                name="fk_agent_runs_scheduled_job_id_scheduled_jobs"),
        sa.CheckConstraint("status IN ('running','succeeded','failed','cancelled')",
                           name="ck_agent_runs_status"),
        schema="agents",
    )
    op.create_index("ix_agents_agent_runs_agent_id_started_at", "agent_runs",
                    ["agent_id", "started_at"], schema="agents")

    op.create_table(
        "agent_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", postgresql.JSONB(), server_default=JSON_EMPTY, nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_agent_logs"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agents.id"], ondelete="CASCADE",
                                name="fk_agent_logs_agent_id_agents"),
        sa.ForeignKeyConstraint(["run_id"], ["agents.agent_runs.id"], ondelete="SET NULL",
                                name="fk_agent_logs_run_id_agent_runs"),
        sa.CheckConstraint("level IN ('debug','info','warning','error')",
                           name="ck_agent_logs_level"),
        schema="agents",
    )
    op.create_index("ix_agents_agent_logs_agent_id_created_at", "agent_logs",
                    ["agent_id", "created_at"], schema="agents")


def downgrade() -> None:
    op.drop_table("agent_logs", schema="agents")
    op.drop_table("agent_runs", schema="agents")
    op.drop_table("scheduled_jobs", schema="agents")
    op.drop_table("agents", schema="agents")
