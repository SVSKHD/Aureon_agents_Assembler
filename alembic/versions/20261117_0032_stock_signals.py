"""Stock Signals (docs/37-stock-signals.md): EMA 8/32 + session VWAP runs, states, events.

Additive only. Creates in schema `trading`:

- stock_signal_runs (uuid PK): one evaluation per (timeframe, boundary), unique, so a
  candle-close trigger, a scheduled scan and a retry never evaluate the same close twice.
- stock_signal_events (bigint identity PK): one row per EMA crossover, unique on
  (instrument_id, timeframe, direction, crossed_ts); a VWAP confirmation updates the row.
- stock_signal_states (instrument_id, timeframe PK): the latest classification and the
  previous one.

No existing table or column is touched. Downgrade drops only these three tables.
Grants: `trading` is in deploy/sql/roles.sql's DML schemas (default privileges cover the
tables and the identity sequence).

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.

S = "trading"
PRICE = sa.Numeric(24, 8)
SCORE = sa.Numeric(6, 2)
TS = sa.DateTime(timezone=True)
STATES = (
    "('BULLISH_CONFIRMED','BULLISH_PENDING','BEARISH_CONFIRMED','BEARISH_PENDING',"
    "'BULLISH_TREND','BEARISH_TREND','MIXED','UNAVAILABLE')"
)
TFS = "('5m','15m','1h','1d')"


def _json() -> sa.Column:  # type: ignore[type-arg]
    return sa.Column(
        "metrics",
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "stock_signal_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("timeframe", sa.String(4), nullable=False),
        sa.Column("boundary", TS, nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("universe_size", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("history_requested", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("evaluated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("unavailable", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("events_new", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("confirmations", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_signal_runs")),
        sa.UniqueConstraint("timeframe", "boundary", name="uq_stock_signal_runs_tf_boundary"),
        sa.CheckConstraint(
            "status IN ('queued','running','done','failed')",
            name=op.f("ck_stock_signal_runs_status"),
        ),
        sa.CheckConstraint(f"timeframe IN {TFS}", name=op.f("ck_stock_signal_runs_timeframe")),
        schema=S,
    )
    op.create_index(
        "ix_trading_stock_signal_runs_status_created",
        "stock_signal_runs",
        ["status", "created_at"],
        schema=S,
    )

    op.create_table(
        "stock_signal_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column("timeframe", sa.String(4), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("crossed_ts", TS, nullable=False),
        sa.Column("crossed_at", TS, nullable=False),
        sa.Column("confirmed_at", TS, nullable=True),
        sa.Column("ema_fast", PRICE, nullable=True),
        sa.Column("ema_slow", PRICE, nullable=True),
        sa.Column("vwap", PRICE, nullable=True),
        sa.Column("last_price", PRICE, nullable=True),
        sa.Column("confirm_price", PRICE, nullable=True),
        sa.Column("confirm_vwap", PRICE, nullable=True),
        sa.Column("candle_close_at", TS, nullable=False),
        sa.Column("data_source", sa.String(16), server_default=sa.text("'dhan'"), nullable=False),
        sa.Column("data_as_of", TS, nullable=True),
        sa.Column("trend_score", SCORE, nullable=True),
        _json(),
        sa.Column("in_holdings", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_stock_signal_events")),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["trading.instruments.id"],
            name=op.f("fk_stock_signal_events_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["trading.stock_signal_runs.id"],
            name=op.f("fk_stock_signal_events_run_id_stock_signal_runs"),
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "instrument_id",
            "timeframe",
            "direction",
            "crossed_ts",
            name="uq_stock_signal_events_cross",
        ),
        sa.CheckConstraint(
            "direction IN ('bullish','bearish')", name=op.f("ck_stock_signal_events_direction")
        ),
        sa.CheckConstraint(f"state IN {STATES}", name=op.f("ck_stock_signal_events_state")),
        sa.CheckConstraint(f"timeframe IN {TFS}", name=op.f("ck_stock_signal_events_timeframe")),
        schema=S,
    )
    op.create_index(
        "ix_trading_stock_signal_events_crossed",
        "stock_signal_events",
        ["timeframe", sa.text("crossed_at DESC")],
        schema=S,
    )
    op.create_index(
        "ix_trading_stock_signal_events_created", "stock_signal_events", ["created_at"], schema=S
    )

    op.create_table(
        "stock_signal_states",
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("timeframe", sa.String(4), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("previous_state", sa.String(20), nullable=True),
        sa.Column("state_changed_at", TS, nullable=True),
        sa.Column("candle_ts", TS, nullable=True),
        sa.Column("candle_close_at", TS, nullable=True),
        sa.Column("last_price", PRICE, nullable=True),
        sa.Column("ema_fast", PRICE, nullable=True),
        sa.Column("ema_slow", PRICE, nullable=True),
        sa.Column("vwap", PRICE, nullable=True),
        sa.Column("vwap_applicable", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("trend_score", SCORE, nullable=True),
        _json(),
        sa.Column("in_holdings", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("data_source", sa.String(16), server_default=sa.text("'dhan'"), nullable=False),
        sa.Column("evaluated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("instrument_id", "timeframe", name="pk_stock_signal_states"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["trading.instruments.id"],
            name=op.f("fk_stock_signal_states_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["trading.stock_signal_events.id"],
            name=op.f("fk_stock_signal_states_event_id_stock_signal_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["trading.stock_signal_runs.id"],
            name=op.f("fk_stock_signal_states_run_id_stock_signal_runs"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(f"state IN {STATES}", name=op.f("ck_stock_signal_states_state")),
        sa.CheckConstraint(
            f"previous_state IN {STATES}", name=op.f("ck_stock_signal_states_previous_state")
        ),
        sa.CheckConstraint(f"timeframe IN {TFS}", name=op.f("ck_stock_signal_states_timeframe")),
        schema=S,
    )
    op.create_index(
        "ix_trading_stock_signal_states_tf_state",
        "stock_signal_states",
        ["timeframe", "state"],
        schema=S,
    )


def downgrade() -> None:
    op.drop_table("stock_signal_states", schema=S)
    op.drop_table("stock_signal_events", schema=S)
    op.drop_table("stock_signal_runs", schema=S)
