# Changelog

## Unreleased: AI_LOCAL_ENABLED switch (external models only)

- New setting `AI_LOCAL_ENABLED` (default `true`, editable in /settings → AI). When `false`
  the backend never calls Ollama: chat and the agents use only the external chain
  (`AI_DEFAULT_EXTERNAL_PROVIDER` + `AI_EXTERNAL_FALLBACK_PROVIDERS`, e.g. OpenRouter); an
  external failure is an error instead of a local fallback; the startup warm-up and the
  `refresh-model-inventory` job are skipped; `/api/health/ready` drops the Ollama check, so
  the Ollama container can be stopped without failing deploys.
- The classification filter is unchanged. A prompt it keeps local (secrets, personal finance,
  broker, positions, logs, trading/finance conversations) and an agent set to provider
  `ollama` are refused with "local models are disabled", never sent to an external model.
- Local-only features report themselves unavailable: research explanations without an
  external route, the coding agent (`CODE_MODEL`), self-repair (`REPAIR_MODEL`) and the voice
  model fallback (the grammar still works).

## Unreleased: Gemini 3.5 Flash-Lite as the primary external model

- Defaults ([docs/26-ai-providers.md](docs/26-ai-providers.md)): `AI_DEFAULT_EXTERNAL_PROVIDER`
  `gemini`, `GEMINI_DEFAULT_MODEL` / `GEMINI_ALLOWED_MODELS` `gemini-3.5-flash-lite` (a blank
  model means it too), `AGENT_NEWS/VOICE/RESEARCH_PROVIDER` `gemini`. A verified built-in price
  (0.30 / 2.50 USD per million tokens, output incl. thinking) applies when the operator has not
  priced the model; an operator price wins. Enable flags, keys, daily/global caps and
  `AI_RESEARCH_EXTERNAL_ALLOWED` are unchanged. Saved settings and `.env` still take precedence.
- Voice is unchanged (grammar, then optional local Ollama): its module may not import
  services/DB code (approval-safety invariant), so it is not routed to Gemini.
- Trading research explanations follow `AGENT_RESEARCH_PROVIDER`/`_MODEL` (Gemini first where
  `AI_RESEARCH_EXTERNAL_ALLOWED`), record every attempt in `ai.model_calls` (agent
  `trading-research`), and no longer require a local `RESEARCH_AI_MODEL` when external research
  is permitted; without any model they fail visibly.
- `ModelRouter.complete(..., allow_local=False)`: no Qwen call at all when the caller had no
  local model. A successful external answer never triggers a local inference (tested).
- `POST /api/v1/ai/route-check` (admin, CSRF): one fixed public prompt through an agent's real
  route, returning the `ai.model_calls` rows it produced, to verify runtime routing.
- The news job makes no model call (RSS ingest only), so there is nothing to route for News.

## Unreleased: market terminal backend (market-data part 2)

- **Stock screener** ([docs/35-market-data.md](docs/35-market-data.md)): `POST
  /api/trading/screener/run` with the ASTRA-FRONTEND §6 contract (EMA cross incl. 8/32, EMA
  position, price vs EMA, RSI, MACD cross, volume vs average, ADX, VWAP position) over
  NSE/BSE equities, indices, futures or a server watchlist. Reads stored Dhan candles only (no
  Dhan call per scan); confirmed vs live candles; no lookahead; missing, short and stale
  history is counted in `coverage`, never reported as a match. Small universes answer inline
  (200); larger ones are queued for the research worker's new `screener` lane (202 + `run_id`,
  poll `GET /screener/runs/{id}`), persisted, cached per bar period, cancellable. `GET
  /screener/status`. 30m/4h are refused (Dhan has no such interval).
- **Watchlists**: owner-only CRUD under `/api/trading/watchlists` (CSRF on writes).
- **Indicators**: RSI, ADX (+DI/−DI), SMA, Bollinger, matching the frontend's formulas; `GET
  /api/trading/indicators/compute` (any EMA period, same window as the screener).
- **Instruments**: exchange/segment/multi-type/underlying/expiry/strike/option filters and
  name search server-side; `GET /api/trading/instruments/status`. Fix: an instrument row
  without a segment that disappears from the master is `missing_from_master`, not
  `segment_disabled`.
- **Candles**: `is_closed` on `GET /api/trading/candles`.
- **History queue**: `POST/GET /api/trading/candles/backfill` (validated against Dhan's
  intervals and segments), worked by the research worker's new `candle-backfill` lane one
  request at a time under the shared Dhan rate limit, fetching only missing bars. The
  `dhan-universe-backfill` job now reads `DHAN_UNIVERSE_BACKFILL*` (new
  `DHAN_UNIVERSE_BACKFILL_TIMEFRAMES`, default `1d`).
- Migration 0029: index `(timeframe, ts DESC) WHERE NOT is_gap` on `trading.market_candles`,
  built CONCURRENTLY. New setting `SCREENER_RUN_TIMEOUT_SECONDS` (300).

## Unreleased: Dhan instrument master fixes (market-data part 1)

- **Instruments now load without waiting for 08:11 IST.** At startup the scheduler runs
  `dhan-refresh-instruments` at once when no refresh succeeded within
  `DHAN_INSTRUMENT_MAX_AGE_HOURS` (80). The job is registered whenever `DHAN_CLIENT_ID` is set:
  the master is a public file and needs no Dhan session. Its timeout is raised to
  `DHAN_INSTRUMENT_SYNC_TIMEOUT_SECONDS` (1800).
- **Tick sizes were 100x too large:** the master's `TICK_SIZE` is in paise (`5.0000` = ₹0.05).
  Every Dhan instrument's `tick_size` is corrected on the next refresh.
- Normalized master fields on `trading.instruments` (migration 0028): `trading_symbol`
  (`INFY`, `NIFTY-Nov2026-25500-CE`), `display_name`, `name`, `isin`, `series`,
  `security_type`, `underlying_security_id`, `inactive_reason`, plus search/derivative indexes.
- Only `DHAN_INSTRUMENT_SEGMENTS` are kept (default NSE_EQ, BSE_EQ, IDX_I, NSE_FNO, BSE_FNO).
  Contracts past expiry are stored inactive (`expired`); rows missing from the master are
  deactivated (`missing_from_master` / `segment_disabled`), never deleted.
- Every refresh is recorded in `dhan.instrument_sync_runs` (rows read, skipped, duplicates,
  upserted, expired, deactivated, per segment, error); one refresh at a time (advisory lock).
- Migration 0028 also creates the (still unused) watchlist, candle-backfill and screener
  tables for the follow-up parts. `DHAN_UNIVERSE_BACKFILL*` settings are accepted but no job
  reads them yet.

## Unreleased: admin password reset

- `python -m app.auth.reset_password --email …` resets an existing, active admin's password in
  place (same UUID, role, data): Argon2id hash, every open session revoked, an
  `admin_password_reset` audit event, one transaction (nothing changes on error). Prompts
  twice without echo, or `--stdin`; never logs the password. Operator action only, not run by
  deploys ([docs/02-auth-and-finance.md](docs/02-auth-and-finance.md)).
- The user row is locked with `FOR UPDATE OF users`: a plain `FOR UPDATE` on `User` (whose
  `role` is a joined eager load) failed with "FOR UPDATE cannot be applied to the nullable side
  of an outer join".

## Unreleased: V1.6.4 watchdog health summary

- `GET /api/v1/ingest/health_summary` (collector, guard, service roles) for the Aureon V1.6.4
  Windows watchdog, with V1.6.4's exact response contract; missing records are `null` (never a
  default healthy state), a database failure is 503, `?detail=true` adds diagnostics
  ([docs/34-aureon-v163.md](docs/34-aureon-v163.md)). The watchdog answered "HTTP 404" before.
- Tests: the unmodified V1.6.4 watchdog (`tests/fixtures/aureon_v164`) polls the endpoint
  over HTTP and judges RUNNING / DEGRADED / PAUSED / FAILED from it.

## Unreleased: Aureon V1.6.3 database API and workers

The Assembler serves the V1.6.3 Windows box (`STORE_MODE=api`) and runs V1.6.3's Linux-side
work itself ([docs/34-aureon-v163.md](docs/34-aureon-v163.md)). No MT5 access or order execution
on the Assembler; no strategy change.

### Added

- Schema `aureon` (migration `0027`, additive, IF NOT EXISTS, grants mirrored, downgrade refuses
  while it holds data): V1.6.3 tables 001-004 + resumable review phases, stored report files,
  `mt5_deals.server`, queue/lookup indexes.
- `/api/v1/ingest/*` (collector, guard, service), the V1.6.3 reads, decisions, admin risk clear,
  admin review run, report files, `aureon/healthz|readyz` — exact V1.6.3 paths, bodies,
  validation and status codes; role bearer tokens `AUREON_API_*_TOKEN` (constant-time, least
  privilege, rate-limited, body-limited, audited in `aureon.api_audit`; admin actions also in
  `audit.audit_events`); prod refuses weak or reused tokens; Cloudflare Access exempts exactly
  these machine routes.
- Scheduler tasks (`AUREON_WORKERS_ENABLED`): detector, scheduler state machine with durable
  backfill ack and gap `backfill_range` jobs, watchdog, Discord delivery of `aureon.alerts`,
  signal queue (fail closed on risk), nightly verify, Sunday review, weekend auto-replay with
  coverage, recovery jobs, deal history, matching, JSON/HTML/CSV reports.
- Strategy and matching code copied verbatim from V1.6.3 (`indicators`, `matcher`,
  `build_positions`, R3.0 rules).

### Fixed (vs V1.6.3's own server)

- Accepts four requests the V1.6.3 client sends and V1.6.3's server rejected: trade event
  `close_sent`, `status=open` with `exit_reason=null`, heartbeat `DEGRADED`/`FAILED`, bar source
  `backfill`.

### Tests

- `tests/test_aureon_unit.py`, `tests/test_aureon_db.py`, and
  `tests/test_aureon_client_compat.py`, which drives the unmodified V1.6.3 collector and guard
  against the Assembler over HTTP (MetaTrader5 faked).

## Unreleased: live deploy progress

Stage-by-stage progress of backend and frontend deploys on the Spasta Deploys and CI card
([docs/33-deploy-progress.md](docs/33-deploy-progress.md)).

### Added

- `devops.deployment_stages` + progress columns on `devops.deployments` (migration `0026`,
  additive, grants mirrored).
- `scripts/deploy.sh` reports `pulling`, `validating`, `building`, `migrating`, `restarting`,
  `health_checking`, `verifying` (the running api image carries the expected commit) and
  `rolling_back`; exit 3 = failed before anything changed. ASTRA-FRONTEND's deploy script does
  the same (companion PR).
- `scripts/ops/deploy-poll.sh` runs the deploy in the background and reports progress every few
  seconds into the running API (`python -m app.workers.deploy_progress`), plus a final report;
  missed reports (the API restarting) are healed by the next.
- `GET /api/v1/github/actions/monitor/deploys/{run_id}/progress` and `/logs` (admin, read-only);
  monitor rows gain `stage`, `stale`, `stages_completed`, `stages_total`, and their status comes
  from the recorded deploy (`success` only when health-verified, else `unverified`).
- Expected vs deployed commit and `commit_verified`; PR number from a recorded merge or the
  merge subject; stale detection (`DEVOPS_DEPLOY_STALE_SECONDS`).

## Unreleased: Dhan login survives a Spasta re-login

The Dhan API-key login kept its one-time pending state under the Spasta session id, so when the
admin had to sign in to Spasta again after Dhan's redirect (a new session), `POST
/api/v1/dhan/auth/consume` answered `409 no_pending_login` (docs/11-dhan.md).

### Fixed

- **One-time login handoff bound to the admin, not the session**
  (`app/services/dhan_login_handoff.py`): `/start` creates a random 256-bit state, stored in
  Redis only as a hash (10 min) and given to the browser as an HttpOnly, Secure,
  `SameSite=Lax` cookie scoped to `/api/v1/dhan/auth`. `/callback` keeps Dhan's tokenId
  server-side (Fernet-encrypted, first callback wins) and redirects with `?dhan_login=ready`;
  the tokenId is no longer put in a URL. `/consume` (still admin + CSRF) claims it atomically
  and once (one Lua script) for the same user only.
- Specific refusals: `dhan_login_other_user` (403, the login stays for its owner),
  `no_pending_login`, `login_expired`, `login_already_used`, `login_not_returned`,
  `duplicate_callback`, `token_mismatch`; `dhan_unavailable` keeps the login so it can be
  retried (`retry: true`).
- The log scrubber redacts `tokenId=`, `consentAppId=` and similar one-time ids in any URL:
  httpx logged the server's own exchange call to Dhan with the tokenId.

Unchanged: client id check, Profile API verification, encrypted session storage, audit events.

### Files

`app/services/dhan_login_handoff.py` (new), `app/api/v1/dhan_auth.py`, `app/core/logging.py`,
`docs/11-dhan.md`, `tests/test_dhan_auth_db.py`, `tests/test_dhan_auth.py`.

## Unreleased: DevOps monitoring for ASTRA-FRONTEND

Verified repository, PR, build, deployment, runtime and notification data for the DevOps
dashboard ([docs/32-devops-monitoring.md](docs/32-devops-monitoring.md)). No trading, risk or
strategy code changed; no live-trading path added.

### Added

- **`devops` schema** (migration `0025`, additive): `repo_events`, `deployments`, `services`,
  `service_snapshots`, `events`, `notification_prefs`, `notification_reads`. Builds reuse
  `github.workflow_runs` / `workflow_jobs`.
- **GitHub activity**: `pull_request`, `deployment`, `deployment_status` webhooks are
  normalized (dedup keys shared with the backfill); workflow runs emit `build.*` transitions
  from the webhook worker and `github_actions_sync`; `devops_backfill` job after outages,
  stopping on a rate limit.
- **Server deploys**: `deploy.sh` records phase timestamps, a failure reason and `health_ok`
  only after its readiness gate; `deploy-poll.sh` passes them and the exit code to
  `deploy_record`, which writes `devops.deployments` (succeeded only when health-verified;
  unverified, rolled back, failed otherwise). Images carry their commit as the OCI revision.
- **Runtime collector**: `scripts/ops/runtime-collect.sh` + systemd timer on the host, piping
  `aureon.runtime/v1` reports into `python -m app.workers.runtime_record`; no Docker access
  for the API.
- **Read API** `/api/v1/devops/{overview,repositories,events,builds,deployments,services,feed}`,
  **SSE** `/api/v1/devops/stream` with `Last-Event-ID` replay, **notifications** with
  per-user unread counts, mark-read, preferences, repository subscriptions, failures-only.
- Separate PR / CI / deployment / runtime status vocabularies with `UNKNOWN`.
- `devops_purge` retention job.

### Changed

- The Dhan connection response model is named `DhanConnectionOut` (OpenAPI schema title only;
  the JSON is unchanged), so GitHub's `ConnectionOut` keeps its name in generated clients.
- UI manifest: read-only `GET /api/v1/devops/*` is not floored to `hidden` by the generic
  "deploy" rule.
- `deploy/sql/roles.sql` grants the new `devops` schema like the other app schemas.

### Files

`app/models/devops.py`, `alembic/versions/20261108_0025_devops_monitoring.py`,
`app/services/devops.py`, `app/services/devops_read.py`, `app/schemas/devops.py`,
`app/api/v1/devops.py`, `app/workers/runtime_record.py`, `app/workers/deploy_record.py`,
`app/workers/scheduler.py`, `app/services/github_webhooks.py`, `app/services/github_actions.py`,
`app/core/config.py`, `app/api/ui.py`, `app/api/router.py`, `app/api/v1/dhan_auth.py`,
`app/models/__init__.py`, `scripts/deploy.sh`, `scripts/ops/deploy-poll.sh`,
`scripts/ops/runtime-collect.sh`, `deploy/systemd/aureon-runtime-collect.{service,timer}`,
`deploy/sql/roles.sql`, `Dockerfile`, `compose.yaml`, `.env.example`, docs, tests
(`tests/test_devops_unit.py`, `tests/test_devops_db.py`, `tests/test_route_auth.py`,
`tests/test_db_roles.py`).

## V1.6.1: concurrency, credential and deployment hardening

The six V1.6.1 items (A1–A6), mapped to this repository's real components. Full mapping,
verdicts, compatibility notes and the bot coordination list: [docs/31-v1.6.1-hardening.md](docs/31-v1.6.1-hardening.md).
No trading strategy, live-trading path or database table changed.

### Fixed

- **A1 kill switch (`PUT /api/trading/accounts/{id}/risk-policy`).** An omitted `kill_switch`
  no longer releases an engaged switch; the policy row is locked for the update; optional
  `expected_updated_at` (409 `stale_policy`); releasing an engaged switch requires it (409
  `kill_switch_release_needs_version`); concurrent first PUTs answer 409 instead of 500. The
  account-mode change locks the account row.
- **A2 MT5 ingest token.** Must not reuse `MT5_STATUS_TOKEN`, `SECRET_KEY`,
  `GITHUB_WEBHOOK_SECRET`, `GITHUB_TOKEN` or `SETTINGS_MASTER_KEY`, and must be ≥ 32 chars,
  checked at prod startup, per request (503 `ingest_misconfigured`, fail closed), on managed
  settings writes (422) and in the settings test.
- **A3 deploy readiness.** `scripts/deploy.sh` promotes only on HTTP 200 + `status: ready` +
  every check ok, twice in a row; exit 2 when the rollback is unhealthy; `deploy-poll.sh`
  records a rollback only when it was healthy.
- **A4 migrations.** `python -m app.workers.migrate --check` (at_head 0 / pending 3 /
  diverged 4 / unreachable 1) from the real version table; `migrate` verifies head after
  upgrading; `deploy.sh` gates on it. Migration `0024` fixed: its constraint name was
  double-prefixed by the naming convention and `upgrade head` failed on every database.
  Migration `0023`'s downgrade fixed: it restored the constraint under a name `0020`'s
  downgrade could not find, so `downgrade base` and the rollback drill failed.
- **A5 paper intents.** The scheduler locks each intent (`FOR UPDATE SKIP LOCKED`) and can no
  longer overwrite a cancel; an explicit transition table (`paper.transition`, 409
  `invalid_transition`); compare-and-set on `status` via the SQLAlchemy version column (409
  `conflict`); optional `expected_status` on cancel/approve (409 `status_mismatch`).
- **A6 alerts.** Fenced delivery leases instead of claim-then-send: a send cancelled by the
  scheduler timeout or lost in a crash is retried on the next run instead of being silenced
  for `ALERTS_DEDUP_MINUTES`; stale workers cannot commit or release a newer attempt.

### Files changed

| File | Item |
|---|---|
| `app/api/v1/trading.py` | A1 (risk-policy PUT, mode-change row lock) |
| `app/schemas/trading.py` | A1 (`kill_switch` optional, `expected_updated_at`) |
| `app/api/v1/mt5_push.py` | A2 |
| `app/core/prod_checks.py` | A2 (shared token checks) |
| `app/services/app_settings.py` | A2 (managed-settings write + test) |
| `scripts/deploy.sh` | A3, A4 |
| `scripts/ops/deploy-poll.sh` | A3 |
| `app/workers/migrate.py` | A4 (`--check`, post-upgrade verification) |
| `alembic/versions/20261105_0024_dhan_connections.py` | A4 (constraint-name fix, no schema change) |
| `alembic/versions/20261104_0023_direct_ai_providers.py` | A4 (downgrade constraint name, no schema change) |
| `app/services/paper.py` | A5 (transition table, scheduler locking) |
| `app/api/v1/paper.py` | A5 (`expected_status`, 409 mapping) |
| `app/models/trading.py` | A5 (`status` as version column, no schema change) |
| `app/services/alerts.py` | A6 |
| `docs/31-v1.6.1-hardening.md`, `docs/deploy-automation.md`, `README.md`, `CHANGELOG.md` | docs |
| `tests/test_v161_unit.py`, `tests/test_v161_mt5_token_unit.py`, `tests/test_v161_deploy_scripts.py`, `tests/test_v161_db.py` | regression tests |

### Compatibility

One documented API change: releasing an engaged kill switch needs `expected_updated_at`
(copy `updated_at` from GET). Everything else is additive or tightens a misconfiguration.
