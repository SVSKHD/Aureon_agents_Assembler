"""Dhan market data, instrument search, watchlists and screener (docs/35-market-data.md).

Additive only:
- trading.instruments: nullable normalized master columns (trading_symbol, display_name,
  name, isin, series, security_type, underlying_security_id, inactive_reason) and search /
  derivative indexes. Existing rows keep working; the next instrument refresh fills them.
- dhan.instrument_sync_runs: one row per instrument-master refresh.
- trading.watchlists, trading.watchlist_items: per-user lists.
- trading.candle_backfill_requests: the history request queue.
- trading.screener_runs, trading.screener_results: screener scans.

A trigram index is created only when pg_trgm is already installed (the migrator is not
expected to have CREATE EXTENSION); search falls back to prefix indexes otherwise.

Downgrade drops exactly what this adds.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.

TS = sa.DateTime(timezone=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
TIMEFRAMES = "('1m','5m','15m','30m','1h','4h','1d')"
JOB_STATES = "('queued','running','done','failed','cancelled')"
NEW_INSTRUMENT_COLUMNS = (
    ("trading_symbol", sa.String(100)),
    ("display_name", sa.String(200)),
    ("name", sa.String(200)),
    ("isin", sa.String(16)),
    ("series", sa.String(16)),
    ("security_type", sa.String(16)),
    ("underlying_security_id", sa.String(32)),
    ("inactive_reason", sa.String(24)),
)


def upgrade() -> None:
    for name, type_ in NEW_INSTRUMENT_COLUMNS:
        op.add_column("instruments", sa.Column(name, type_, nullable=True), schema="trading")
    op.create_index(
        "ix_trading_instruments_broker_segment_active",
        "instruments",
        ["broker", "segment", "is_active"],
        schema="trading",
    )
    op.create_index(
        "ix_trading_instruments_underlying_expiry",
        "instruments",
        ["broker", "underlying", "expiry"],
        schema="trading",
        postgresql_where=sa.text("expiry IS NOT NULL"),
    )
    op.create_index("ix_trading_instruments_isin", "instruments", ["isin"], schema="trading")
    op.execute(
        "CREATE INDEX ix_trading_instruments_lower_trading_symbol ON trading.instruments "
        "(lower(trading_symbol) text_pattern_ops)"
    )
    op.execute(
        "CREATE INDEX ix_trading_instruments_lower_symbol ON trading.instruments "
        "(lower(symbol) text_pattern_ops)"
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
            EXECUTE 'CREATE INDEX IF NOT EXISTS ix_trading_instruments_search_trgm '
                    'ON trading.instruments USING gin ((lower(coalesce(trading_symbol, '''') '
                    '|| '' '' || coalesce(display_name, symbol) || '' '' || '
                    'coalesce(name, ''''))) gin_trgm_ops)';
          END IF;
        END $$;
        """
    )

    op.create_table(
        "instrument_sync_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("source_host", sa.String(200), nullable=True),
        sa.Column("segments", JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("rows_read", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_unparsed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "rows_skipped_segment", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("duplicates", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("upserted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("active", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("expired", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("deactivated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("by_segment", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint(
            "status IN ('running','succeeded','failed')",
            name=op.f("ck_instrument_sync_runs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instrument_sync_runs")),
        schema="dhan",
    )
    op.create_index(
        "ix_dhan_instrument_sync_runs_started_at",
        "instrument_sync_runs",
        ["started_at"],
        schema="dhan",
    )

    op.create_table(
        "watchlists",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["core.users.id"],
            name=op.f("fk_watchlists_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watchlists")),
        sa.UniqueConstraint("owner_user_id", "name", name="uq_watchlists_owner_name"),
        schema="trading",
    )
    op.create_index(
        "ix_trading_watchlists_owner_user_id", "watchlists", ["owner_user_id"], schema="trading"
    )
    op.create_table(
        "watchlist_items",
        sa.Column("watchlist_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("added_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["watchlist_id"],
            ["trading.watchlists.id"],
            name=op.f("fk_watchlist_items_watchlist_id_watchlists"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["trading.instruments.id"],
            name=op.f("fk_watchlist_items_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("watchlist_id", "instrument_id", name="pk_watchlist_items"),
        schema="trading",
    )
    op.create_index(
        "ix_trading_watchlist_items_instrument_id",
        "watchlist_items",
        ["instrument_id"],
        schema="trading",
    )

    op.create_table(
        "candle_backfill_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("timeframe", sa.String(4), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("source", sa.String(16), server_default=sa.text("'user'"), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("candles", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint(
            f"status IN {JOB_STATES}", name=op.f("ck_candle_backfill_requests_status")
        ),
        sa.CheckConstraint(
            f"timeframe IN {TIMEFRAMES}", name=op.f("ck_candle_backfill_requests_timeframe")
        ),
        sa.CheckConstraint("days >= 1", name=op.f("ck_candle_backfill_requests_days")),
        sa.CheckConstraint(
            "source IN ('user','universe','system')",
            name=op.f("ck_candle_backfill_requests_source"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["trading.instruments.id"],
            name=op.f("fk_candle_backfill_requests_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["core.users.id"],
            name=op.f("fk_candle_backfill_requests_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candle_backfill_requests")),
        schema="trading",
    )
    op.create_index(
        "uq_candle_backfill_open",
        "candle_backfill_requests",
        ["instrument_id", "timeframe"],
        unique=True,
        schema="trading",
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index(
        "ix_trading_candle_backfill_requests_status_created",
        "candle_backfill_requests",
        ["status", "created_at"],
        schema="trading",
    )

    op.create_table(
        "screener_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("timeframe", sa.String(4), nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(12), server_default=sa.text("'queued'"), nullable=False),
        sa.Column(
            "cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("universe_size", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("scanned", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("matched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("skipped", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("data_as_of", TS, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", TS, server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.CheckConstraint(f"status IN {JOB_STATES}", name=op.f("ck_screener_runs_status")),
        sa.CheckConstraint(f"timeframe IN {TIMEFRAMES}", name=op.f("ck_screener_runs_timeframe")),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["core.users.id"],
            name=op.f("fk_screener_runs_owner_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screener_runs")),
        schema="trading",
    )
    op.create_index(
        "ix_trading_screener_runs_owner_created",
        "screener_runs",
        ["owner_user_id", "created_at"],
        schema="trading",
    )
    op.create_index(
        "ix_trading_screener_runs_status_created",
        "screener_runs",
        ["status", "created_at"],
        schema="trading",
    )
    op.create_index(
        "ix_trading_screener_runs_owner_hash",
        "screener_runs",
        ["owner_user_id", "request_hash"],
        schema="trading",
    )
    op.create_table(
        "screener_results",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("instrument_id", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("candle_ts", TS, nullable=True),
        sa.Column("metrics", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("detail", sa.String(200), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('match','stale','insufficient_data','no_data','unsupported')",
            name=op.f("ck_screener_results_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["trading.screener_runs.id"],
            name=op.f("fk_screener_results_run_id_screener_runs"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["trading.instruments.id"],
            name=op.f("fk_screener_results_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "instrument_id", name="pk_screener_results"),
        schema="trading",
    )
    op.create_index(
        "ix_trading_screener_results_run_outcome",
        "screener_results",
        ["run_id", "outcome"],
        schema="trading",
    )


def downgrade() -> None:
    op.drop_table("screener_results", schema="trading")
    op.drop_table("screener_runs", schema="trading")
    op.drop_table("candle_backfill_requests", schema="trading")
    op.drop_table("watchlist_items", schema="trading")
    op.drop_table("watchlists", schema="trading")
    op.drop_table("instrument_sync_runs", schema="dhan")
    op.execute("DROP INDEX IF EXISTS trading.ix_trading_instruments_search_trgm")
    op.execute("DROP INDEX IF EXISTS trading.ix_trading_instruments_lower_symbol")
    op.execute("DROP INDEX IF EXISTS trading.ix_trading_instruments_lower_trading_symbol")
    op.drop_index("ix_trading_instruments_isin", table_name="instruments", schema="trading")
    op.drop_index(
        "ix_trading_instruments_underlying_expiry", table_name="instruments", schema="trading"
    )
    op.drop_index(
        "ix_trading_instruments_broker_segment_active", table_name="instruments", schema="trading"
    )
    for name, _ in reversed(NEW_INSTRUMENT_COLUMNS):
        op.drop_column("instruments", name, schema="trading")
