"""Finance (Prompt 3): Aureon ownership on the existing Spasta finance tables.

Additive only. For public.trades, public.expenses, public.secured and
public.astra_documents: a nullable `aureon_user_id uuid` (FK core.users, ON DELETE
SET NULL) and an index. Plus core.finance_identities (Aureon user ↔ legacy uid).

The legacy tables are created only if they are missing (a fresh database or CI), with
the column set of ASTRA-FRONTEND's supabase/migrations, minus the Supabase-only
`astra_uid()` defaults, RLS policies and Realtime publication. Where they exist they
are never altered beyond the added column and index.

Downgrade removes only what this revision added (the column, the index and
core.finance_identities). It never drops a legacy table.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.

OWNED = ("trades", "expenses", "secured", "astra_documents")

CREATE_IF_MISSING = """
create table if not exists public.astra_documents (
  namespace text not null,
  doc_id text not null,
  user_id text,
  data jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now(),
  primary key (namespace, doc_id)
);
create table if not exists public.trades (
  user_id text not null,
  id text not null,
  ist_date date not null,
  ist_time text not null default '',
  entry_at timestamptz,
  exit_at timestamptz,
  ts timestamptz,
  broker_offset_minutes integer not null default 180,
  symbol text not null default '',
  session text not null default 'London' check (session in ('Asia', 'London', 'NY')),
  side text not null default 'buy' check (side in ('buy', 'sell')),
  lot double precision not null default 1,
  entry double precision not null default 0,
  exit double precision not null default 0,
  move double precision not null default 0,
  pl double precision not null default 0,
  note text not null default '',
  signal_id text,
  time_estimated boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (user_id, id)
);
create table if not exists public.expenses (
  user_id text not null,
  id text not null,
  date date not null,
  amount double precision not null default 0 check (amount >= 0),
  category text not null default 'Other',
  note text not null default '',
  kind text not null default 'one-off' check (kind in ('one-off', 'recurring')),
  recur_day smallint check (recur_day between 1 and 31),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (user_id, id)
);
create table if not exists public.secured (
  user_id text not null,
  id text not null,
  date date not null,
  amt double precision not null default 0 check (amt >= 0),
  note text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (user_id, id)
);
create table if not exists public.portfolio_profiles (
  id text primary key,
  content jsonb not null check (jsonb_typeof(content) = 'object'),
  updated_at timestamptz not null default now()
);
"""


def upgrade() -> None:
    op.create_table(
        "finance_identities",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("legacy_user_id", sa.Text(), nullable=False),
        sa.Column("linked_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["linked_by"],
            ["core.users.id"],
            name=op.f("fk_finance_identities_linked_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["core.users.id"],
            name=op.f("fk_finance_identities_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_finance_identities")),
        sa.UniqueConstraint("legacy_user_id", name=op.f("uq_finance_identities_legacy_user_id")),
        sa.UniqueConstraint("user_id", name=op.f("uq_finance_identities_user_id")),
        schema="core",
    )
    for stmt in CREATE_IF_MISSING.split(";"):
        if stmt.strip():
            op.execute(stmt)
    for t in OWNED:
        op.execute(
            f"alter table public.{t} add column if not exists aureon_user_id uuid "
            f"constraint fk_{t}_aureon_user_id references core.users(id) on delete set null"
        )
        op.execute(
            f"create index if not exists ix_{t}_aureon_user_id on public.{t} (aureon_user_id)"
        )


def downgrade() -> None:
    for t in OWNED:
        op.execute(f"drop index if exists public.ix_{t}_aureon_user_id")
        op.execute(f"alter table public.{t} drop column if exists aureon_user_id")
    op.drop_table("finance_identities", schema="core")
