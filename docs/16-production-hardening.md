# 16 — Production hardening (Phase E): ops runbook

Phase E makes the stack safe to leave running on the box. The API refuses an unsafe `ENV=prod`
config. Compose starts everything in a fixed, health-gated order, with tight container limits.
Backups run nightly to the HDD and are proven by a weekly restore drill. systemd brings the
stack back after a reboot, but only once Docker is up and the HDD is mounted. No migration in
this phase.

The security review that came with it is [docs/17-security-review.md](17-security-review.md).
Sudo steps (systemd, timers, firewall, log limits, unattended upgrades) are in
[docs/manual-setup.md](manual-setup.md#phase-e--production-hardening).

## What it does

| Area | Change |
| --- | --- |
| Startup checklist | `app/core/prod_checks.py`. When `ENV=prod`, the API refuses to start if any check below fails. |
| LIVE flag | `TRADING_LIVE_ENABLED=true` is rejected at settings load, in every process and every ENV, unless `PHASE_G_APPROVED=yes`. Even then, LIVE stays unreachable: the Phase G gate in `app/services/trading.py` is unchanged. |
| Compose | Every service gets `no-new-privileges`, `cap_drop: [ALL]`, `read_only` + tmpfs, mem/cpu limits and json-file logs capped at 10 MB × 5. Every long-running service has a healthcheck and `restart: unless-stopped`. Only `127.0.0.1:8000` is published. |
| Start order | mount-check → redis (healthy) → **migrate** (waits for Postgres, then `alembic upgrade head`) → api (healthy) → scheduler and workers. |
| Worker health | Every agent heartbeat touches `/tmp/aureon-heartbeat` in its own container. `python -m app.workers.healthcheck --max-age 90` checks how old that file is. |
| Logs | A structlog processor masks keys that look like secrets (`token`, `password`, `secret`, `authorization`, `cookie`, `api_key`, `dsn`…) and credentials inside strings (`user:pass@`, `Bearer …`). It covers both app logs and stdlib logs. |
| Headers | The app sets nosniff, `X-Frame-Options: DENY`, `Referrer-Policy`, `Permissions-Policy`, COOP and `CSP: default-src 'none'; frame-ancestors 'none'` on every response. Nginx adds HSTS and an SPA CSP (`deploy/nginx/security-headers.conf`). uvicorn sends no `server` header. |
| Docs | In prod, Swagger and `/api/openapi.json` are off. `scripts/export_openapi.py` still works because it calls `app.openapi()` directly. |
| Ops scripts | In `scripts/ops/`: `backup.sh`, `restore.sh`, `restore-drill.sh`, `healthcheck.sh`, and the shared `lib.sh`. |
| systemd | In `deploy/systemd/`: `aureon.service` (boot), `aureon-backup.{service,timer}` (nightly) and `aureon-restore-drill.{service,timer}` (weekly). |

## Production checklist (ENV=prod)

The API checks all of these at startup and lists every failing item in one error. The error
never includes a secret.

1. `SECRET_KEY` is ≥ 32 chars, is not the `.env.example` placeholder, and has ≥ 12 distinct
   characters. Generate one with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
2. `DEBUG=false`, and `API_DOCS_ENABLED` is unset or false.
3. `COOKIE_SECURE=true`, which gives the cookies the `__Host-` prefix plus Secure, HttpOnly
   (session) and SameSite=Strict.
4. `CORS_ORIGINS` has no `*`.
5. The HDD is mounted: `DATA_ROOT/DATA_ROOT_MARKER` exists and `DATA_ROOT` is writable.
6. `TRADING_LIVE_ENABLED=false`, or `PHASE_G_APPROVED=yes`.

These are also enforced, but outside the startup check:

- Only the API is published, and only on loopback. `tests/test_compose_hardening.py` asserts it.
- Postgres and Ollama are reached by container name over `PRIVATE_AI_NETWORK`.
- Every non-GET route needs CSRF, and every route except `/api/health`, `/api/health/ready`
  and `POST /api/auth/login` needs a session. `tests/test_route_auth.py` asserts it.

## Endpoints

No new endpoints. Three existing POSTs now **require the CSRF header** (they previously only
needed a session): `/api/trading/strategies/backtest`, `/api/trading/signals/{id}/explain` and
`/api/trading/backtests/{id}/explain`. The SPA already sends `X-CSRF-Token` on every unsafe
request. In prod, `/api/openapi.json` and `/api/docs` return 404. The Nginx snippet now answers
404 to `/api/health/ready` from outside. On-box checks call `127.0.0.1:8000` directly.

## Config

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEBUG` | `false` | Refused in prod. |
| `API_DOCS_ENABLED` | unset | Unset: docs are on outside prod and off in prod. `true` is refused in prod. |
| `PHASE_G_APPROVED` | empty | Must be exactly `yes` for `TRADING_LIVE_ENABLED=true` to load at all. |
| `MIGRATE_WAIT_SECONDS` | `180` | How long the `migrate` one-shot waits for Postgres. |
| `AUREON_HEARTBEAT_FILE` | `/tmp/aureon-heartbeat` | Liveness file read by the worker healthcheck. |

Ops scripts (`scripts/ops/ops.env`, copied from `ops.env.example`, git-ignored, no secrets):

| Variable | Default | Meaning |
| --- | --- | --- |
| `PG_COMPOSE_DIR` / `PG_SERVICE` | `/opt/private-ai` / `postgres` | The existing Postgres compose project, used via `docker compose exec -T`. |
| `PG_CONTAINER` | empty | If set, scripts use `docker exec -i <container>` instead. |
| `PG_USER` / `PG_DB` | `postgres` / `spasta` | Role used *inside* the container (local socket, no password) and the live DB. |
| `DATA_ROOT` / `BACKUP_DIR` | `/mnt/spasta-data` / `$DATA_ROOT/backups/aureon` | `BACKUP_DIR` must be under `DATA_ROOT` (the HDD). |
| `BACKUP_RETENTION_DAYS` | `14` | Backups older than this are deleted after a successful backup. |
| `BACKUP_DATA_DIRS` | `images finance-attachments` | `DATA_ROOT` subdirs to archive. Missing ones are skipped. |
| `BACKUP_PUBLIC_FUNCTIONS` | `astra_uid` | Functions used as defaults by the legacy finance tables. Their definitions are saved so a fresh DB can recreate those tables. |
| `AUREON_DIR` | the checkout | This compose project, for `docker compose run migrate` and `ps`. |

## Deploy and validate

First deploy (and after every `git pull`):

```bash
cd /opt/private-ai/aureon
git pull
docker compose build
docker compose up -d                  # migrate runs before the API (additive, idempotent)
scripts/ops/healthcheck.sh --wait 300 # /api/health, /ready, compose ps health
```

Then install the systemd units and timers once (sudo, see manual-setup.md).

Validate the hardening:

```bash
docker compose ps                                        # every service healthy; one-shots exited 0
ss -tlnp | grep -E ':(5432|6379|11434|2375|8000)\b'      # only 127.0.0.1:8000
docker inspect aureon-api -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.Memory}}'
curl -sI http://127.0.0.1:8000/api/health | grep -iE 'x-frame|content-security|server'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/openapi.json   # 404 in prod
```

Prod guard test (optional): set `COOKIE_SECURE=false` in `.env` and run
`docker compose up -d api`. `docker compose logs api` should show
`refusing to start with ENV=prod: COOKIE_SECURE must be true`. Then revert the change.

**Redis note:** Redis now runs as uid 999 with every capability dropped, so its data dir must be
owned by 999: `sudo chown -R 999:999 /mnt/spasta-data/redis`. That is step 0 in
manual-setup.md. **docker-proxy note:** it now has `cap_drop: [ALL]`. HAProxy runs as root in
that image and binds 2375, so it should need no capabilities. If it does not become healthy,
remove `cap_drop` from that one service and record why.

## Upgrade

```bash
cd /opt/private-ai/aureon
scripts/ops/backup.sh                 # fresh backup before any upgrade
git fetch && git log --oneline HEAD..origin/main
git pull
docker compose build
docker compose up -d                  # migrate -> api -> workers, in order
scripts/ops/healthcheck.sh --wait 300
```

## Backups

`scripts/ops/backup.sh` (run nightly at 02:17 IST by `aureon-backup.timer`) writes
`$BACKUP_DIR/aureon-<UTC stamp>/` containing:

| File | Content |
| --- | --- |
| `db-schemas.dump` | `pg_dump -Fc -n core -n audit -n ai -n agents -n trading -n dhan -n mt5` |
| `db-public.dump` | `pg_dump -Fc -t public.{trades,expenses,secured,astra_documents,portfolio_profiles,aureon_alembic_version}` |
| `public-functions.sql` | `pg_get_functiondef` of `BACKUP_PUBLIC_FUNCTIONS` (the legacy tables' default functions) |
| `row-counts.txt` | Exact `count(*)` of every owned table at dump time |
| `data.tar` | `DATA_ROOT/images` and `DATA_ROOT/finance-attachments` (when present) |
| `MANIFEST.json` | Stamp, DB, alembic revision, schemas, tables, row counts, file sizes and sha256 |
| `SHA256SUMS` | sha256 of every file above |

The two dumps are separate because `pg_dump` ignores `-n` once any `-t` is given.
`pg_dump` runs inside the Postgres container (`docker compose exec -T`), so no port is
published and no password is used or printed. The script writes into `aureon-<stamp>.partial/`
and renames it only on success. A failed run deletes its own partial folder and nothing else.

**Retention:** this runs only after a successful backup. It removes `aureon-<stamp>` folders
whose stamp is older than `BACKUP_RETENTION_DAYS`, but only if each one is a real directory
(not a symlink), contains a `MANIFEST.json`, has a name that matches the stamp pattern exactly,
and resolves strictly inside `BACKUP_DIR`. Nothing else is ever deleted.

Run it by hand with `scripts/ops/backup.sh` (or `--dry-run`). Check the result with
`ls -l $BACKUP_DIR` and `cat $BACKUP_DIR/aureon-*/MANIFEST.json | tail -40`.

## Restore

```bash
scripts/ops/restore.sh latest                                 # into a NEW db spasta_restore_<ts>
scripts/ops/restore.sh aureon-20261103T204700Z --target-db spasta_check
scripts/ops/restore.sh latest --with-data                     # + data.tar -> DATA_ROOT/restore-<ts>/
```

- The script verifies checksums (`sha256sum --strict -c`) before touching anything. A mismatch
  aborts it.
- By default it restores into a **new** database (`CREATE DATABASE … TEMPLATE template0`) and
  refuses one that already exists. It never restores over `spasta` unless given
  **both** `--into-live --yes-i-am-sure`. `--target-db spasta` on its own is refused.
- Non-fatal `pg_restore` errors are counted and reported, not fatal. An example is Supabase
  roles named in RLS policies that this server does not have. The drill checks row counts.
- `--with-data` unpacks into a new `DATA_ROOT/restore-<ts>/`, never over the live folders.
  Move the files into place by hand.

**Disaster recovery onto the live DB** (owner only, after a fresh backup):

```bash
sudo systemctl stop aureon        # or: docker compose stop
scripts/ops/restore.sh aureon-<stamp> --into-live --yes-i-am-sure
# --clean --if-exists --single-transaction --exit-on-error: all or nothing
sudo systemctl start aureon && scripts/ops/healthcheck.sh --wait 300
```

To point the API at a restored copy instead, set `DATABASE_URL`'s database name to
`spasta_restore_<ts>` and run `docker compose up -d`. That needs no data movement.

## Restore drill

`scripts/ops/restore-drill.sh [backup]` runs weekly (Sunday 04:23 IST) from
`aureon-restore-drill.timer`. It:

1. restores the backup into `spasta_drill_<ts>`. If that name already exists, it stops without
   touching it;
2. runs `alembic current` against it inside the migrate image
   (`python -m app.workers.migrate --current-for <db>`, so the DSN never leaves the container);
3. compares exact row counts with the manifest;
4. writes `$BACKUP_DIR/drills/drill-<ts>.json` and prints it;
5. drops **only** the scratch database it created, on success or failure.

It exits 0 only when the revision matches and every count matches. A failed drill shows as a
failed unit: `systemctl --failed`, `journalctl -u aureon-restore-drill`.

## Reboot order

```
local-fs + /mnt/spasta-data mounted   (RequiresMountsFor)
docker.service                         (Requires/After)
  existing private-ai stack (Postgres, Ollama) — their own restart policies
aureon.service: docker compose up -d
  mount-check  — marker on the HDD, else everything below stays down
  redis        — healthy (redis-cli ping)
  migrate      — waits ≤ MIGRATE_WAIT_SECONDS for Postgres, alembic upgrade head, exit 0
  api          — healthy (GET /api/health)
  scheduler, docker-proxy, opt-in workers (COMPOSE_PROFILES) — healthy (heartbeat file)
ExecStartPost: scripts/ops/healthcheck.sh --wait 300  (logged to the journal)
```

If the HDD is missing, systemd does not start `aureon.service` at all. Running compose by hand
still stops at `mount-check`. After a reboot, check with
`systemctl status aureon` and `journalctl -u aureon -b`.

## Alerts and monitoring

- `GET /api/system/status` (admin) reports Postgres, Redis, Ollama, Docker containers (via the
  read-only proxy), host units (`deploy/host-status`), agents and their staleness. Poll it from
  the frontend's status page.
- Agent logs: `GET /api/agents/{id}/logs` (admin), and the `agents.agent_logs` table. A failing
  scheduled job shows `consecutive_failures` in `GET /api/scheduler/jobs`.
- Container health: `scripts/ops/healthcheck.sh` (exit 1 on any problem) and
  `docker compose ps`.
- Backups: `systemctl list-timers 'aureon-*'`, `systemctl --failed`, and the drill report JSON.
  Run `journalctl -u aureon-backup -u aureon-restore-drill --since -7d` for detail.

## Rollback

- Code: `git checkout <previous tag/commit> && docker compose build && docker compose up -d`.
  There is no migration in this phase, so no schema rollback is needed.
- Compose hardening, one service at a time: if a service fails under `read_only` or
  `cap_drop`, remove that key from that service only, `docker compose up -d <svc>`, and
  record why in docs/decisions.md.
- Auto-migrate: to go back to manual migrations, give `migrate` `profiles: [tools]` again and
  remove `migrate` from the `depends_on` of `api` and the `x-after-api` anchor.
- systemd: `sudo systemctl disable --now aureon aureon-backup.timer aureon-restore-drill.timer`.
  Backups already taken stay on the HDD.

## Tests

| File | Covers |
| --- | --- |
| `tests/test_prod_hardening.py` | Each checklist item refused; the API lifespan refuses to start; the LIVE flag needs `PHASE_G_APPROVED=yes` while LIVE stays `preconditions_unmet`; redaction (structlog + stdlib); security headers; the worker healthcheck and heartbeat file; DSN database swap |
| `tests/test_route_auth.py` | Walks every route: auth except a 3-route allowlist, CSRF on every unsafe method, a pinned admin set, no docs/openapi in prod |
| `tests/test_compose_hardening.py` | Ports (loopback API only, no `0.0.0.0`), restart, healthchecks, read_only/caps/no-new-privileges/limits/logging, start order, docker.sock only on the proxy, `docker compose config` |
| `tests/test_ops_scripts.py` | Fake `docker` on PATH: exact pg_dump argv (compose exec -T, schemas, tables, no port), manifest and checksums, HDD and BACKUP_DIR guards, retention path guard (old, unmanifested, odd name, symlink, outside), refusals (`--into-live` without `--yes-i-am-sure`, `--target-db spasta`, tampered checksums, existing target, bad names), drill drops only its own DB, healthcheck, shellcheck. **db:** a real backup → drill round trip against the test DB |

## Open risks

- The Postgres compose project and service names are still unverified (docs/00-inspection.md
  §1). Set `PG_SERVICE`/`PG_COMPOSE_DIR` or `PG_CONTAINER` in `scripts/ops/ops.env` after the
  inspection.
- `redis` running as uid 999 needs the existing AOF directory chowned once. `docker-proxy` with
  `cap_drop: [ALL]` has not been tested against the real image.
- Backups live on the same HDD as the data. An off-box copy (rsync to another machine or an
  encrypted cloud bucket) is a recommended follow-up. It needs a destination from the owner.
- The SPA CSP in `security-headers.conf` assumes the bundle is self-hosted. Verify it in the
  browser console, using Report-Only first.
