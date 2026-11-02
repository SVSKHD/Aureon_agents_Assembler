"""Trading Phase A: trading.*, dhan.*, mt5.* (broker-agnostic core + broker stubs).

Additive only: creates three NEW schemas (trading, dhan, mt5) and 22 NEW tables. No existing
table is touched (the legacy public.trades table is left alone). Downgrade drops exactly
these tables and then the three schemas without CASCADE.

Generated with `alembic revision --autogenerate` from app/models/trading.py and
app/models/brokers.py, then reviewed; `alembic check` confirms models == schema.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-02
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.


SCHEMAS = ("trading", "dhan", "mt5")


def upgrade() -> None:
    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    op.create_table('instruments',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('broker', sa.String(length=16), nullable=False),
    sa.Column('broker_instrument_id', sa.String(length=64), nullable=False),
    sa.Column('symbol', sa.String(length=100), nullable=False),
    sa.Column('exchange', sa.String(length=16), nullable=True),
    sa.Column('segment', sa.String(length=32), nullable=True),
    sa.Column('instrument_type', sa.String(length=32), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('lot_size', sa.Numeric(precision=24, scale=6), nullable=False),
    sa.Column('tick_size', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('expiry', sa.Date(), nullable=True),
    sa.Column('strike', sa.Numeric(precision=24, scale=8), nullable=True),
    sa.Column('option_type', sa.String(length=4), nullable=True),
    sa.Column('underlying', sa.String(length=100), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("broker IN ('dhan','mt5')", name=op.f('ck_instruments_broker')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_instruments')),
    sa.UniqueConstraint('broker', 'broker_instrument_id', name='uq_instruments_broker_broker_instrument_id'),
    schema='trading'
    )
    op.create_index('ix_trading_instruments_symbol', 'instruments', ['symbol'], unique=False, schema='trading')
    op.create_table('instruments',
    sa.Column('exchange_segment', sa.String(length=16), nullable=False),
    sa.Column('security_id', sa.String(length=32), nullable=False),
    sa.Column('trading_symbol', sa.String(length=100), nullable=False),
    sa.Column('instrument', sa.String(length=32), nullable=True),
    sa.Column('lot_size', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('tick_size', sa.Numeric(precision=24, scale=8), nullable=True),
    sa.Column('expiry_date', sa.Date(), nullable=True),
    sa.Column('strike_price', sa.Numeric(precision=24, scale=8), nullable=True),
    sa.Column('option_type', sa.String(length=4), nullable=True),
    sa.Column('underlying_security_id', sa.String(length=32), nullable=True),
    sa.Column('trading_instrument_id', sa.BigInteger(), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['trading_instrument_id'], ['trading.instruments.id'], name=op.f('fk_instruments_trading_instrument_id_instruments'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('exchange_segment', 'security_id', name='pk_instruments'),
    schema='dhan'
    )
    op.create_index('ix_dhan_instruments_trading_symbol', 'instruments', ['trading_symbol'], unique=False, schema='dhan')
    op.create_table('symbols',
    sa.Column('server', sa.String(length=100), nullable=False),
    sa.Column('symbol', sa.String(length=64), nullable=False),
    sa.Column('digits', sa.Integer(), nullable=True),
    sa.Column('point', sa.Numeric(precision=24, scale=8), nullable=True),
    sa.Column('contract_size', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('volume_min', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('volume_step', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('volume_max', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('currency_base', sa.String(length=8), nullable=True),
    sa.Column('currency_profit', sa.String(length=8), nullable=True),
    sa.Column('currency_margin', sa.String(length=8), nullable=True),
    sa.Column('trading_instrument_id', sa.BigInteger(), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['trading_instrument_id'], ['trading.instruments.id'], name=op.f('fk_symbols_trading_instrument_id_instruments'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('server', 'symbol', name='pk_symbols'),
    schema='mt5'
    )
    op.create_table('market_candles',
    sa.Column('instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('timeframe', sa.String(length=4), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('open', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('high', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('low', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('close', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('volume', sa.Numeric(precision=24, scale=6), server_default=sa.text('0'), nullable=False),
    sa.Column('oi', sa.Numeric(precision=24, scale=6), nullable=True),
    sa.Column('session', sa.String(length=8), nullable=False),
    sa.Column('source', sa.String(length=16), server_default=sa.text("'broker'"), nullable=False),
    sa.Column('is_gap', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("session IN ('Asia','London','NY')", name=op.f('ck_market_candles_session')),
    sa.CheckConstraint("timeframe IN ('1m','5m','15m','30m','1h','4h','1d')", name=op.f('ck_market_candles_timeframe')),
    sa.CheckConstraint('high >= low', name=op.f('ck_market_candles_high_low')),
    sa.ForeignKeyConstraint(['instrument_id'], ['trading.instruments.id'], name=op.f('fk_market_candles_instrument_id_instruments'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('instrument_id', 'timeframe', 'ts', name='pk_market_candles'),
    schema='trading'
    )
    op.create_table('broker_accounts',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('broker', sa.String(length=16), nullable=False),
    sa.Column('account_id', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=True),
    sa.Column('owner_user_id', sa.Uuid(), nullable=False),
    sa.Column('mode', sa.String(length=8), server_default=sa.text("'OBSERVE'"), nullable=False),
    sa.Column('mode_changed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('mode_changed_by', sa.Uuid(), nullable=True),
    sa.Column('base_currency', sa.String(length=3), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("broker IN ('dhan','mt5')", name=op.f('ck_broker_accounts_broker')),
    sa.CheckConstraint("mode IN ('OBSERVE','PAPER','LIVE')", name=op.f('ck_broker_accounts_mode')),
    sa.ForeignKeyConstraint(['mode_changed_by'], ['core.users.id'], name=op.f('fk_broker_accounts_mode_changed_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['core.users.id'], name=op.f('fk_broker_accounts_owner_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_broker_accounts')),
    sa.UniqueConstraint('broker', 'account_id', name='uq_broker_accounts_broker_account_id'),
    schema='trading'
    )
    op.create_index(op.f('ix_trading_broker_accounts_owner_user_id'), 'broker_accounts', ['owner_user_id'], unique=False, schema='trading')
    op.create_table('strategies',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('owner_user_id', sa.Uuid(), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['owner_user_id'], ['core.users.id'], name=op.f('fk_strategies_owner_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_strategies')),
    sa.UniqueConstraint('name', name='uq_strategies_name'),
    schema='trading'
    )
    op.create_table('account_profiles',
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('dhan_client_id', sa.String(length=32), nullable=False),
    sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('active_segments', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('data_plan', sa.String(length=32), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('refreshed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_account_profiles_broker_account_id_broker_accounts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('broker_account_id', name=op.f('pk_account_profiles')),
    schema='dhan'
    )
    op.create_table('fund_snapshots',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('available_balance', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('sod_limit', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('collateral', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('utilized', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('withdrawable', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_fund_snapshots_broker_account_id_broker_accounts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_fund_snapshots')),
    schema='dhan'
    )
    op.create_index('ix_dhan_fund_snapshots_broker_account_id_as_of', 'fund_snapshots', ['broker_account_id', 'as_of'], unique=False, schema='dhan')
    op.create_table('account_snapshots',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('balance', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('equity', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('margin', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('free_margin', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('margin_level', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('profit', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_account_snapshots_broker_account_id_broker_accounts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_account_snapshots')),
    schema='mt5'
    )
    op.create_index('ix_mt5_account_snapshots_broker_account_id_as_of', 'account_snapshots', ['broker_account_id', 'as_of'], unique=False, schema='mt5')
    op.create_table('terminals',
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('login', sa.String(length=32), nullable=False),
    sa.Column('server', sa.String(length=100), nullable=False),
    sa.Column('company', sa.String(length=100), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('leverage', sa.Integer(), nullable=True),
    sa.Column('terminal_build', sa.Integer(), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_terminals_broker_account_id_broker_accounts'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('broker_account_id', name=op.f('pk_terminals')),
    schema='mt5'
    )
    op.create_table('positions_snapshots',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('instrument_id', sa.BigInteger(), nullable=True),
    sa.Column('symbol', sa.String(length=100), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=24, scale=6), nullable=False),
    sa.Column('avg_price', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('last_price', sa.Numeric(precision=24, scale=8), nullable=True),
    sa.Column('unrealized_pnl', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('realized_pnl', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('source', sa.String(length=12), nullable=False),
    sa.Column('as_of', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("source IN ('broker','simulated','calculated')", name=op.f('ck_positions_snapshots_source')),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_positions_snapshots_broker_account_id_broker_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['instrument_id'], ['trading.instruments.id'], name=op.f('fk_positions_snapshots_instrument_id_instruments'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_positions_snapshots')),
    schema='trading'
    )
    op.create_index('ix_trading_positions_snapshots_broker_account_id_as_of', 'positions_snapshots', ['broker_account_id', 'as_of'], unique=False, schema='trading')
    op.create_table('risk_policies',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('max_daily_loss', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('max_exposure', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('max_position_size', sa.Numeric(precision=24, scale=6), nullable=False),
    sa.Column('max_drawdown_pct', sa.Numeric(precision=6, scale=3), nullable=False),
    sa.Column('allowed_sessions', postgresql.ARRAY(sa.String(length=8)), server_default=sa.text("ARRAY['Asia','London','NY']::varchar[]"), nullable=False),
    sa.Column('kill_switch', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('updated_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('max_daily_loss >= 0 AND max_exposure >= 0 AND max_position_size >= 0', name=op.f('ck_risk_policies_non_negative')),
    sa.CheckConstraint('max_drawdown_pct >= 0 AND max_drawdown_pct <= 100', name=op.f('ck_risk_policies_drawdown')),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_risk_policies_broker_account_id_broker_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['core.users.id'], name=op.f('fk_risk_policies_updated_by_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_policies')),
    sa.UniqueConstraint('broker_account_id', name='uq_risk_policies_broker_account_id'),
    schema='trading'
    )
    op.create_table('strategy_versions',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('strategy_id', sa.Uuid(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('code_ref', sa.String(length=100), nullable=False),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('version >= 1', name=op.f('ck_strategy_versions_version')),
    sa.ForeignKeyConstraint(['created_by'], ['core.users.id'], name=op.f('fk_strategy_versions_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['strategy_id'], ['trading.strategies.id'], name=op.f('fk_strategy_versions_strategy_id_strategies'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_strategy_versions')),
    sa.UniqueConstraint('strategy_id', 'version', name='uq_strategy_versions_strategy_id_version'),
    schema='trading'
    )
    op.create_table('backtest_runs',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('strategy_version_id', sa.Uuid(), nullable=False),
    sa.Column('instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('timeframe', sa.String(length=4), nullable=False),
    sa.Column('period_start', sa.DateTime(timezone=True), nullable=False),
    sa.Column('period_end', sa.DateTime(timezone=True), nullable=False),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('costs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('status', sa.String(length=8), server_default=sa.text("'queued'"), nullable=False),
    sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('queued','running','done','failed')", name=op.f('ck_backtest_runs_status')),
    sa.CheckConstraint("timeframe IN ('1m','5m','15m','30m','1h','4h','1d')", name=op.f('ck_backtest_runs_timeframe')),
    sa.CheckConstraint('period_end > period_start', name=op.f('ck_backtest_runs_period')),
    sa.ForeignKeyConstraint(['created_by'], ['core.users.id'], name=op.f('fk_backtest_runs_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['instrument_id'], ['trading.instruments.id'], name=op.f('fk_backtest_runs_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['strategy_version_id'], ['trading.strategy_versions.id'], name=op.f('fk_backtest_runs_strategy_version_id_strategy_versions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_backtest_runs')),
    schema='trading'
    )
    op.create_table('trading_signals',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('strategy_version_id', sa.Uuid(), nullable=False),
    sa.Column('instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('timeframe', sa.String(length=4), nullable=False),
    sa.Column('candle_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('session', sa.String(length=8), nullable=False),
    sa.Column('side', sa.String(length=8), nullable=False),
    sa.Column('strength', sa.Numeric(precision=6, scale=4), nullable=True),
    sa.Column('inputs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("session IN ('Asia','London','NY')", name=op.f('ck_trading_signals_session')),
    sa.CheckConstraint("side IN ('BUY','SELL','NO_TRADE')", name=op.f('ck_trading_signals_side')),
    sa.CheckConstraint("timeframe IN ('1m','5m','15m','30m','1h','4h','1d')", name=op.f('ck_trading_signals_timeframe')),
    sa.ForeignKeyConstraint(['instrument_id'], ['trading.instruments.id'], name=op.f('fk_trading_signals_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['strategy_version_id'], ['trading.strategy_versions.id'], name=op.f('fk_trading_signals_strategy_version_id_strategy_versions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trading_signals')),
    sa.UniqueConstraint('strategy_version_id', 'instrument_id', 'timeframe', 'candle_time', name='uq_trading_signals_dedupe'),
    schema='trading'
    )
    op.create_index('ix_trading_trading_signals_instrument_id_candle_time', 'trading_signals', ['instrument_id', 'candle_time'], unique=False, schema='trading')
    op.create_table('ai_analyses',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('signal_id', sa.Uuid(), nullable=True),
    sa.Column('backtest_run_id', sa.Uuid(), nullable=True),
    sa.Column('model', sa.String(length=200), nullable=False),
    sa.Column('summary', sa.Text(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('(signal_id IS NULL) <> (backtest_run_id IS NULL)', name=op.f('ck_ai_analyses_one_subject')),
    sa.ForeignKeyConstraint(['backtest_run_id'], ['trading.backtest_runs.id'], name=op.f('fk_ai_analyses_backtest_run_id_backtest_runs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['signal_id'], ['trading.trading_signals.id'], name=op.f('fk_ai_analyses_signal_id_trading_signals'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_analyses')),
    schema='trading'
    )
    op.create_index(op.f('ix_trading_ai_analyses_backtest_run_id'), 'ai_analyses', ['backtest_run_id'], unique=False, schema='trading')
    op.create_index(op.f('ix_trading_ai_analyses_signal_id'), 'ai_analyses', ['signal_id'], unique=False, schema='trading')
    op.create_table('backtest_trades',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('backtest_run_id', sa.Uuid(), nullable=False),
    sa.Column('side', sa.String(length=4), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=24, scale=6), nullable=False),
    sa.Column('entry_time', sa.DateTime(timezone=True), nullable=False),
    sa.Column('entry_price', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('exit_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('exit_price', sa.Numeric(precision=24, scale=8), nullable=True),
    sa.Column('pnl', sa.Numeric(precision=20, scale=4), nullable=True),
    sa.Column('fees', sa.Numeric(precision=20, scale=4), server_default=sa.text('0'), nullable=False),
    sa.Column('session', sa.String(length=8), nullable=False),
    sa.CheckConstraint("session IN ('Asia','London','NY')", name=op.f('ck_backtest_trades_session')),
    sa.CheckConstraint("side IN ('BUY','SELL')", name=op.f('ck_backtest_trades_side')),
    sa.ForeignKeyConstraint(['backtest_run_id'], ['trading.backtest_runs.id'], name=op.f('fk_backtest_trades_backtest_run_id_backtest_runs'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_backtest_trades')),
    schema='trading'
    )
    op.create_index('ix_trading_backtest_trades_backtest_run_id_entry_time', 'backtest_trades', ['backtest_run_id', 'entry_time'], unique=False, schema='trading')
    op.create_table('order_intents',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('signal_id', sa.Uuid(), nullable=True),
    sa.Column('side', sa.String(length=4), nullable=False),
    sa.Column('order_type', sa.String(length=12), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=24, scale=6), nullable=False),
    sa.Column('limit_price', sa.Numeric(precision=24, scale=8), nullable=True),
    sa.Column('stop_price', sa.Numeric(precision=24, scale=8), nullable=True),
    sa.Column('product', sa.String(length=16), nullable=True),
    sa.Column('validity', sa.String(length=8), server_default=sa.text("'DAY'"), nullable=False),
    sa.Column('mode', sa.String(length=8), nullable=False),
    sa.Column('status', sa.String(length=16), server_default=sa.text("'PENDING_RISK'"), nullable=False),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=True),
    sa.Column('correlation_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("mode IN ('OBSERVE','PAPER','LIVE')", name=op.f('ck_order_intents_mode')),
    sa.CheckConstraint("order_type IN ('MARKET','LIMIT','STOP','STOP_LIMIT')", name=op.f('ck_order_intents_order_type')),
    sa.CheckConstraint("side IN ('BUY','SELL')", name=op.f('ck_order_intents_side')),
    sa.CheckConstraint("source IN ('manual','strategy')", name=op.f('ck_order_intents_source')),
    sa.CheckConstraint("status IN ('PENDING_RISK','REJECTED','APPROVED','SUBMITTED','FILLED','CANCELLED','EXPIRED')", name=op.f('ck_order_intents_status')),
    sa.CheckConstraint('quantity > 0', name=op.f('ck_order_intents_quantity')),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_order_intents_broker_account_id_broker_accounts'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['created_by'], ['core.users.id'], name=op.f('fk_order_intents_created_by_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['instrument_id'], ['trading.instruments.id'], name=op.f('fk_order_intents_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['signal_id'], ['trading.trading_signals.id'], name=op.f('fk_order_intents_signal_id_trading_signals'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_intents')),
    sa.UniqueConstraint('correlation_id', name='uq_order_intents_correlation_id'),
    schema='trading'
    )
    op.create_index('ix_trading_order_intents_broker_account_id_created_at', 'order_intents', ['broker_account_id', 'created_at'], unique=False, schema='trading')
    op.create_table('broker_orders',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('order_intent_id', sa.Uuid(), nullable=True),
    sa.Column('broker_order_id', sa.String(length=64), nullable=False),
    sa.Column('instrument_id', sa.BigInteger(), nullable=True),
    sa.Column('symbol', sa.String(length=100), nullable=False),
    sa.Column('side', sa.String(length=4), nullable=False),
    sa.Column('order_type', sa.String(length=12), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=24, scale=6), nullable=False),
    sa.Column('filled_quantity', sa.Numeric(precision=24, scale=6), server_default=sa.text('0'), nullable=False),
    sa.Column('price', sa.Numeric(precision=24, scale=8), nullable=True),
    sa.Column('avg_fill_price', sa.Numeric(precision=24, scale=8), nullable=True),
    sa.Column('raw_status', sa.String(length=32), nullable=False),
    sa.Column('normalized_status', sa.String(length=12), nullable=False),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('placed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint("normalized_status IN ('OPEN','PARTIAL','FILLED','CANCELLED','REJECTED','EXPIRED','UNKNOWN')", name=op.f('ck_broker_orders_normalized_status')),
    sa.CheckConstraint("side IN ('BUY','SELL')", name=op.f('ck_broker_orders_side')),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_broker_orders_broker_account_id_broker_accounts'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['instrument_id'], ['trading.instruments.id'], name=op.f('fk_broker_orders_instrument_id_instruments'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['order_intent_id'], ['trading.order_intents.id'], name=op.f('fk_broker_orders_order_intent_id_order_intents'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_broker_orders')),
    sa.UniqueConstraint('broker_account_id', 'broker_order_id', name='uq_broker_orders_account_order'),
    schema='trading'
    )
    op.create_index(op.f('ix_trading_broker_orders_order_intent_id'), 'broker_orders', ['order_intent_id'], unique=False, schema='trading')
    op.create_table('risk_decisions',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('order_intent_id', sa.Uuid(), nullable=True),
    sa.Column('signal_id', sa.Uuid(), nullable=True),
    sa.Column('policy_id', sa.Uuid(), nullable=True),
    sa.Column('approved', sa.Boolean(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('policy_snapshot', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('inputs', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('checks', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_risk_decisions_broker_account_id_broker_accounts'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['order_intent_id'], ['trading.order_intents.id'], name=op.f('fk_risk_decisions_order_intent_id_order_intents'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['policy_id'], ['trading.risk_policies.id'], name=op.f('fk_risk_decisions_policy_id_risk_policies'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['signal_id'], ['trading.trading_signals.id'], name=op.f('fk_risk_decisions_signal_id_trading_signals'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_risk_decisions')),
    schema='trading'
    )
    op.create_index('ix_trading_risk_decisions_broker_account_id_created_at', 'risk_decisions', ['broker_account_id', 'created_at'], unique=False, schema='trading')
    op.create_index(op.f('ix_trading_risk_decisions_order_intent_id'), 'risk_decisions', ['order_intent_id'], unique=False, schema='trading')
    op.create_table('executions',
    sa.Column('id', sa.Uuid(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('broker_account_id', sa.Uuid(), nullable=False),
    sa.Column('broker_order_row_id', sa.Uuid(), nullable=True),
    sa.Column('order_intent_id', sa.Uuid(), nullable=True),
    sa.Column('instrument_id', sa.BigInteger(), nullable=False),
    sa.Column('broker_trade_id', sa.String(length=64), nullable=True),
    sa.Column('side', sa.String(length=4), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=24, scale=6), nullable=False),
    sa.Column('price', sa.Numeric(precision=24, scale=8), nullable=False),
    sa.Column('fees', sa.Numeric(precision=20, scale=4), server_default=sa.text('0'), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('executed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('session', sa.String(length=8), nullable=False),
    sa.Column('is_simulated', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("session IN ('Asia','London','NY')", name=op.f('ck_executions_session')),
    sa.CheckConstraint("side IN ('BUY','SELL')", name=op.f('ck_executions_side')),
    sa.CheckConstraint('quantity > 0 AND price > 0 AND fees >= 0', name=op.f('ck_executions_amounts')),
    sa.ForeignKeyConstraint(['broker_account_id'], ['trading.broker_accounts.id'], name=op.f('fk_executions_broker_account_id_broker_accounts'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['broker_order_row_id'], ['trading.broker_orders.id'], name=op.f('fk_executions_broker_order_row_id_broker_orders'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['instrument_id'], ['trading.instruments.id'], name=op.f('fk_executions_instrument_id_instruments'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['order_intent_id'], ['trading.order_intents.id'], name=op.f('fk_executions_order_intent_id_order_intents'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_executions')),
    schema='trading'
    )
    op.create_index('ix_trading_executions_broker_account_id_executed_at', 'executions', ['broker_account_id', 'executed_at'], unique=False, schema='trading')
    op.create_index('uq_executions_account_broker_trade', 'executions', ['broker_account_id', 'broker_trade_id'], unique=True, schema='trading', postgresql_where=sa.text('broker_trade_id IS NOT NULL'))
    op.create_table('order_events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column('order_intent_id', sa.Uuid(), nullable=True),
    sa.Column('broker_order_row_id', sa.Uuid(), nullable=True),
    sa.Column('event_type', sa.String(length=32), nullable=False),
    sa.Column('correlation_id', sa.String(length=64), nullable=True),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('order_intent_id IS NOT NULL OR broker_order_row_id IS NOT NULL', name=op.f('ck_order_events_has_subject')),
    sa.ForeignKeyConstraint(['broker_order_row_id'], ['trading.broker_orders.id'], name=op.f('fk_order_events_broker_order_row_id_broker_orders'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['order_intent_id'], ['trading.order_intents.id'], name=op.f('fk_order_events_order_intent_id_order_intents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_events')),
    schema='trading'
    )
    op.create_index(op.f('ix_trading_order_events_broker_order_row_id'), 'order_events', ['broker_order_row_id'], unique=False, schema='trading')
    op.create_index(op.f('ix_trading_order_events_correlation_id'), 'order_events', ['correlation_id'], unique=False, schema='trading')
    op.create_index(op.f('ix_trading_order_events_order_intent_id'), 'order_events', ['order_intent_id'], unique=False, schema='trading')


def downgrade() -> None:
    op.drop_index(op.f('ix_trading_order_events_order_intent_id'), table_name='order_events', schema='trading')
    op.drop_index(op.f('ix_trading_order_events_correlation_id'), table_name='order_events', schema='trading')
    op.drop_index(op.f('ix_trading_order_events_broker_order_row_id'), table_name='order_events', schema='trading')
    op.drop_table('order_events', schema='trading')
    op.drop_index('uq_executions_account_broker_trade', table_name='executions', schema='trading', postgresql_where=sa.text('broker_trade_id IS NOT NULL'))
    op.drop_index('ix_trading_executions_broker_account_id_executed_at', table_name='executions', schema='trading')
    op.drop_table('executions', schema='trading')
    op.drop_index(op.f('ix_trading_risk_decisions_order_intent_id'), table_name='risk_decisions', schema='trading')
    op.drop_index('ix_trading_risk_decisions_broker_account_id_created_at', table_name='risk_decisions', schema='trading')
    op.drop_table('risk_decisions', schema='trading')
    op.drop_index(op.f('ix_trading_broker_orders_order_intent_id'), table_name='broker_orders', schema='trading')
    op.drop_table('broker_orders', schema='trading')
    op.drop_index('ix_trading_order_intents_broker_account_id_created_at', table_name='order_intents', schema='trading')
    op.drop_table('order_intents', schema='trading')
    op.drop_index('ix_trading_backtest_trades_backtest_run_id_entry_time', table_name='backtest_trades', schema='trading')
    op.drop_table('backtest_trades', schema='trading')
    op.drop_index(op.f('ix_trading_ai_analyses_signal_id'), table_name='ai_analyses', schema='trading')
    op.drop_index(op.f('ix_trading_ai_analyses_backtest_run_id'), table_name='ai_analyses', schema='trading')
    op.drop_table('ai_analyses', schema='trading')
    op.drop_index('ix_trading_trading_signals_instrument_id_candle_time', table_name='trading_signals', schema='trading')
    op.drop_table('trading_signals', schema='trading')
    op.drop_table('backtest_runs', schema='trading')
    op.drop_table('strategy_versions', schema='trading')
    op.drop_table('risk_policies', schema='trading')
    op.drop_index('ix_trading_positions_snapshots_broker_account_id_as_of', table_name='positions_snapshots', schema='trading')
    op.drop_table('positions_snapshots', schema='trading')
    op.drop_table('terminals', schema='mt5')
    op.drop_index('ix_mt5_account_snapshots_broker_account_id_as_of', table_name='account_snapshots', schema='mt5')
    op.drop_table('account_snapshots', schema='mt5')
    op.drop_index('ix_dhan_fund_snapshots_broker_account_id_as_of', table_name='fund_snapshots', schema='dhan')
    op.drop_table('fund_snapshots', schema='dhan')
    op.drop_table('account_profiles', schema='dhan')
    op.drop_table('strategies', schema='trading')
    op.drop_index(op.f('ix_trading_broker_accounts_owner_user_id'), table_name='broker_accounts', schema='trading')
    op.drop_table('broker_accounts', schema='trading')
    op.drop_table('market_candles', schema='trading')
    op.drop_table('symbols', schema='mt5')
    op.drop_index('ix_dhan_instruments_trading_symbol', table_name='instruments', schema='dhan')
    op.drop_table('instruments', schema='dhan')
    op.drop_index('ix_trading_instruments_symbol', table_name='instruments', schema='trading')
    op.drop_table('instruments', schema='trading')
    for schema in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema}")
