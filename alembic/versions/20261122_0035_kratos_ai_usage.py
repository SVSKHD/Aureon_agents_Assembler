"""KRATOS research calls in ai.usage (docs/39-kratos-ai-gateway.md).

Two changes to ai.usage and nothing else, so the KRATOS route can record its calls:

* ck_usage_budget also accepts 'kratos' (was investments, business, chat, reserve).
* agent widens from varchar(16) to varchar(64): KRATOS labels are "kratos:<role>".
  Widening a varchar is a catalogue change in Postgres; no row is rewritten.

No row is changed and no other table is touched. A build that does not know this revision
keeps working on the widened table: it only ever writes the four older budgets and labels of
at most 16 characters.

Downgrade never deletes or shortens a usage record. It puts the four-budget check back
(as NOT VALID while 'kratos' rows exist, so history stays and new ones are refused; validated
when there are none) and narrows agent to varchar(16) only if every stored label fits.

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

S = "ai"
CK = "ck_usage_budget"
OLD = "budget IN ('investments','business','chat','reserve')"
NEW = "budget IN ('investments','business','chat','reserve','kratos')"


def upgrade() -> None:
    op.drop_constraint(op.f(CK), "usage", schema=S, type_="check")
    op.create_check_constraint(op.f(CK), "usage", NEW, schema=S)
    op.alter_column(
        "usage", "agent", type_=sa.String(64), existing_type=sa.String(16),
        existing_nullable=False, schema=S,
    )  # fmt: skip


def downgrade() -> None:
    bind = op.get_bind()
    kratos_rows = bind.scalar(sa.text("SELECT count(*) FROM ai.usage WHERE budget = 'kratos'"))
    long_labels = bind.scalar(sa.text("SELECT count(*) FROM ai.usage WHERE length(agent) > 16"))
    op.drop_constraint(op.f(CK), "usage", schema=S, type_="check")
    op.execute(f"ALTER TABLE ai.usage ADD CONSTRAINT {CK} CHECK ({OLD}) NOT VALID")
    if not kratos_rows:
        op.execute(f"ALTER TABLE ai.usage VALIDATE CONSTRAINT {CK}")
    if not long_labels:
        op.alter_column(
            "usage", "agent", type_=sa.String(16), existing_type=sa.String(64),
            existing_nullable=False, schema=S,
        )  # fmt: skip
