"""MT5 push ingest (schema aureon.mt5.push/v1): what status/v1 storage lacked.

Additive only. Uses the existing schema `mt5` (created in 0006) and creates, IF NOT EXISTS:

- mt5.pushes (push_id uuid PK = idempotency key, seq, generated_at, received_at, size_bytes,
  events_new, events_duplicate). Index on received_at (retention).
- mt5.bot_snapshots (bot_id PK): the latest push per bot: guardian state, strategy config,
  health, signals_latest, plus the v1 blocks the dashboard reads (bot, agents, halt, console,
  ema921e, ema921e_trades, backtests, errors).
- mt5.events (id varchar PK = the bot's event id, so a re-sent event is ON CONFLICT DO
  NOTHING): t, event, kind (timeline badge), symbol, ticket, payload, push_id, received_at.
- mt5.failures (id identity, key, kind CHECK IN the six kinds, symbol NULL, reason, first_seen,
  last_seen, count, resolved_at, details). Partial unique index on key WHERE resolved_at IS
  NULL: one open row per (kind, symbol).
- mt5.candles (symbol, timeframe, ts PK; OHLCV numeric, ema jsonb) with an index on ts for
  the retention purge. trading.market_candles is not reused: it needs a registered
  trading.instruments row (FK) and is the broker feed's table.

No existing table or column is touched. Downgrade drops only these five tables.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.

S = "mt5"
FAILURE_KINDS = (
    "mt5_disconnected",
    "stale_ticks",
    "guardian_error",
    "discord_error",
    "exception",
    "stale_heartbeat",
)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def _json(default: str = "'{}'::jsonb") -> sa.TextClause:
    return sa.text(default)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS mt5")
    op.create_table(
        "pushes",
        sa.Column("push_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("size_bytes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("events_new", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("events_duplicate", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("push_id", name=op.f("pk_pushes")),
        schema=S,
        if_not_exists=True,
    )
    op.create_index(
        "ix_mt5_pushes_received_at",
        "pushes",
        ["received_at"],
        unique=False,
        schema=S,
        if_not_exists=True,
    )

    op.create_table(
        "bot_snapshots",
        sa.Column("bot_id", sa.String(length=64), nullable=False),
        sa.Column("push_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column("schema_name", sa.String(length=64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bot", JSONB, server_default=_json(), nullable=False),
        sa.Column("agents", JSONB, server_default=_json(), nullable=False),
        sa.Column("health", JSONB, server_default=_json(), nullable=False),
        sa.Column("guardian", JSONB, server_default=_json(), nullable=False),
        sa.Column("signals_latest", JSONB, server_default=_json(), nullable=False),
        sa.Column("strategy", JSONB, server_default=_json(), nullable=False),
        sa.Column("halt", JSONB, nullable=True),
        sa.Column("console", JSONB, nullable=True),
        sa.Column("ema921e", JSONB, nullable=True),
        sa.Column("ema921e_trades", JSONB, server_default=_json("'[]'::jsonb"), nullable=False),
        sa.Column("backtests", JSONB, server_default=_json("'[]'::jsonb"), nullable=False),
        sa.Column("errors", JSONB, server_default=_json(), nullable=False),
        sa.PrimaryKeyConstraint("bot_id", name=op.f("pk_bot_snapshots")),
        schema=S,
        if_not_exists=True,
    )

    op.create_table(
        "events",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("t", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("ticket", sa.BigInteger(), nullable=True),
        sa.Column("payload", JSONB, server_default=_json(), nullable=False),
        sa.Column("push_id", sa.Uuid(), nullable=True),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_events")),
        schema=S,
        if_not_exists=True,
    )
    op.create_index("ix_mt5_events_t", "events", ["t"], schema=S, if_not_exists=True)
    op.create_index(
        "ix_mt5_events_kind_t", "events", ["kind", "t"], schema=S, if_not_exists=True
    )
    op.create_index(
        "ix_mt5_events_symbol_t", "events", ["symbol", "t"], schema=S, if_not_exists=True
    )

    op.create_table(
        "failures",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("details", JSONB, server_default=_json(), nullable=False),
        sa.CheckConstraint(f"kind IN {FAILURE_KINDS}", name=op.f("ck_failures_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_failures")),
        schema=S,
        if_not_exists=True,
    )
    op.create_index(
        "uq_mt5_failures_open_key",
        "failures",
        ["key"],
        unique=True,
        schema=S,
        postgresql_where=sa.text("resolved_at IS NULL"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_mt5_failures_last_seen", "failures", ["last_seen"], schema=S, if_not_exists=True
    )

    op.create_table(
        "candles",
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=4), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(24, 8), nullable=False),
        sa.Column("high", sa.Numeric(24, 8), nullable=False),
        sa.Column("low", sa.Numeric(24, 8), nullable=False),
        sa.Column("close", sa.Numeric(24, 8), nullable=False),
        sa.Column("volume", sa.Numeric(24, 6), nullable=True),
        sa.Column("ema", JSONB, server_default=_json(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("symbol", "timeframe", "ts", name="pk_candles"),
        schema=S,
        if_not_exists=True,
    )
    op.create_index("ix_mt5_candles_ts", "candles", ["ts"], schema=S, if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_mt5_candles_ts", table_name="candles", schema=S, if_exists=True)
    op.drop_table("candles", schema=S, if_exists=True)
    op.drop_index("ix_mt5_failures_last_seen", table_name="failures", schema=S, if_exists=True)
    op.drop_index(
        "uq_mt5_failures_open_key",
        table_name="failures",
        schema=S,
        postgresql_where=sa.text("resolved_at IS NULL"),
        if_exists=True,
    )
    op.drop_table("failures", schema=S, if_exists=True)
    op.drop_index("ix_mt5_events_symbol_t", table_name="events", schema=S, if_exists=True)
    op.drop_index("ix_mt5_events_kind_t", table_name="events", schema=S, if_exists=True)
    op.drop_index("ix_mt5_events_t", table_name="events", schema=S, if_exists=True)
    op.drop_table("events", schema=S, if_exists=True)
    op.drop_table("bot_snapshots", schema=S, if_exists=True)
    op.drop_index("ix_mt5_pushes_received_at", table_name="pushes", schema=S, if_exists=True)
    op.drop_table("pushes", schema=S, if_exists=True)
