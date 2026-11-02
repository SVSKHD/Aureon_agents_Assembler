# 24 — Production integration (Phase 22)

Phase 22 wires the finished pieces into the production box without touching it from here:
scoped database roles, the MT5 read-only feed confirmed end to end, health/restart/startup
order checks, Discord alerts, a migration rollback drill, Cloudflare Access for
`spasta.online` and `api.spasta.online`, resource caps, and a 16-step acceptance run with
recorded results. **No migration** (the schema `spasta` is created by `deploy/sql/roles.sql`,
not by alembic). Every server-side action is a script or a config file tested here and listed
in [manual-setup.md](manual-setup.md#22--production-integration).

## What it does

| Area | Piece |
| --- | --- |
| DB roles | `deploy/sql/roles.sql` (idempotent, never drops) + `scripts/ops/apply-db-roles.sh` (passwords via stdin, never argv). Roles `aureon_app` (DML, no DDL, append-only audit), `aureon_migrator` (owns the app schemas), `aureon_readonly` (SELECT), `aureon_audit_writer` (NOLOGIN group: SELECT + INSERT on the audit trail). Creates the empty, documented schema `spasta`. |
| Two URLs | `DATABASE_URL` = the app role; `MIGRATIONS_DATABASE_URL` = the migrator, read by alembic `env.py` and `python -m app.workers.migrate` first. Compose gives it only to the `migrate` one-shot, from `.env.migrate` (`required: false`). |
| MT5 feed | Confirmed read-only: an end-to-end fixture test (bot export → `mt5_sync_status` job → `/api/trading/mt5/status`, positions, the trading SSE stream, the integrations card), plus static checks that no order method / `order_send` / `MetaTrader5` import / HTTP write exists. |
| Health card | `GET /api/v1/system/integrations` (admin): MT5 freshness, Dhan, GitHub, webhook worker, Claude CLI, Ollama models (configured vs installed), Discord, the DB role (superuser = scoped roles not in use), backups. |
| Alerts | Scheduler task `alerts_dispatch` (job `alerts-dispatch`, `ALERTS_CRON`, only when `DISCORD_WEBHOOK_URL` is set): open incidents ≥ warning, stale heartbeats, failed/old backups, approvals about to expire → Discord via the Phase 20 adapter. One alert per fingerprint per `ALERTS_DEDUP_MINUTES` (Redis `SET NX EX`); a failed send is retried next run; at most `ALERTS_MAX_PER_RUN` per run, most severe first. |
| Backup status | `scripts/ops/backup.sh` writes `$DATA_ROOT/ops-status/backup.json` (0644, no secrets) on success and failure; the scheduler mounts the HDD **read-only** to read it. Backups now also dump the `github` and `spasta` schemas (the Phase 15 tables were missing from the dump). |
| Rollback drill | `scripts/ops/rollback-drill.sh [--steps N]`: a scratch DB `aureon_rbk_<ts>` → upgrade head → downgrade −1 ×N → upgrade +1 back to head → schema snapshot identical → app smoke (every mapped table selectable, version = head) → JSON report → drops only its scratch DB. `python -m app.workers.migrate --rollback-drill DB` does the in-container part. |
| Cloudflare | `deploy/cloudflare/{README.md,config.yml.example,access.tf.example}`, `deploy/nginx/api-spasta-online.conf`, and the optional app middleware `app/core/cf_access.py` (`CF_ACCESS_TEAM_DOMAIN` + `CF_ACCESS_AUD`, default off). |
| Resource caps | Asserted for every compose service; the one-heavy-model rule asserted for the coding/repair `HEAVY_MODEL_LOCK`, the research AI lane, the image worker and the Claude review runner. |
| Acceptance | `scripts/ops/acceptance.py`: 16 steps, PASS / FAIL / SKIP, JSON report. |

## Endpoints

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/v1/system/integrations` | admin | `{generated_at, ok, problems[], integrations{mt5, dhan, github, webhook_worker, claude_cli, ollama, discord, database, backups}}`; each section has `status` ok / degraded / down / not_configured / disabled. Read-only; only Ollama (`/api/tags`, cached) and the Claude CLI version check (cached, only when enabled) are probed live. Errors are exception types only. |

No other endpoint changes. With `CF_ACCESS_*` set, every route except `/api/v1/github/webhooks`,
`/api/public/*` and `/api/health*` answers **403 `cf_access_required`** without a valid
Cloudflare Access token.

## Config

| Variable | Default | Meaning |
| --- | --- | --- |
| `MIGRATIONS_DATABASE_URL` | unset | The migrator role's URL; alembic and the migrate worker use it before `DATABASE_URL`. Put it in `.env.migrate`, not `.env`. |
| `ALERTS_CRON` | `*/5 * * * *` | Alert job schedule; empty = no job. The job exists only when `DISCORD_WEBHOOK_URL` is set. |
| `ALERTS_DEDUP_MINUTES` | `60` | One alert per fingerprint per window (an incident's severity is part of its fingerprint, so an escalation alerts again). |
| `ALERTS_MAX_PER_RUN` | `10` | Cap per run; the rest wait for the next run. |
| `ALERTS_APPROVAL_EXPIRY_MINUTES` | `10` | Pending approvals expiring within this window are announced. |
| `BACKUP_STATUS_FILE` | `/mnt/spasta-data/ops-status/backup.json` | Written by backup.sh; unreadable = no backup alert, `unknown` on the card. |
| `BACKUP_MAX_AGE_HOURS` | `26` | Older last success = backup alert. |
| `CF_ACCESS_TEAM_DOMAIN` | unset | `<team>.cloudflareaccess.com`. With `CF_ACCESS_AUD`, turns on the app-level JWT check. Set one without the other and `ENV=prod` refuses to start. |
| `CF_ACCESS_AUD` | empty | Comma list of Access AUD tags (web, api). |
| `CF_ACCESS_CERTS_TTL_SECONDS` | `3600` | JWKS cache; an unknown `kid` refetches at most once a minute (rotation). |
| `CF_ACCESS_LEEWAY_SECONDS` | `30` | Clock leeway for `exp`/`nbf`/`iat`. |
| `CF_ACCESS_BYPASS_PATHS` | `/api/v1/github/webhooks,/api/public/,/api/health` | Path prefixes (segment-aware: `/api/healthz` is not bypassed). |

Ops (`scripts/ops/ops.env`): `AUREON_MIGRATOR_ROLE` (scratch DB owner for the rollback
drill), `BACKUP_STATUS_FILE`. Wrapper inputs (`apply-db-roles.sh`): `AUREON_APP_DB_PASSWORD`,
`AUREON_MIGRATOR_DB_PASSWORD`, `AUREON_READONLY_DB_PASSWORD` (or prompted),
`AUREON_{APP,MIGRATOR,READONLY,AUDIT_WRITER}_ROLE`.

## Schemas and roles

| Schema | Owner after roles.sql | `aureon_app` | `aureon_readonly` |
| --- | --- | --- | --- |
| `core ai agents trading dhan mt5 github` | migrator | SELECT INSERT UPDATE DELETE (+ sequences) | SELECT |
| `audit` | migrator | SELECT INSERT on everything; UPDATE DELETE only on `approval_requests`, `approval_policies` | SELECT |
| `audit.audit_events`, `approval_events`, `approval_artifacts` | migrator | **INSERT + SELECT only** (explicit REVOKE UPDATE/DELETE/TRUNCATE from app, writer group and PUBLIC) | SELECT |
| `spasta` | migrator | DML (empty today) | SELECT |
| `public.{trades,expenses,secured,astra_documents,portfolio_profiles}` | unchanged (legacy app) | DML, no TRUNCATE | SELECT |
| `public.aureon_alembic_version` | migrator | SELECT | SELECT |

No role gets TRUNCATE, REFERENCES or TRIGGER; no `CREATE` on any schema or the database
except the migrator; `CREATE ON SCHEMA public` is revoked from PUBLIC. Default privileges
cover tables the migrator creates later; in `audit` the default is append-only (fail
closed), so a new mutable audit table needs `roles.sql` updated (a test fails until it is
classified) and re-applied.

## Health checks, restart policies, startup order

Unchanged from Phase E and now asserted for the Phase 17/18 workers too
(`tests/test_compose_hardening.py`): every long-running service (api, redis, scheduler,
docker-proxy, imaging-worker, dhan-feed, research-worker, coding-worker — which also runs
the self-repair loop under `HEAVY_MODEL_LOCK` — and github-webhook-worker) has a healthcheck
and `restart: unless-stopped`; one-shots `mount-check` and `migrate` are `restart: "no"`.
Order: mount-check → redis (healthy) → migrate (exit 0) → api (healthy) → workers.

## Resource budget

The server is a 4-core box shared with the existing Postgres and Ollama containers (outside
this compose project). Container limits (`cpus`, `mem_limit`) are ceilings, not
reservations:

| Service | Profile | CPUs | Memory |
| --- | --- | --- | --- |
| api | – | 2.0 | 2 GiB |
| scheduler | – | 1.0 | 512 MiB |
| redis | – | 0.5 | 384 MiB |
| docker-proxy | – | 0.25 | 64 MiB |
| migrate / mount-check | one-shot | 1.0 / 0.1 | 512 / 32 MiB |
| **always on** | | **3.75** | **≈ 2.9 GiB** |
| imaging-worker | imaging | 3.0 | 10 GiB |
| coding-worker (+ repair) | coding | 2.0 | 3 GiB |
| research-worker | research | 1.5 | 1 GiB |
| dhan-feed | dhan | 1.0 | 512 MiB |
| github-webhook-worker | github | 0.5 | 256 MiB |

Plus Ollama's loaded model (a 7B Q4 coder ≈ 5–6 GB). Do not run `imaging` and `coding`
together on a 16 GB host; check `free -h` and `docker stats --no-stream` (manual-setup).
Tests assert: no service above 3 CPUs (one core always left), always-on memory ≤ 4 GiB and
CPUs ≤ 4, the two heavy workers opt-in with fixed container names and no replicas.

One heavy model at a time (asserted in `tests/test_prod_integration_unit.py`):

- **coding/repair:** `coding.run_task` and `repairs.run_repair` are only called inside
  `async with HEAVY_MODEL_LOCK` (AST check), and a repair waits while a code task holds it;
- **research AI:** one consumer per lane, a bounded lane queue (`RESEARCH_QUEUE_MAX`), and
  observed concurrency 1;
- **image worker:** a Redis NX lock (one worker instance) and one `current_job`;
- **Claude reviews:** `ReviewRunner` runs one at a time with a bounded queue (429 beyond);
- **chat:** `OLLAMA_MAX_CONCURRENT` defaults to 1.

## Acceptance list

No 16-step list existed in the repo, so this phase defines it (decision D-22-6) to cover the
roadmap end to end. `scripts/ops/acceptance.py` implements each step:

| # | Step | What PASS means | SKIP when |
| --- | --- | --- | --- |
| 1 | health/ready | `/api/health` ok; `/api/health/ready` all checks ok (404 through nginx = on-box only, noted) | – |
| 2 | login/CSRF | login sets session + CSRF; an unsafe call without the header is 403; no session is 401 | – |
| 3 | finance CRUD + ownership | create/patch/read/delete an expense with exact decimals; a foreign id is 404; anonymous 401 | – |
| 4 | paper trade via the approval engine | approve without a password files an approval (202); a wrong password is refused; re-auth approves → FILLED/SUBMITTED | no fresh price for the instrument (risk check fail-closed) |
| 5 | LIVE unreachable | the live token and the LIVE mode switch are refused; the account stays PAPER | – |
| 6 | MT5 feed fresh | card `mt5.fresh` and `/api/trading/mt5/status` ok | no MT5 export configured |
| 7 | Dhan read | Dhan account health ok and positions read | no Dhan credentials / account |
| 8 | chat via Ollama | `/api/chat` streams token events and `done` | no model, or `--skip-heavy` |
| 9 | image job | a job reaches `done` | no image worker, or `--skip-heavy` |
| 10 | coding agent task | a task reaches ready/tests_failed, then is rejected | no coding worker / `AUREON_ACCEPTANCE_CODE_REPO`, or `--skip-heavy` |
| 11 | GitHub connection + repo browse | connection check ok; repositories listed; a tree read | no PAT |
| 12 | PR merge gated by approval with re-auth | merge files an approval (202), a wrong password is refused, it is denied, the PR is not merged | no PAT / `AUREON_ACCEPTANCE_PR` |
| 13 | webhook HMAC + idempotency | bad signature 401; signed `ping` 202; replay 200 `duplicate` | no secret on the server / in `AUREON_ACCEPTANCE_WEBHOOK_SECRET` |
| 14 | incident → repair PR (no merge) | repair PRs exist and there is no merge route | repairs disabled or no PR yet |
| 15 | Claude review advisory-only | reviews exist and all are `advisory` | review disabled / CLI unavailable / none yet |
| 16 | backup + restore drill | backup status ok and fresh; the latest drill report passed (`AUREON_ACCEPTANCE_DRILL_DIR`) | the status file is not readable |

### Results (build sandbox, 2026-11-02)

Run against a local `uvicorn --factory app.main:app_factory` on `127.0.0.1:8765` (ENV=dev,
`COOKIE_SECURE=false`), a scratch Postgres DB at head, local Redis DB 6, the scheduler and
the image worker (`IMAGE_GENERATOR=stub`) as local processes, `tests/fake_ollama.py` as
Ollama, and a fake MT5 bot exporter refreshing the `tests/fixtures/mt5/status.json` export
every 10 s (with a last close, so the paper step has a fresh price; `XAUUSD` registered via
the API). No PAT, Dhan credentials, Claude CLI, repair model, coding worker or backup on
this machine. Command:
`acceptance.py --base-url http://127.0.0.1:8765 --wait 120` with
`AUREON_ACCEPTANCE_INSTRUMENT_ID=4` and the local webhook secret.

| # | Step | Result | Detail |
| --- | --- | --- | --- |
| 1 | health/ready | PASS | ready ok (data_root, ollama, postgres, redis) |
| 2 | login + CSRF | PASS | login 200, 403 without the CSRF header, 401 without a session |
| 3 | finance CRUD + ownership | PASS | exact decimals; foreign id 404; anonymous 401 |
| 4 | paper trade via the approval engine | PASS | approval filed (202), wrong password refused, re-auth → SUBMITTED (LIMIT far below the price) |
| 5 | LIVE unreachable | PASS | live-token 403, mode LIVE 403 `live_disabled`; still PAPER |
| 6 | MT5 feed fresh | PASS | mt5-bot heartbeat 34.8 s old (window 90 s) — fake exporter |
| 7 | Dhan read | SKIP | DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set |
| 8 | chat via Ollama | PASS | fake Ollama: 2 token events, done |
| 9 | image job | PASS | stub generator: job done |
| 10 | coding agent task | SKIP | coding worker not running |
| 11 | GitHub connection + repo browse | SKIP | no PAT |
| 12 | PR merge gated by approval with re-auth | SKIP | no PAT |
| 13 | webhook HMAC + idempotency | PASS | bad HMAC 401; signed 202; replay 200 duplicate |
| 14 | incident → repair PR (no merge) | SKIP | REPAIR_MODEL not set |
| 15 | Claude review advisory-only | SKIP | CLAUDE_REVIEW_ENABLED=false |
| 16 | backup + restore drill | SKIP | backup status file not readable (no backup here; the real backup → restore drill round trip runs in `tests/test_ops_scripts.py`) |

**9 PASS, 0 FAIL, 7 SKIP.** Earlier runs in the same session found two real problems, both
fixed in this phase: the image worker's serve loop died on its first idle 5 s wait under
redis-py ≥ 8 (its `TimeoutError` was not suppressed; the same latent bug was in the research
AI lane and logged a warning every poll in the webhook worker), and `ReviewRunner.drain()`
could spin forever when the last task had finished but its done-callback had not run yet.

**The server run is a manual step** (manual-setup.md "22", item 6): run the script on the box
against `http://127.0.0.1:8000` and paste its table here under "Results (server, <date>)".

## Deploy and validate

```bash
cd /opt/private-ai/aureon && git pull && docker compose build
scripts/ops/backup.sh                                   # fresh backup first
scripts/ops/apply-db-roles.sh --dry-run                 # see what runs
scripts/ops/apply-db-roles.sh                           # prompts for the three passwords
# .env: DATABASE_URL=...aureon_app:<pw>@<pg>:5432/spasta ; .env.migrate: MIGRATIONS_DATABASE_URL=...aureon_migrator...
docker compose up -d                                    # migrate (as the migrator) -> api -> workers
scripts/ops/healthcheck.sh --wait 300
curl -s -b $J $B/api/v1/system/integrations | python3 -m json.tool   # database.scoped: true
scripts/ops/rollback-drill.sh --steps 3                 # scratch DB only
AUREON_ACCEPTANCE_EMAIL=you@… python3 scripts/ops/acceptance.py --base-url http://127.0.0.1:8000
```

## Rollback

- Roles: point `DATABASE_URL` back at the previous role and delete `.env.migrate`; `docker
  compose up -d`. The roles and grants can stay (they only add). Ownership moved to the
  migrator stays valid for a superuser connection.
- Alerts: unset `DISCORD_WEBHOOK_URL` or `ALERTS_CRON=`; pause the job with
  `POST /api/scheduler/jobs/alerts-dispatch/pause`.
- Cloudflare: see deploy/cloudflare/README.md "Rollback"; the app check goes away when the
  two `CF_ACCESS_*` variables are unset.
- Code: `git revert` the merge; no migration to undo.

## Tests

| File | Covers |
| --- | --- |
| `tests/test_db_roles.py` | roles.sql applied (twice) through the wrapper on a scratch DB with unique role names: attributes, ownership, `spasta` empty and commented; the app role cannot CREATE/DROP/ALTER/TRUNCATE/CREATE SCHEMA; audit append-only; CRUD works; readonly only reads; the real app (login, finance CRUD, audit API, logout) runs as the app role; the migrator runs `alembic downgrade/upgrade` via `MIGRATIONS_DATABASE_URL` and the app role is refused; re-applying restores grants. Static: no DROP/DELETE, no password literal, every audit table classified. |
| `tests/test_migrations_rollback.py` | every revision 0010..head downgrades and upgrades again on a scratch DB with an identical schema and a clean smoke test; the drill refuses the configured DB; the shell wrapper (shimmed and end to end) drops only its scratch DB. |
| `tests/test_prod_integration_db.py` | MT5 export → job → status/positions/stream/card, stale detection; the integrations card (sections, auth, no secret, missing models, DB role, backups); alerts collection, dedup, escalation, retry after failure, cap, no-op without Discord, job wiring, the real Discord post (mocked). |
| `tests/test_prod_integration_unit.py` | MT5 has no write path; one heavy model at a time (four mechanisms); cloudflared ingress and nginx block; the redis-py idle timeout fix; the acceptance script shape. |
| `tests/test_cf_access.py` | the middleware with a generated RSA key and a Cloudflare-format JWKS: valid/expired/wrong aud/iss/key/alg/kid, bypass paths and prefix tricks, caching, rotation, outage, off by default, the prod check. |
| `tests/test_compose_hardening.py` | + supervision of the Phase 17/18 workers, caps and budget, `.env.migrate` only on migrate, read-only HDD on the scheduler. |
| `tests/test_ops_scripts.py` | + github/spasta in the dump, the backup status file (ok and failed). |

## Open risks

- **Not run on the server.** Every step above is a script; the box itself (roles, tunnel,
  Access, nginx, alerts, the acceptance run) is a manual step.
- **Legacy public tables keep their owner.** A future migration that alters them must run
  as that owner (not the migrator). 0010 already ran.
- **BYPASSRLS on the app role** keeps D-A7 (Supabase RLS untouched, ownership enforced in
  the service). Dropping it would make the legacy RLS policies apply to Aureon.
- **Public share pages.** `/api/public/*` is bypassed, but the SPA route that renders a
  shared document on `spasta.online` is behind Access; anonymous viewers would need a
  bypass for that SPA path too (not added: owner decision).
- **Cloudflare provider names.** `access.tf.example` uses the provider 4.x resource names;
  5.x renamed them (`cloudflare_zero_trust_access_*`).
- **The image worker idle bug** affected any deployment built with redis-py ≥ 8; after this
  fix, rebuild the images.
