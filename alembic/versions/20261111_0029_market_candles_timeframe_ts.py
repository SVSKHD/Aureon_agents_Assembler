"""Screener status index: newest candle per timeframe (docs/35-market-data.md).

Additive only: one index on trading.market_candles (timeframe, ts DESC) for real (non-gap)
candles, so GET /api/trading/screener/status reads "newest candle per timeframe" with an index
scan instead of a full scan of the candle table. The screener's per-instrument reads already
use pk_market_candles (instrument_id, timeframe, ts).

Built CONCURRENTLY (outside the migration transaction), so candle ingestion is never blocked on
a large table. If a previous concurrent build was interrupted it left an INVALID index; that one
is dropped and rebuilt. Downgrade drops exactly this index.

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.

INDEX = "ix_trading_market_candles_timeframe_ts"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            f"""
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'trading' AND c.relname = '{INDEX}' AND NOT i.indisvalid
              ) THEN
                EXECUTE 'DROP INDEX trading.{INDEX}';
              END IF;
            END $$;
            """
        )
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} "
            "ON trading.market_candles (timeframe, ts DESC) WHERE is_gap = false"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS trading.{INDEX}")
