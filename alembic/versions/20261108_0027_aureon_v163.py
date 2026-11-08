"""Aureon V1.6.3 database API storage (docs/34-aureon-v163.md).

Additive only. Creates the NEW schema `aureon` with the tables of the V1.6.3 package's own
migrations 001-004 (same names, columns and keys, so V1.6.3's queries and its Linux services
read the same layout), plus what the Assembler needs to run the work itself:

- weekly_reports: `phase`, `phase_at`, `backfill_job_id`, `history_job_id`, `history_ok`,
  `summary` (a review waits for the collector across scheduler ticks instead of blocking)
- weekly_report_files: the JSON / HTML / CSV report bodies (PostgreSQL, not a local disk)
- mt5_deals: `server` (broker identity) in the key: (server, account_id, deal_ticket)
- indexes for the job queue, open trades, live signals, alert delivery and risk events

Every statement is IF NOT EXISTS, so a database that already holds V1.6.3's `aureon` schema
keeps its rows. Nothing outside `aureon` is touched.

Grants: every role that can use schema `trading` gets the same kind of access to `aureon`
(DML if it can write trading.trading_signals, else SELECT).

Downgrade refuses while any `aureon` table holds rows; on an empty schema it drops it.

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# RULE: additive and reversible only. Never drop/recreate existing tables.

DDL = """
create schema if not exists aureon;

-- 001_init
create table if not exists aureon.bars (
  symbol text not null, tf text not null, time_utc timestamptz not null,
  open double precision, high double precision, low double precision, close double precision,
  tick_volume bigint, spread int, source text default 'live', verified_week boolean default false,
  primary key(symbol, tf, time_utc));
create table if not exists aureon.indicators (
  symbol text, tf text, time_utc timestamptz, rules_version text,
  ema8 double precision, ema32 double precision, ema200 double precision, atr14 double precision,
  vwap double precision, bar_of_day int, vwap_side smallint, vwap_flips8 smallint,
  gap_atr double precision,
  primary key(symbol, tf, time_utc, rules_version));
create table if not exists aureon.market_calendar (
  id serial primary key, kind text not null, start_utc timestamptz not null,
  end_utc timestamptz, label text);
create table if not exists aureon.rules_versions (
  id serial primary key, label text unique not null, params jsonb not null,
  status text not null default 'candidate', created_at timestamptz default now(), note text);
create table if not exists aureon.chop_zones (
  id serial primary key, symbol text, tf text, kind text, start_utc timestamptz,
  end_utc timestamptz, hi double precision, lo double precision, result text,
  rules_version text);
create table if not exists aureon.signals (
  id serial primary key, symbol text, tf text, time_utc timestamptz not null,
  rules_version text not null, source text not null default 'live', kind text not null,
  dir smallint not null, price double precision, atr double precision,
  suggested_sl double precision, suggested_tp double precision, features jsonb,
  sent_at timestamptz,
  unique(symbol, tf, time_utc, kind, rules_version, source));
create table if not exists aureon.decisions (
  id serial primary key, signal_id int references aureon.signals(id),
  tagged_at timestamptz default now(), tag text not null, via text default 'discord', note text);
create table if not exists aureon.trades (
  id serial primary key, signal_id int references aureon.signals(id), mt5_ticket bigint unique,
  dir smallint, lots double precision, entry_time timestamptz, entry_price double precision,
  sl double precision, tp double precision, status text default 'open',
  exit_time timestamptz, exit_price double precision, pnl_usd double precision,
  exit_reason text);
create table if not exists aureon.trade_events (
  id serial primary key, trade_id int references aureon.trades(id),
  time_utc timestamptz default now(), event text not null, old_sl double precision,
  new_sl double precision, note text);
create table if not exists aureon.hypo_outcomes (
  signal_id int references aureon.signals(id), rules_version text, mfe double precision,
  mae double precision, bars_to_20 int, dip_before_20 double precision,
  secured double precision, pl_rule double precision, exit_how text,
  computed_at timestamptz default now(),
  primary key(signal_id, rules_version));
create table if not exists aureon.weekly_review (
  week_start date primary key, alerts int, taken int, skipped int, both_right int,
  both_wrong int, bot_right_you_wrong int, you_right_bot_wrong int,
  pl_bot_usd double precision, pl_you_usd double precision, summary text,
  created_at timestamptz default now());
create table if not exists aureon.service_heartbeats (
  service text primary key, state text, last_beat timestamptz, note text);
create table if not exists aureon.deployments (
  id serial primary key, time_utc timestamptz default now(), commit_sha text, actor text,
  model text, prompt_hash text, rolled_back boolean default false, note text);
create index if not exists ix_signals_time on aureon.signals(time_utc);
create index if not exists ix_bars_tf_time on aureon.bars(tf, time_utc);
create table if not exists aureon.jobs (
  id serial primary key, kind text not null, payload jsonb default '{}',
  requested_at timestamptz default now(), claimed_at timestamptz, claimed_by text,
  lease_until timestamptz, attempts int default 0, done_at timestamptz, ok boolean,
  result text);
create table if not exists aureon.control (
  key text primary key, value text not null, updated_at timestamptz default now(),
  updated_by text);
insert into aureon.control(key, value, updated_by) values('desired', 'RUN', 'init')
  on conflict do nothing;

-- 002_v16
create table if not exists aureon.risk_state (
  broker_day date not null, account_id text not null, ref_equity double precision,
  ref_balance double precision, ref_source text, realized double precision default 0,
  floating double precision default 0, equity double precision, balance double precision,
  drawdown double precision default 0, deposits double precision default 0,
  state text not null default 'UNKNOWN', locked_at timestamptz, lock_reason text,
  cleared_at timestamptz, cleared_by text, clear_reason text,
  updated_at timestamptz default now(),
  primary key(broker_day, account_id));
create table if not exists aureon.risk_events (
  id serial primary key, time_utc timestamptz default now(), broker_day date,
  account_id text, event text not null, detail jsonb, actor text);
alter table aureon.jobs add column if not exists lease_token text,
  add column if not exists generation int default 0,
  add column if not exists state text default 'pending';
create table if not exists aureon.alerts (
  id serial primary key, alert_key text unique not null, kind text not null,
  payload text not null, state text not null default 'PENDING', attempts int default 0,
  next_attempt_at timestamptz default now(), delivered_at timestamptz, last_error text,
  created_at timestamptz default now());
create table if not exists aureon.api_audit (
  id serial primary key, time_utc timestamptz default now(), role text, method text,
  path text, status int, client text, detail text);
create table if not exists aureon.schema_migrations (
  version text primary key, applied_at timestamptz default now());
alter table aureon.trades add column if not exists close_sent_at timestamptz,
  add column if not exists close_request_id text;
create index if not exists ix_alerts_state on aureon.alerts(state, next_attempt_at);

-- 003_v161_boundary
alter table aureon.risk_state add column if not exists equity_at timestamptz,
  add column if not exists boundary_equity double precision,
  add column if not exists boundary_at timestamptz,
  add column if not exists history_suspect boolean default false;

-- 004_weekend_review
create table if not exists aureon.weekly_reports (
  id serial primary key, account_id text not null, symbol text not null,
  week_start date not null, strategy_version text not null,
  state text not null default 'PENDING', started_at timestamptz, finished_at timestamptz,
  attempts int default 0, coverage jsonb, actual_metrics jsonb, replay_metrics jsonb,
  files jsonb, error text,
  unique(account_id, symbol, week_start, strategy_version));
create table if not exists aureon.mt5_deals (
  server text not null default '', account_id text not null, deal_ticket bigint not null,
  order_ticket bigint, position_id bigint, symbol text, type int, entry int,
  time_utc timestamptz, volume double precision, price double precision,
  profit double precision, commission double precision, swap double precision,
  fee double precision, comment text, magic bigint, ingested_at timestamptz default now(),
  primary key(server, account_id, deal_ticket));
alter table aureon.mt5_deals add column if not exists server text not null default '';
create table if not exists aureon.replay_detections (
  report_id int references aureon.weekly_reports(id) on delete cascade, time_utc timestamptz,
  tf text, dir smallint, price double precision, category text, reason text,
  features jsonb, live_signal_id int, hypo jsonb,
  primary key(report_id, time_utc, tf));
create index if not exists ix_mt5_deals_time on aureon.mt5_deals(account_id, symbol, time_utc);

-- Assembler: resumable reviews, stored report files, queue and lookup indexes
alter table aureon.weekly_reports add column if not exists phase text,
  add column if not exists phase_at timestamptz,
  add column if not exists backfill_job_id int,
  add column if not exists history_job_id int,
  add column if not exists history_ok boolean,
  add column if not exists summary jsonb;
create table if not exists aureon.weekly_report_files (
  report_id int not null references aureon.weekly_reports(id) on delete cascade,
  name text not null, content_type text not null, body text not null,
  created_at timestamptz default now(),
  primary key(report_id, name));
create index if not exists ix_jobs_queue on aureon.jobs(state, requested_at)
  where done_at is null;
create index if not exists ix_jobs_kind on aureon.jobs(kind, requested_at);
create index if not exists ix_trades_status on aureon.trades(status);
create index if not exists ix_signals_live on aureon.signals(source, time_utc);
create index if not exists ix_risk_events_day on aureon.risk_events(account_id, broker_day);
create index if not exists ix_api_audit_time on aureon.api_audit(time_utc);
"""

GRANTS = """
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT rolname FROM pg_roles
    WHERE rolname <> current_user AND NOT rolsuper AND rolname NOT LIKE 'pg\\_%'
      AND has_schema_privilege(oid, 'trading', 'USAGE')
  LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA aureon TO %I', r.rolname);
    IF has_table_privilege(r.rolname, 'trading.trading_signals', 'INSERT') THEN
      EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA aureon TO %I',
                     r.rolname);
      EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA aureon TO %I', r.rolname);
    ELSIF has_table_privilege(r.rolname, 'trading.trading_signals', 'SELECT') THEN
      EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA aureon TO %I', r.rolname);
      EXECUTE format('GRANT SELECT ON ALL SEQUENCES IN SCHEMA aureon TO %I', r.rolname);
    END IF;
  END LOOP;
END
$$;
"""

TABLES = (
    "weekly_report_files", "replay_detections", "mt5_deals", "weekly_reports",
    "schema_migrations", "api_audit", "alerts", "risk_events", "risk_state", "control", "jobs",
    "deployments", "service_heartbeats", "weekly_review", "hypo_outcomes", "trade_events",
    "trades", "decisions", "signals", "chop_zones", "rules_versions", "market_calendar",
    "indicators", "bars",
)  # fmt: skip


def statements(sql: str) -> list[str]:
    """One statement per execute (asyncpg prepares each); `--` comment lines dropped."""
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";\n") if s.strip().rstrip(";")]


def upgrade() -> None:
    for stmt in statements(DDL):
        op.execute(stmt)
    op.execute(GRANTS)


def downgrade() -> None:
    bind = op.get_bind()
    present = [
        t
        for t in TABLES
        if bind.execute(sa.text("select to_regclass(:n)"), {"n": f"aureon.{t}"}).scalar()
    ]
    # `control` is seeded by the upgrade itself; only operator rows count as data.
    busy = []
    for t in present:
        extra = " where updated_by <> 'init'" if t == "control" else ""
        if bind.execute(sa.text(f"select exists(select 1 from aureon.{t}{extra})")).scalar():
            busy.append(t)
    if busy:
        raise RuntimeError(
            "refusing to drop schema aureon: these tables hold data: " + ", ".join(busy)
        )
    for t in present:
        op.execute(f"drop table if exists aureon.{t} cascade")
    op.execute("drop schema if exists aureon")
