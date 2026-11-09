# Aureon V1.6.3 on the Assembler

The Assembler now serves the database API that the Aureon V1.6.3 Windows box uses in
`STORE_MODE=api` (`aureon/api_client.py` + `aureon/store.py` `ApiStore`), and runs V1.6.3's
Linux-side work itself: detector, scheduler, alert queue, nightly verify, Sunday review and the
weekend auto-replay. Source of truth: the V1.6.3 package (`aureon_v1`), read in full —
`assembler/app.py`, `aureon/*.py`, `db/migrations/001-004`, the changelogs and the tests.

No second server: the routes live in the existing API (`app/api/v1/aureon_v163.py`), the data in
the existing PostgreSQL (schema `aureon`, Alembic 0027), the work in the existing scheduler.

## Execution boundary

| Assembler (Ubuntu) | Windows box (V1.6.3, unchanged) |
|---|---|
| stores bars, deals, trades, risk rows, heartbeats, alerts | MT5 terminal access, bars and deal history |
| detector (R3.0 on closed M30 bars), signals | the guard: attaches positions, stops, lock ladder, exits, flattening |
| queues collector jobs (`backfill`, `backfill_range`, `mt5_history`, `verify_week`) | runs the jobs it claims and uploads the results |
| scheduler state, watchdog, Discord delivery | daily risk engine (writes its state through the API) |
| weekend replay + Sunday review, reports | order execution — never on the Assembler |

Nothing under `app/aureon/` imports MetaTrader5 or V1.6.3's `orders`, calls `order_send`, or
reads broker positions (AST-checked by `test_no_aureon_module_can_place_an_order_or_reach_mt5`).
The guard's protection does not depend on the Assembler: when the API is unreachable its loop
skips (V1.6.3 behaviour: alert, retry, the broker-held stop stays), and the same client resumes
when the API returns, with the trade and risk rows intact (`test_guard_cannot_unlock_and_api_outage_then_reconnect`).
No agent got live-trading permission; no strategy parameter changed.

## Compatibility matrix

| V1.6.3 operation | Before | Now |
|---|---|---|
| `POST /api/trading/mt5/ingest` (existing MT5 push bot) | implemented | unchanged; not a substitute, not authorized by the new tokens |
| collector: bars, heartbeat, state, last bar, jobs claim/renew/finish, MT5 deals | missing | implemented, V1.6.3 contract |
| guard: recent signal, open trades, trade by ticket, daily P&L, trade create/patch, trade events, risk get/set, risk events | missing | implemented, V1.6.3 contract |
| alerts, control (`reconcile_request`, `lockout_until`, `day_start:*`) | missing | implemented |
| admin risk clear | missing | implemented, audited (`aureon.risk_events` + `audit.audit_events`) |
| reads: status, signals, signal, decisions, trades, chart, review, review/latest, risk | missing | implemented |
| `/healthz`, `/readyz` | missing | `GET /api/v1/aureon/healthz` (no auth), `/api/v1/aureon/readyz` (read role) |
| storage 001-004 (bars, indicators, signals, decisions, trades, events, hypo outcomes, weekly review, heartbeats, jobs, control, risk, alerts, api_audit, weekly reports, MT5 deals, replay detections) | missing | Alembic 0027, same tables in schema `aureon` |
| detector (live + replay + nightly verify) | missing | `app/aureon/detector.py`, scheduler task |
| scheduler (LIVE/PAUSED/RESUMING/DEGRADED/FAILED, backfill ack, watchdog) | missing | `app/aureon/scheduler.py`, state in PostgreSQL |
| alerter (durable queue, delivery, signal queue, auto-skip) | Discord dispatch for incidents only | `app/aureon/alerter.py`, existing `DISCORD_WEBHOOK_URL` |
| Sunday review (`weekly_review`) | missing | `app/aureon/sunday_review.py` |
| weekend auto-replay (V1.6.3) | missing | `app/aureon/weekend_review.py`, resumable |
| matching + classes, deal-history positions | missing | copied verbatim (`matcher.py`, `positions.py`) |
| manual review run | — | `POST /api/v1/admin/review/run` (admin, audited) |
| report files | local disk (V1.6.3) | PostgreSQL `aureon.weekly_report_files` + `GET /api/v1/review/reports/...`; disk optional |
| V1.6.3 Discord bot commands (take/skip/pause/resume/risk) | — | not ported: decisions via `POST /api/v1/decisions`; pause via `aureon.control.desired`; risk clear via the admin route |
| V1.6.3 self-heal (AI patches) | — | not ported (off by default in V1.6.3; the Assembler has its own repair flow) |
| Gemini summary in the Sunday review | — | not run (summary left empty) |

Copied byte for byte (and excluded from reformatting so `diff` against V1.6.3 stays empty):
`app/aureon/indicators.py`, `app/aureon/matcher.py`, the `build_positions` body of
`app/aureon/positions.py`, and the R3.0 rule dict in `app/aureon/config.py`.

## Endpoints

Paths are what the V1.6.3 client requests relative to its `ASSEMBLER_API_URL`. Every route
needs `Authorization: Bearer <role token>` (except `aureon/healthz`); no session, cookie or CSRF.

| Method | Path | Roles | Body / query | Answer |
|---|---|---|---|---|
| POST | `/api/v1/ingest/bars` | collector | `{tf: M30\|H1, source: live\|csv\|verify\|backfill, rows: [[time_utc,o,h,l,c,tick_volume,spread]] ≤500}` | 201 `{n}`; 422 future/unaligned/OHLC/bounds |
| POST | `/api/v1/ingest/heartbeat` | collector, guard, service | `{service: collector\|guard, state: LIVE\|PAUSED\|RESUMING\|DEGRADED\|FAILED, note ≤200}` | 201 `{ok}` |
| GET | `/api/v1/ingest/state` | collector, guard, service | — | `{state}` |
| GET | `/api/v1/ingest/last_bar` | collector, guard, service | — | last closed M30 bar + `vwap_side, bar_of_day, atr14`, or `null` |
| GET | `/api/v1/ingest/health_summary` | collector, guard, service | `detail` (optional) | V1.6.4 watchdog summary, see below |
| POST | `/api/v1/ingest/jobs/claim` | collector | `{worker}` | the job row (`id, kind, payload, lease_token, attempts…`) or `null` |
| POST | `/api/v1/ingest/jobs/{jid}/renew` | collector | `{worker, token}` | `{ok}` (false unless the caller holds the lease) |
| POST | `/api/v1/ingest/jobs/{jid}/finish` | collector | `{ok, result ≤2000, worker, token}` | `{ok, ignored}` (fenced: wrong worker/token/expired → ignored) |
| POST | `/api/v1/ingest/mt5_deals` | collector | `{deals: [{account_id, deal_ticket, time_utc, volume, price, …}] ≤200}` | 201 `{n, inserted}` (idempotent on server+account+ticket) |
| GET | `/api/v1/ingest/recent_signal` | guard | `dir` (-1..1) | `{id, suggested_sl, dir}` or `null` |
| GET | `/api/v1/ingest/trades_open` | guard | — | open / closing / unreconciled trades |
| GET | `/api/v1/ingest/trade_by_ticket` | guard | `ticket ≥1` | `{id, status}` or `null` |
| GET | `/api/v1/ingest/daily_pnl` | guard | — | `{pnl}` (closed today, UTC) |
| POST | `/api/v1/ingest/trade` | guard | `{signal_id?, mt5_ticket, dir ±1, lots, entry_time, entry_price, sl?, tp?}` | 201 `{id}` / `{id, existing: true}`; 422 sl wrong side |
| PATCH | `/api/v1/ingest/trade/{tid}` | guard | `{expected_status?, status?, exit_*, close_sent_at?, close_request_id?, sl?, tp?}` | `{ok}`; 409 precondition/closed/lost race; 422 state rules |
| POST | `/api/v1/ingest/trade_event` | guard | `{trade_id, event: attach\|sl_set\|lock\|flat\|exit\|limit_block\|close_sent, old_sl?, new_sl?, note}` | 201; 404 unknown trade |
| GET | `/api/v1/ingest/risk` | guard | `day, account_id` | the risk row or `null` |
| POST | `/api/v1/ingest/risk` | guard | the risk row (`state: UNLOCKED\|WARNING\|LOCKED\|UNKNOWN`, …) | 201; 409 LOCKED → anything else |
| POST | `/api/v1/ingest/risk_event` | guard | `{broker_day, account_id, event, detail, actor}` | 201 |
| POST | `/api/v1/ingest/alert` | collector, guard, service | `{alert_key ≤120, kind ≤30, payload ≤1900}` | 201 (idempotent by key) |
| GET/POST | `/api/v1/ingest/control` | collector, guard, service | `key` ∈ reconcile_request, lockout_until, day_start:YYYY-MM-DD | `{key, value}` / 201 |
| POST | `/api/v1/admin/risk/clear` | admin | `{account_id, reason 5..200}` | `{ok}`; 409 no active lock |
| POST | `/api/v1/admin/review/run` | admin | `{week?, force?, account_id?}` | 202 `{report_id, state, phase, …}` |
| GET | `/api/v1/status` | reader, writer, admin | — | state, rules version, heartbeats, open trades, risk, last bar |
| GET | `/api/v1/signals` | reader, writer, admin | `week, tradeable, limit ≤500, offset` | live signals + decision, hypo outcome, trade |
| GET | `/api/v1/signals/{sid}` | reader, writer, admin | — | signal + decisions, outcome, trade + events |
| POST | `/api/v1/decisions` | writer | `{signal_id, tag: take\|skip\|late\|news\|doubt, note}` | 201; same tag → `{unchanged}`; 409 > 2 h old |
| GET | `/api/v1/trades` | reader, writer, admin | `week, limit, offset` | trades of the week |
| GET | `/api/v1/chart` | reader, writer, admin | `tf, from, to` (≤60 days, ≤3000 bars) | bars + indicators + live signals |
| GET | `/api/v1/review`, `/review/latest` | reader, writer, admin | `limit ≤104` | Sunday reviews (`weekly_review`) |
| GET | `/api/v1/review/reports[/{rid}[/files/{name}]]` | reader, writer, admin | — | weekend reports, their metadata and files |
| GET | `/api/v1/risk` | reader, writer, admin | — | last 14 risk rows (no clear reasons) |
| GET | `/api/v1/aureon/readyz` | reader, writer, admin | — | 200 or 503 with problems (V1.6.3 `/readyz`) |
| GET | `/api/v1/aureon/healthz` | none | — | `{ok}` / 503 |

### V1.6.4 watchdog: `GET /api/v1/ingest/health_summary`

The V1.6.4 Windows watchdog (`aureon/watchdog.py`) polls this every `WATCHDOG_TICK_S` through
`ApiStore.health_summary()` and judges each service from it. Contract (V1.6.4's own
`assembler/app.py`, field for field):

```json
{
  "state": "LIVE",                       // scheduler heartbeat state, or null (no row)
  "heartbeats": [{"service": "collector", "state": "LIVE", "last_beat": "2026-11-10T09:59:55+00:00", "note": ""}],
  "bars": [{"tf": "H1", "t": "2026-11-10T09:00:00+00:00", "n24": 22}, {"tf": "M30", "t": "…", "n24": 44}],
  "indicators_m30_latest": "2026-11-10T09:30:00+00:00",   // or null: the detector has not run
  "signals": {"t": "…", "n24": 1},       // t null when no live signal yet
  "risk": {"state": "UNLOCKED", "ref_source": "day_open"},   // or null
  "control_desired": "RUN"               // RUN | PAUSE, or null (no row)
}
```

How the watchdog reads it: collector RUNNING only when its heartbeat is < 180 s old **and** the
newest M30 bar is < 90 min and H1 < 150 min old (both computed on the box from `last_beat` /
`t`); DEGRADED otherwise after its 120 s start grace; PAUSED when `control_desired == "PAUSE"`;
FAILED (never restarted) on 401/403; any other error is shown as `backend: …`.

Truthfulness: V1.6.4's server filled in `"PAUSED"` / `"RUN"` when the scheduler or control row was
missing; the Assembler answers `null` (no record, no claim). A heartbeat row that never beat
is left out. A database failure is `503 database unavailable`, never a guessed summary.

`?detail=true` (the watchdog never sends it) adds `diagnostics`: heartbeat ages and staleness,
the last accepted upload per ingest endpoint (`aureon.api_audit`), open/failed collector jobs
and failed alerts — for checking the pipeline by hand. Every query reads an index; a poll
every 5 s is cheap.

Reading an empty `last_bar`: it joins the newest M30 bar to its indicator row, so it is `null`
(HTTP 200) both when no bar is stored and when bars are stored but the detector has not run
(`AUREON_WORKERS_ENABLED=false`). A credential problem is a 401/403, never an empty 200.
`health_summary` tells the cases apart: `bars` empty = nothing ingested; `bars` present and
`indicators_m30_latest` null = the detector is not running.

### Differences from V1.6.3's server (deliberate)

Four fixes for requests the V1.6.3 client sends that its own server answered 422 (each covered
by a test that runs the real client code):

1. trade event `close_sent` (the guard's `_exit` and naked fail-safe post it);
2. `PATCH status=open, exit_reason=null` (the guard re-adopting an unreconciled position);
3. heartbeat state `DEGRADED` / `FAILED` (the collector echoes the shared scheduler state);
4. bar source `backfill` (the collector's `backfill_range` job uploads with it).

And these, for least privilege and the Assembler's runtime:

- the admin token reads and runs admin routes only (V1.6.3 let it into every route);
- a `service` role (heartbeat, alerts, control, state, last bar) for a process that needs no more;
- failed authentications are rate-limited per client (30/min) besides V1.6.3's 600/min per role;
- `413` above `AUREON_API_MAX_BODY_BYTES` (1 MiB; nginx caps `/api/` at 2 MiB);
- `mt5_deals` is keyed by (server, account, ticket) — `server` is optional in the upload (V1.6.3
  sends none, so its key is effectively account + ticket, as before);
- a `trade` with an unknown `signal_id` is 422 (V1.6.3 failed with a database error);
- `daily_pnl` counts exits on today's UTC date explicitly (V1.6.3 used the DB session's zone);
- job renewal and finish are fenced exactly as V1.6.3; claims also order by id on equal times.

## Storage (Alembic 0027, schema `aureon`)

Additive only, every statement `IF NOT EXISTS`; nothing outside `aureon` is touched. Same tables
and keys as V1.6.3 001-004, plus: `weekly_reports.phase/phase_at/backfill_job_id/history_job_id/
history_ok/summary`, `weekly_report_files`, `mt5_deals.server`, and indexes for the job queue,
open trades, live signals, alerts and risk events. Grants mirror schema `trading`'s roles. The
downgrade refuses while any `aureon` table holds rows (never drops production data).

Idempotency: bars upsert on (symbol, tf, time); signals unique per (symbol, tf, time, kind,
rules, source); trades unique per MT5 ticket; deals per (server, account, ticket); alerts per
key; weekly reports per (account, symbol, week, strategy); decisions are latest-wins. Job claims
are `FOR UPDATE SKIP LOCKED` with a fresh lease token; renew/finish require worker + token (and
finish an unexpired lease); a failure returns the job to `pending` until 3 attempts, then
`dead`. All timestamps are `timestamptz`, written as UTC.

## Background work (scheduler container, `AUREON_WORKERS_ENABLED=true`)

| Task | Cron (default) | Does |
|---|---|---|
| `aureon-tick` | every minute | scheduler state machine → resume backfill (`backfill` job, durable ack) → LIVE; DEGRADED when the collector stops; gap → `backfill_range` job and replay; watchdog; detector on new closed bars; signal queue (fail closed on risk); auto-skip |
| `aureon-alerts` | every minute | `aureon.alerts` → Discord (bounded retries, Retry-After, jitter); kept PENDING without a webhook |
| `aureon-weekend-review` | every 5 min | advances RUNNING reviews; on a closed weekend starts last week's once |
| `aureon-weekly-review` | Sun 12:00-13:50, every 10 min | Sunday review: `verify_week` job (≤300 s), hypothetical outcomes, decisions vs bot |
| `aureon-verify` | 02:30 Tue-Sat | replay last 7 days, alert on live signals not reproduced |

Weekend review phases: `coverage` (M30 + H1 expected bars, holidays, warm-up) → missing bars →
`backfill_range` job, `wait_backfill` (≤ `AUREON_REVIEW_JOB_WAIT_SECONDS`, 600) → `mt5_history`
job, `wait_history` (≤600) → `finalize`: chronological replay with the live rule (no lookahead),
live-signal join, risk-blocked days, one-to-one matching, actual metrics (broker) and replay
metrics (hypothetical $/oz) kept apart, `detections.csv`, `actual_trades.csv`, `comparison.csv`,
`summary.json`, `summary.html`, Discord summary → `COMPLETED`, or `INCOMPLETE` when data is
missing, or `FAILED` (alert). Same-bar stop/TP stays `AMBIGUOUS` (never resolved as TP).

## Environment variables (backend; names only)

`AUREON_API_COLLECTOR_TOKEN`, `AUREON_API_GUARD_TOKEN`, `AUREON_API_SERVICE_TOKEN`,
`AUREON_API_READ_TOKEN`, `AUREON_API_WRITE_TOKEN`, `AUREON_API_ADMIN_TOKEN`,
`AUREON_API_SVC_TOKEN` (legacy), `AUREON_API_RATE_PER_MINUTE`, `AUREON_API_MAX_BODY_BYTES`,
`AUREON_WORKERS_ENABLED`, `AUREON_TICK_CRON`, `AUREON_ALERTS_CRON`, `AUREON_WEEKEND_REVIEW_CRON`,
`AUREON_WEEKLY_REVIEW_CRON`, `AUREON_VERIFY_CRON`, `AUREON_REVIEW_REPORTS_DIR`, and the strategy
values `AUREON_SYMBOL`, `AUREON_SESSION_FIRST_BAR_UTC`, `AUREON_SESSION_LAST_BAR_UTC`,
`AUREON_REVIEW_MAX_ENTRY_DELAY_MIN`, `AUREON_REVIEW_MAX_PRICE_DEV`,
`AUREON_REVIEW_JOB_WAIT_SECONDS`, `AUREON_MAGIC`. Discord uses the existing
`DISCORD_WEBHOOK_URL`. Generate each token with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`; ENV=prod refuses short,
placeholder or reused values. The existing `MT5_INGEST_TOKEN` does **not** authorize these routes.

Windows `.env`: `deploy/aureon-v163/env.windows.v163.example` (collector + guard tokens only, no
database credentials, no admin token).

## Network path

The Windows box reaches the Assembler the way the MT5 push already does: HTTPS over Tailscale
(`https://svskhd8.taila439d5.ts.net` → tailscale serve → nginx `location /api/` → api). The app's
Cloudflare Access check exempts exactly these routes (`MACHINE_PATTERNS` in
`app/core/cf_access.py`, full-path anchored regexes) because they authenticate themselves with
role tokens; nothing else changed. `https://spasta.online` stays behind Cloudflare Access: the
V1.6.3 client cannot send Access service-token headers, so using the public hostname would need
an Access bypass policy for these paths — a decision for you, not made here.

## Verification commands (after deploy; tokens from your secret store, never echoed)

```bash
# liveness (no token)
curl -fsS https://svskhd8.taila439d5.ts.net/api/v1/aureon/healthz
# wrong / missing token -> 401, wrong role -> 403
curl -s -o /dev/null -w '%{http_code}\n' https://svskhd8.taila439d5.ts.net/api/v1/status
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $AUREON_API_COLLECTOR_TOKEN" https://svskhd8.taila439d5.ts.net/api/v1/status
# reader
curl -fsS -H "Authorization: Bearer $AUREON_API_READ_TOKEN" https://svskhd8.taila439d5.ts.net/api/v1/status
# collector: queue is reachable (null when empty)
curl -fsS -X POST -H "Authorization: Bearer $AUREON_API_COLLECTOR_TOKEN" -H 'Content-Type: application/json' \
  -d '{"worker":"curl-check"}' https://svskhd8.taila439d5.ts.net/api/v1/ingest/jobs/claim
# plain HTTP must not be served to remote clients
curl -s -o /dev/null -w '%{http_code}\n' http://svskhd8.taila439d5.ts.net/api/v1/aureon/healthz
# existing routes unchanged
curl -fsS https://svskhd8.taila439d5.ts.net/api/health
```

Run the `jobs/claim` check only while the collector is stopped, or it may take a real job (it is
re-claimable after its 10-minute lease).

## Deployment (staged, needs your approval) and rollback

1. Back up PostgreSQL (`scripts/ops/backup.sh`); 0027 only adds schema `aureon`, but back up first.
2. Set the tokens in the server `.env` (not the repo); leave `AUREON_WORKERS_ENABLED=false`.
3. Deploy the image; the deploy runs `alembic upgrade head` (0027 only adds schema `aureon`).
4. Run the verification commands; `GET /api/health/ready` must answer as before.
5. On the Windows box set the `.env` from the template; start the collector, then the guard;
   watch `aureon.api_audit` and `GET /api/v1/aureon/readyz`.
6. Set `AUREON_WORKERS_ENABLED=true`, restart the scheduler container; watch the first ticks
   (`agents.scheduled_jobs` runs, `aureon.service_heartbeats`).

Rollback: stop the Windows services (the guard keeps broker-held stops), set
`AUREON_WORKERS_ENABLED=false`, redeploy the previous image. The `aureon` schema can stay (no
other code reads it); `alembic downgrade 0026` only drops it when it is empty, by design.

## Tests

- `tests/test_aureon_unit.py` (no DB): V1.6.3's server-side unit tests ported (gaps, indicators
  warm-up/determinism, AMBIGUOUS, positions hedging/netting/partial/open, matching classes, week
  selection, no-lookahead replay, reports), roles, prod checks, machine paths, Discord send
  classification, the AST execution-boundary check.
- `tests/test_aureon_db.py` (PostgreSQL + Redis): every endpoint's contract, the role matrix,
  idempotency, the trade state machine and race, risk lock/clear/audit, the job queue (fencing,
  expiry, retries, dead, concurrent claims), the scheduler (collector offline → reconnect,
  backfill ack, pause, gap backfill), the detector, the weekend review end to end (complete,
  missing candles → recovery job → INCOMPLETE, history failure, dedup, force, restart resume,
  auto trigger, admin run, failure → FAILED + alert), alert delivery, audit, rate limits.
- `tests/test_aureon_client_compat.py` (PostgreSQL + Redis + uvicorn over HTTP): the unmodified
  V1.6.3 collector and guard (`tests/fixtures/aureon_v163/`) against the Assembler, with only
  `MetaTrader5` faked: bar upload, heartbeat echo, `backfill_range` and `mt5_history` jobs end to
  end, job failure and retry, the guard attaching a position and writing risk + heartbeat, the
  guard's exit (`close_sent` → `closed` with broker P&L), the lock it cannot clear, an API
  outage and reconnect, and every `ApiStore` path served.

## Still to verify on the real infrastructure

These need the real Windows box, MT5 terminal and network; none was available here.

1. `MT5_TIME_POLICY` for your broker (`scripts/diagnose_timestamps.py` on the box) and that the
   first uploaded bars line up with the broker's weekly open.
2. The collector over Tailscale HTTPS: bars arriving, `backfill_range` and `mt5_history` jobs
   claimed and finished, deals in `aureon.mt5_deals`.
3. The guard on the demo account: attach, stop verification, lock ladder, exits, risk baseline at
   the broker-day boundary, and its behaviour when the API is stopped for a few minutes.
4. The first weekend replay on real data (`POST /api/v1/admin/review/run` or wait for Saturday),
   its coverage numbers and matched trades against your MT5 history.
5. Discord delivery of `aureon.alerts` with the production webhook.
6. Cloudflare/Tailscale: the machine routes reachable over Tailscale and not over the public
   hostname unless you add an Access bypass.
