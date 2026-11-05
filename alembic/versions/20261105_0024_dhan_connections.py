"""Dhan connection state and Dhan incidents (docs/28-dhan-operations.md).

1. NEW table dhan.connections: safe connection metadata only, never a token.
2. agents.incidents may now have source 'dhan' (the Dhan monitor). The check constraint is
   widened (drop + re-create with one more value); no row changes.

Downgrade drops the table and puts the narrower source check back as NOT VALID, so rows
written with source 'dhan' survive a rollback (new rows must use the old sources again).

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = (
    "connected",
    "unverified",
    "expired",
    "rejected",
    "mismatch",
    "disconnected",
    "not_configured",
)


INCIDENT_CHECK = "ck_incidents_source"
OLD_SOURCES = "'exception','heartbeat','docker','actions','db','api'"
NEW_SOURCES = OLD_SOURCES + ",'dhan'"


def upgrade() -> None:
    op.drop_constraint(INCIDENT_CHECK, "incidents", schema="agents", type_="check")
    op.create_check_constraint(
        INCIDENT_CHECK, "incidents", f"source IN ({NEW_SOURCES})", schema="agents"
    )
    op.create_table(
        "connections",
        sa.Column("dhan_client_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "token_source", sa.String(length=8), server_default=sa.text("'none'"), nullable=False
        ),
        sa.Column("credential_fp", sa.String(length=32), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected_by", sa.Uuid(), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_by", sa.Uuid(), nullable=True),
        sa.Column("data_plan", sa.String(length=32), nullable=True),
        sa.Column("data_valid_until", sa.String(length=40), nullable=True),
        sa.Column(
            "active_segments",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),  # fmt: skip
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_outcome", sa.String(length=24), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in STATES) + ")",
            name=op.f("ck_connections_status"),
        ),
        sa.ForeignKeyConstraint(
            ["connected_by"],
            ["core.users.id"],
            name=op.f("fk_connections_connected_by_users"),
            ondelete="SET NULL",
        ),  # fmt: skip
        sa.ForeignKeyConstraint(
            ["disconnected_by"],
            ["core.users.id"],
            name=op.f("fk_connections_disconnected_by_users"),
            ondelete="SET NULL",
        ),  # fmt: skip
        sa.PrimaryKeyConstraint("dhan_client_id", name=op.f("pk_connections")),
        schema="dhan",
    )


def downgrade() -> None:
    op.drop_table("connections", schema="dhan")
    op.drop_constraint(INCIDENT_CHECK, "incidents", schema="agents", type_="check")
    op.execute(
        f"ALTER TABLE agents.incidents ADD CONSTRAINT {INCIDENT_CHECK} "
        f"CHECK (source IN ({OLD_SOURCES})) NOT VALID"
    )
