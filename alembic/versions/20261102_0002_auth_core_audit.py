"""Auth: core.roles, core.users, core.sessions, audit.audit_events.

Additive only: creates two NEW schemas (core, audit) and four NEW tables. No existing
table is touched. Downgrade drops only what this revision created; DROP SCHEMA is
issued without CASCADE, so it fails rather than remove anything someone else put there.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS core")
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")

    roles = op.create_table(
        "roles",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("name", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
        schema="core",
    )
    op.bulk_insert(roles, [{"id": 1, "name": "admin"}, {"id": 2, "name": "user"}])

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role_id", sa.SmallInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
        sa.ForeignKeyConstraint(["role_id"], ["core.roles.id"], name="fk_users_role_id_roles"),
        schema="core",
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_sessions"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        sa.ForeignKeyConstraint(["user_id"], ["core.users.id"], ondelete="CASCADE",
                                name="fk_sessions_user_id_users"),
        schema="core",
    )
    op.create_index("ix_core_sessions_user_id", "sessions", ["user_id"], schema="core")

    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("target_user_id", sa.Uuid(), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_events"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["core.users.id"], ondelete="SET NULL",
                                name="fk_audit_events_actor_user_id_users"),
        sa.ForeignKeyConstraint(["target_user_id"], ["core.users.id"], ondelete="SET NULL",
                                name="fk_audit_events_target_user_id_users"),
        schema="audit",
    )
    op.create_index("ix_audit_audit_events_occurred_at", "audit_events", ["occurred_at"],
                    schema="audit")
    op.create_index("ix_audit_audit_events_event_type", "audit_events", ["event_type"],
                    schema="audit")


def downgrade() -> None:
    op.drop_table("audit_events", schema="audit")
    op.drop_table("sessions", schema="core")
    op.drop_table("users", schema="core")
    op.drop_table("roles", schema="core")
    op.execute("DROP SCHEMA IF EXISTS audit")
    op.execute("DROP SCHEMA IF EXISTS core")
