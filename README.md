# Aureon_Agentic

Self-hosted backend for Spasta: a FastAPI modular monolith on PostgreSQL 17, Redis and a local
Ollama. It is offline-first, has no external AI APIs and does no live trading.

**Status: Phases 1, 2 (auth), 3, 4, 5 (registry, scheduler, monitoring, coding agent) and Trading
Phases A, B (Dhan, read-only), C (MT5 bot, read-only) and D (research agents) are done; finance is
blocked on the live schema and the image pipeline hookup is pending the /opt/image-agent inspection.** Phase 1 provides
health, config, logging, async DB and Alembic. Phase 2 adds cookie-session auth with CSRF,
rate limiting and an audit log ([docs/02-auth-and-finance.md](docs/02-auth-and-finance.md)).
Frontend support adds change sync, session management, receipt attachments, public shares and an
offline-by-default news feed ([docs/02-auth-and-finance.md](docs/02-auth-and-finance.md#frontend-support)).
Phase 3 adds local-Ollama chat over SSE with stored conversations ([docs/03-ollama.md](docs/03-ollama.md)).
Phase 4 adds queued Stable Diffusion jobs run by a separate worker ([docs/04-imaging.md](docs/04-imaging.md)).
Phase 5 adds the agent registry, a cron scheduler and system status ([docs/05-agents.md](docs/05-agents.md)).
Trading Phase A adds the read-only, broker-agnostic trading core: schemas, BrokerAdapter,
execution modes (LIVE unreachable) and risk types ([docs/10-trading-foundation.md](docs/10-trading-foundation.md)).
Trading Phase B adds the read-only DhanHQ v2 adapter, live-feed worker and sync jobs ([docs/11-dhan.md](docs/11-dhan.md)).
Trading Phase C watches the separate Aureon MT5 bot through its read-only status export ([docs/12-mt5-adapter.md](docs/12-mt5-adapter.md)).
Trading Phase D adds the research agents: market data, indicators, signals, risk decisions, backtests and Ollama explanations ([docs/13-research-agents.md](docs/13-research-agents.md)).
Trading Phase F enables PAPER mode end to end: intents, risk, admin approval, simulated fills, P&L and reconciliation; no broker orders ([docs/15-paper-trading.md](docs/15-paper-trading.md)).
Phase E hardens production: an ENV=prod startup checklist, health-gated compose start order, nightly backups with a weekly restore drill, systemd boot units and a security review ([docs/16-production-hardening.md](docs/16-production-hardening.md), [docs/17-security-review.md](docs/17-security-review.md); sudo steps in [docs/manual-setup.md](docs/manual-setup.md)).

## Layout

```
app/
  main.py            app factory + lifespan
  core/              settings, JSON logging, request-ID middleware
  api/v1/            routers (health, auth, models/chat/conversations, images, agents/system)
  auth/              password hashing, sessions, CSRF, rate limit, admin bootstrap
  db/                async engine/session, declarative base
  agents/ollama/     Ollama HTTP client + generation limiter
  services/          health checks, audit log, chat streaming + conversation storage
  models/ schemas/   SQLAlchemy models (core, audit, ai) and Pydantic schemas
  agents/imaging/    image generators (subprocess wrapper, stub)
  workers/imaging.py image worker process (one job at a time)
  agents/registry.py agent registration, heartbeats, runs, logs
  workers/scheduler.py cron scheduler (code-registered tasks, retries + backoff)
  agents/trading/    BrokerAdapter interface, Decimal DTOs, sessions, fake adapter, rate limiter
  agents/trading/dhan/ DhanHQ v2 client, parsers, adapter, binary feed parser
  workers/dhan_feed.py Dhan live feed → Redis (own process)
  agents/trading/mt5/ MT5 bot status export reader + MT5Adapter (no MT5 calls)
  agents/research/   indicators, strategies, backtest engine, AI explainer (pandas/numpy)
  workers/research.py research agents (own process, bounded queues)
  agents/coding/     coding agent guards: repo allowlist, path safety, command allowlist, git, patches
  workers/coding.py  coding worker (worktrees + allowlisted tests; never pushes)
alembic/             async migrations (version table: public.aureon_alembic_version)
tests/  scripts/  deploy/  docs/
```

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Liveness. Always 200 while the process serves. |
| GET | `/api/health/ready` | 200 if Postgres, Redis, Ollama and the HDD are all OK, else 503 with per-check detail. |
| POST | `/api/auth/login` | Email + password; sets session and CSRF cookies. |
| POST | `/api/auth/logout` | Revokes this session (needs `X-CSRF-Token`). |
| GET | `/api/auth/me` | Current user. |
| POST | `/api/auth/sessions/revoke-all` | Signs out every session of the user (needs `X-CSRF-Token`). |
| GET | `/api/models` | Installed Ollama models (cached 60 s). |
| GET | `/api/models/status` | Reachability, loaded models, last use. |
| POST | `/api/chat` | Streaming chat (SSE); stores both turns. |
| GET | `/api/conversations` | Your conversations, paginated. |
| GET / DELETE | `/api/conversations/{id}` | One conversation with messages / delete it. |
| POST | `/api/images/generate` | Queue an image job (202). |
| GET | `/api/images/jobs/{id}` | Job status, progress, queue position. |
| POST | `/api/images/jobs/{id}/cancel` | Cancel a queued or running job. |
| GET | `/api/images` | Your image gallery, paginated. |
| GET / DELETE | `/api/images/{id}/file`, `/api/images/{id}` | Download / delete an image. |
| GET | `/api/agents`, `/api/agents/{id}`, `/api/agents/{id}/runs` | Agent registry, state, runs. |
| GET | `/api/agents/{id}/logs` | Agent logs (admin). |
| POST | `/api/agents/{id}/pause`, `/resume` | Pause / resume an agent (admin, audited). |
| GET / POST | `/api/scheduler/jobs`, `…/{id}/pause\|resume\|run` | Scheduled jobs (admin, audited). |
| GET | `/api/system/status` | Host, data stores, agents, containers, host services (admin). |
| GET | `/api/trading/accounts`, `/api/trading/accounts/{id}[/health\|funds\|positions\|orders\|risk-policy]` | Trading accounts and read-only broker data. |
| POST / PUT | `/api/trading/accounts`, `…/{id}/mode`, `…/{id}/risk-policy` | Admin: create account, change mode (LIVE gated), risk policy. |
| GET | `/api/trading/instruments`, `/candles`, `/quotes`, `/strategies`, `/signals`, `/executions`, `/agents` | Trading data (read-only). |
| GET | `/api/trading/accounts/{id}/holdings`, `/trades`, `/api/trading/option-chain[/expiries]` | Holdings, trade book, option chain (Dhan). |
| GET | `/api/trading/mt5/status` | MT5 bot: health, agents, positions, backtests, calculated performance. |
| PUT | `/api/trading/mt5/instruments/{symbol}` | Admin: register an MT5 symbol's contract spec (audited). |
| POST | `/api/trading/strategies` | Admin: new strategy / next immutable version (audited). |
| POST / GET | `/api/trading/strategies/backtest`, `/api/trading/backtests[/{id}]` | Queue backtests (param grid); results + trades. |
| GET | `/api/trading/indicators`, `/api/trading/risk-decisions`, `/api/trading/ai-analyses` | Stored analysis output. |
| POST | `/api/trading/signals/{id}/explain`, `/api/trading/backtests/{id}/explain` | Queue an Ollama explanation (explanation only). |
| POST / GET | `/api/trading/order-intents[/{id}[/approve\|cancel]]` | Paper intents: risk-checked, admin approval, simulated fill ([docs/15-paper-trading.md](docs/15-paper-trading.md)). |
| GET / POST | `/api/trading/accounts/{id}/paper[/reconcile\|reconciliations]` | Paper book: equity, P&L, positions; reconciliation. |
| GET | `/api/trading/stream` | SSE: signals, risk decisions, backtests, agent state, Dhan quotes ([docs/14-frontend.md](docs/14-frontend.md)). |
| GET | `/api/repos`, `/api/repos/{name}/tree\|file\|commits\|branches` | Allowlisted local repos, read from git (admin). |
| POST / GET | `/api/code/analyze`, `/api/code/tasks[/{id}[/diff\|approve\|reject]]` | Coding agent: explain code; patch in an isolated worktree, test, approve (admin, audited; never pushes). |
| CRUD | `/api/finance/{trades,expenses,secured,documents,portfolio-profiles}`, `/api/finance/totals`, `/api/finance/claim` | Spasta finance tables, scoped to the signed-in user ([docs/02-auth-and-finance.md](docs/02-auth-and-finance.md#finance)). |
| GET | `/api/finance/changes`, `?updated_since=` on finance lists | Change sync with delete tombstones ([docs/02-auth-and-finance.md](docs/02-auth-and-finance.md#frontend-support)). |
| POST / GET / DELETE | `/api/finance/attachments[/{id}[/file]]` | Receipt attachments on the HDD (PNG/JPEG/WebP/PDF, owner-scoped). |
| GET / DELETE | `/api/auth/sessions[/{id}]` | List your active sessions; revoke one (audited). |
| GET | `/api/public/shares/{doc_id}`, `/api/public/forex` | No auth, rate limited: public shares, pulled forex news. |
| GET | `/api/docs`, `/api/openapi.json` | Swagger UI and schema; both off when `ENV=prod` ([docs/16-production-hardening.md](docs/16-production-hardening.md)). |

## Configuration

Every variable is listed in [.env.example](.env.example).

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENV` | `dev` | `dev`/`test`/`prod`. In `prod` the API refuses to start unless the checklist in [docs/16](docs/16-production-hardening.md#production-checklist-envprod) passes (HDD marker, strong `SECRET_KEY`, secure cookies, no DEBUG/docs). |
| `DEBUG` / `API_DOCS_ENABLED` | `false` / unset | Both refused in prod; docs default to on outside prod. |
| `PHASE_G_APPROVED` | empty | Must be `yes` for `TRADING_LIVE_ENABLED=true` to load (LIVE stays blocked until Phase G). |
| `MIGRATE_WAIT_SECONDS` | `180` | How long the `migrate` one-shot waits for Postgres before `alembic upgrade head`. |
| `LOG_LEVEL` | `INFO` | Log level. |
| `DATABASE_URL` | required | `postgresql+asyncpg://…@<postgres-container>:5432/spasta` |
| `REDIS_URL` | `redis://aureon-redis:6379/0` | Redis from this compose file. |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Existing Ollama container on the private network. |
| `IMAGE_AGENT_PATH` | `/opt/image-agent` | SD 1.5 pipeline (Phase 4). |
| `DATA_ROOT` | `/mnt/spasta-data` | HDD mount. |
| `DATA_ROOT_MARKER` | `.spasta-data-mounted` | File that must exist on the HDD. |
| `SECRET_KEY` | required, ≥ 32 chars | Key for CSRF tokens (HMAC). Changing it invalidates CSRF tokens. |
| `CORS_ORIGINS` | empty | Comma-separated browser origin allowlist. |
| `HEALTH_CHECK_TIMEOUT` | `3` | Seconds per readiness check. |
| `SESSION_TTL_HOURS` | `12` | Absolute session lifetime. |
| `COOKIE_SECURE` | `true` | Secure + `__Host-` cookies. Set `false` only for plain-http dev. |
| `LOGIN_MAX_FAILURES` | `5` | Failed logins per (email, IP) per window. |
| `LOGIN_MAX_FAILURES_PER_IP` | `20` | Failed logins per IP per window. |
| `LOGIN_WINDOW_SECONDS` | `900` | Rate-limit window. |
| `FINANCE_ATTACHMENT_MAX_BYTES` | `10485760` | Max receipt upload size (413 above). |
| `FINANCE_ATTACHMENTS_MAX_PER_USER` | `2000` | Receipt attachments per user. |
| `FINANCE_TOMBSTONE_RETENTION_DAYS` | `30` | Delete tombstones kept for `/api/finance/changes`. |
| `FINANCE_CHANGES_MAX_ROWS` | `5000` | Rows per list in one `/changes` call. |
| `PUBLIC_RATE_LIMIT_PER_MINUTE` | `60` | `/api/public/*` requests per IP per minute. |
| `NEWS_FEED_URLS` | empty | JSON list of RSS/Atom URLs for `news_pull`; empty = off. |
| `NEWS_PULL_CRON`, `NEWS_HTTP_TIMEOUT`, `NEWS_MAX_ITEMS`, `NEWS_MAX_FEED_BYTES` | `*/30 * * * *`, `10`, `200`, `2097152` | News job schedule and limits. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxy IPs whose `X-Forwarded-For` uvicorn trusts. |
| `OLLAMA_MAX_CONCURRENT` / `OLLAMA_MAX_QUEUE` | `1` / `3` | Running / waiting generations; beyond that `POST /api/chat` returns 429. |
| `OLLAMA_QUEUE_RETRY_AFTER` | `15` | `Retry-After` seconds on 429. |
| `OLLAMA_CONNECT_TIMEOUT` / `OLLAMA_READ_TIMEOUT` | `5` / `300` | Connect timeout; longest silence between chunks. |
| `OLLAMA_MODEL_CACHE_SECONDS` | `60` | `/api/tags` cache. |
| `CHAT_TIMEOUT_SECONDS` | `900` | Whole chat request, including queue time. |
| `CHAT_HEARTBEAT_SECONDS` | `15` | SSE keep-alive interval. |
| `CHAT_DEFAULT_NUM_CTX` / `CHAT_MAX_NUM_CTX` | `4096` / `8192` | Context window default / cap. |
| `IMAGE_GENERATOR` | `subprocess` | `subprocess` (wrap /opt/image-agent) or `stub` (dev). |
| `IMAGE_AGENT_COMMAND` | empty | JSON argv template for the entrypoint (no shell). |
| `IMAGE_AGENT_REQUIREMENTS` | `requirements.txt` | Requirements file used to build the worker image. |
| `IMAGE_AGENT_ENV_PASSTHROUGH` | empty | Env var names copied into the entrypoint. |
| `IMAGE_THREADS` | `3` | CPU threads for the pipeline. |
| `IMAGE_ALLOWED_SIZES` | `512x512,512x768,768x512` | Allowed output sizes. |
| `IMAGE_DEFAULT_STEPS` / `IMAGE_MAX_STEPS` | `25` / `50` | Steps default / cap. |
| `IMAGE_QUEUE_MAX` / `IMAGE_RETRY_AFTER` | `5` / `600` | Waiting-job cap; `Retry-After` on 429. |
| `IMAGE_JOB_TIMEOUT_SECONDS` | `1800` | Per-job limit. |
| `IMAGE_MAX_ATTEMPTS` | `2` | Times a job may start before an interruption marks it failed. |
| `IMAGE_POLL_SECONDS` | `2` | Cancel / progress check interval. |
| `AGENT_HEARTBEAT_SECONDS` / `AGENT_STALE_SECONDS` | `15` / `60` | Heartbeat interval; stale threshold. |
| `AGENT_LOG_RETENTION_DAYS` / `AGENT_RUN_RETENTION_DAYS` | `30` / `90` | Purged by the scheduler. |
| `SCHEDULER_TICK_SECONDS` / `SCHEDULER_TIMEZONE` | `5` / `Asia/Kolkata` | Poll interval; cron time zone. |
| `DOCKER_API_URL` | `http://aureon-docker-proxy:2375` | Read-only Docker socket proxy (empty = off). |
| `HOST_STATUS_FILE` / `HOST_STATUS_MAX_AGE_SECONDS` | `…/status/host-services.json` / `120` | Host unit states from deploy/host-status. |
| `TRADING_LIVE_ENABLED` | `false` | LIVE feature flag; refused without `PHASE_G_APPROVED=yes`; LIVE also needs Phase G preconditions. |
| `TRADING_LIVE_TOKEN_TTL_SECONDS` | `300` | LIVE confirmation token lifetime. |
| `TRADING_ADAPTER_TIMEOUT` | `10` | Per-call broker adapter timeout. |
| `DHAN_CLIENT_ID` / `DHAN_ACCESS_TOKEN` | unset | Dhan credentials (env only); unset = no Dhan adapter or jobs. |
| `DHAN_TOKEN_WARN_HOURS` | `4` | Warn when the 24 h token has less left. |
| `DHAN_FEED_INSTRUMENTS` / `DHAN_FEED_MODE` | `IDX_I:13` / `quote` | Live feed subscription (`SEGMENT:SECURITY_ID`). |
| `DHAN_FEED_QUEUE_MAX` / `DHAN_FEED_STALE_SECONDS` | `10000` / `60` | Feed queue bound; stale threshold. |
| `DHAN_RECONCILE_CRON` / `DHAN_INSTRUMENTS_CRON` / `DHAN_BACKFILL_CRON` | see .env.example | Dhan scheduled jobs (IST). |
| `DHAN_BACKFILL_INSTRUMENTS` / `_TIMEFRAMES` / `_DAYS` | empty / `1d,15m` / `30` | Candle backfill. |
| `DHAN_STATIC_IP` | unset | Whitelisted IP for scripts/dhan_preflight.py. |
| `MT5_STATUS_FILE` / `MT5_STATUS_URL` / `MT5_STATUS_TOKEN` | unset | MT5 bot export (file wins); unset = no MT5 adapter or job. |
| `MT5_ACCOUNT_ID` / `MT5_SERVER` / `MT5_ACCOUNT_CURRENCY` | unset / unset / `USD` | The bot's MT5 login, server, deposit currency. |
| `MT5_STALE_SECONDS` / `MT5_SYNC_CRON` / `MT5_HTTP_TIMEOUT` | `90` / `* * * * *` / `5` | Bot stale window, sync job, HTTP timeout. |
| `RESEARCH_*` / `RISK_*` / `BACKTEST_*` | see .env.example | Research worker cadence, queues, indicators; risk assumptions; backtest limits. |
| `RESEARCH_AI_MODEL` | unset | Local Ollama model for explanations; unset = explanations off. |
| `TRADING_STREAM_POLL_SECONDS` / `_HEARTBEAT_SECONDS` / `_MAX_SECONDS` | `2` / `15` / `1800` | Trading SSE stream cadence and lifetime. |
| `CODE_REPOS` / `CODE_ALLOWED_COMMANDS` / `CODE_MODEL` | unset / built-in list / unset | Coding agent repo allowlist, exact-argv command allowlist, local Ollama coding model. |
| `PRIVATE_AI_NETWORK` | required (compose) | Existing Docker network shared with Postgres/Ollama. |

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
make install
make check                     # ruff + mypy --strict + pytest
PG_ADMIN_URL=postgresql://postgres:pw@127.0.0.1:5432/postgres make test-db
```

`make test-db` creates a throwaway database, runs the migration up/down tests and the full
readiness test, then drops it. Never point it at `spasta`.

## Deploy

See [deploy/README.md](deploy/README.md) and the ops runbook
[docs/16-production-hardening.md](docs/16-production-hardening.md) (backups, restore, reboot order). Admin bootstrap: `python -m app.auth.bootstrap --email …`. Server facts still to be collected:
[docs/00-inspection.md](docs/00-inspection.md).

## Safety rules (Phase 1)

- Only the API is published, on `127.0.0.1:8000`. Postgres, Ollama and Redis have no host ports.
- The containers run as non-root, read-only, with all capabilities dropped and `no-new-privileges`,
  with memory/CPU limits and capped logs (Phase E).
- Migrations are additive. Autogenerate ignores tables that are not in our models.
- Readiness errors report the exception type only, so DSNs and hosts never leak into responses.
