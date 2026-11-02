# Decisions log

Decisions made autonomously while finishing the roadmap. Each has an ID, the choice, why, and
what would change it. Newest phase last.

## A — Finance (Prompt 3)

- **D-A1: DDL source.** The production Postgres container is not reachable from the build
  sandbox, so it could not be dumped. The authoritative DDL is versioned in ASTRA-FRONTEND:
  `supabase/migrations/20260922_firestore_compat.sql` (astra_documents),
  `20260923_normalise_trading.sql` (trades, expenses, secured), `20261011_portfolio_profile.sql`
  (portfolio_profiles). The migration is written defensively so a live table that differs
  from those files is still safe:
  - only `ADD COLUMN IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`
  - `CREATE TABLE IF NOT EXISTS` only for missing tables

  **Verify on the server:** `pg_dump --schema-only` per docs/00-inspection.md §4, before
  `alembic upgrade 0010`.
- **D-A2: ownership column name.** The legacy tables already have `user_id text` (Firebase /
  Supabase uid, part of the composite PK). The Aureon owner is a new nullable
  `aureon_user_id uuid` column instead. Nothing existing is renamed or retyped.
  - `core.finance_identities` maps an Aureon user to their legacy id.
  - New rows use the legacy id when one is linked, so the old Supabase app still sees them.
- **D-A3: money type.** The legacy money columns are `double precision`. Changing their type is
  not additive, so the API speaks Decimal (exact strings). It converts at the DB boundary using
  the shortest round-trip repr (`0.1` stays `0.1`), and totals are summed as `numeric` in SQL.
  - **Follow-up (needs approval):** a migration to `numeric(20,8)` columns.
- **D-A4: unclaimed rows are invisible.** Rows without `aureon_user_id` belong to nobody until
  an admin runs `POST /api/finance/claim`. It is a dry run by default, audited, and one legacy
  id maps to one user. This is safer than guessing an owner during the migration.
- **D-A5: admins get no extra finance visibility.** Finance is personal data. An admin sees
  their own rows like anyone else; their only extra powers are the claim and the portfolio
  profile edit.
- **D-A6: probing.** Another user's row answers 404, exactly like a missing one, on GET, PATCH,
  DELETE and document PUT.
- **D-A7: Supabase RLS stays.** The policies, the `astra_uid()` defaults and the Realtime
  publication on the live tables are left untouched. Aureon connects as the table owner, so RLS
  does not apply to it, and the scoping is enforced in `app/services/finance.py`.

## D-backend — Frontend support APIs

- **D-Dbe-1: tombstone pruning.** Tombstones are pruned inside the existing
  `purge_agent_history` task, not a new job. The default job set (and its test) stays the
  same. Retention is `FINANCE_TOMBSTONE_RETENTION_DAYS`, default 30.
- **D-Dbe-2: the changes cursor.** The cursor is the server time taken *before* reading, so a
  racing write is resent rather than lost. Lists are capped (`FINANCE_CHANGES_MAX_ROWS`). On a
  cap, `truncated: true` is returned, and the cursor steps back to just before the oldest
  truncated item. Duplicates are harmless; gaps are not. A naive `since` (no UTC offset) is
  422.
- **D-Dbe-3: deleting a row deletes its attachments.** Records go in the same transaction, and
  files are unlinked after commit. A leftover file is harmless; a dangling record is not.
  Tombstones are not cleared when an id is re-created. Clients compare `deleted_at` with
  `updated_at`.
- **D-Dbe-4: the content type comes from magic bytes only.** The stored and served type is the
  sniffed one. The file is served with `Content-Disposition: attachment`, `nosniff` and
  `Cache-Control: private, no-store`. The client filename is cleaned to a display name and
  never touches the filesystem.
- **D-Dbe-5: the per-user cap is 409, not 413.** 409 `attachment_limit` keeps 413 to mean "this
  file is too big".
- **D-Dbe-6: the public rate limit fails open.** It reuses the Redis fixed-window limiter
  (`app/agents/trading/ratelimit.py`), keyed by a hash of the client IP. If Redis is down, the
  public read-only endpoints still answer, with a warning logged, rather than going dark.
- **D-Dbe-7: public forex shows only ownerless rows.** A user can write their own `forex`
  documents through the documents API, and those must not become public. The news job also
  never overwrites a user-owned row with the same id.
- **D-Dbe-8: owner ids are stripped at every depth.** `ownerId` and `userId` are removed from
  public share data recursively, including inside nested objects and lists.
- **D-Dbe-9: defusedxml is optional.** It is not added as a dependency. Any DOCTYPE or ENTITY
  declaration is refused before parsing, which blocks XXE and entity-expansion attacks with
  the stdlib parser. defusedxml is used if it is installed.
- **D-Dbe-10: revoking a session.** Revoking the current session through
  `DELETE /api/auth/sessions/{id}` also clears its cookies. An already revoked or expired own
  session is 404, like an unknown one.
- **D-Dbe-11: new dependency `python-multipart`.** FastAPI needs it for the multipart upload.

## E — Production hardening (Prompt 13)

- **D-E1: migrate runs on every `up`.** The `migrate` one-shot left the `tools` profile. It
  waits for the external Postgres (`MIGRATE_WAIT_SECONDS`) and then runs `alembic upgrade
  head`, and the API starts only after it exits 0. Why: compose cannot order on a container in
  another project, and every migration is additive and idempotent at head, so after a reboot
  or upgrade the schema is right before traffic. To change it, put the profile back (docs/16,
  Rollback).
- **D-E2: the LIVE refusal lives in Settings, not just the API.** `TRADING_LIVE_ENABLED=true`
  without `PHASE_G_APPROVED=yes` fails at settings load in every process and every ENV, so no
  worker can start with it either. The Phase G gate in `services/trading.py` is unchanged:
  LIVE still answers `preconditions_unmet`.
- **D-E3: low-entropy SECRET_KEY counts as weak.** Prod requires ≥ 12 distinct characters on
  top of the existing 32-char minimum, because `"k"*48` passed the old check.
- **D-E4: OpenAPI is off in prod.** `/api/openapi.json` was public. The frontend's typed client
  comes from `scripts/export_openapi.py`, which calls `app.openapi()` directly and is
  unaffected. `API_DOCS_ENABLED` can re-enable docs outside prod only.
- **D-E5: worker health = heartbeat file age.** Workers have no HTTP port. The registry
  heartbeat touches `/tmp/aureon-heartbeat` (tmpfs, per container) after each successful DB
  heartbeat. The healthcheck reads only that file, with no DB or network load. A worker that
  cannot reach the DB turns unhealthy, which is the useful signal.
- **D-E6: two dumps, not one.** `pg_dump` ignores `-n` once a `-t` is present, so the owned
  schemas and the public finance tables are dumped separately. The legacy tables' default
  function (`astra_uid`) is saved via `pg_get_functiondef`, so they restore into a fresh
  database.
- **D-E7: Postgres is reached inside its own container.** The scripts use `docker compose exec
  -T` on the private-ai project (or `docker exec -i` with `PG_CONTAINER`) and the local socket.
  No port, no password, and no secret is read by the scripts.
- **D-E8: retention trusts names, not mtimes.** Deletion is by the UTC stamp in the folder
  name, only for real directories with a `MANIFEST.json`, strictly inside `BACKUP_DIR`, and
  only after a successful backup. A failed run never prunes.
- **D-E9: restore never overwrites.** The default target is a new database. Data files unpack
  into a new `DATA_ROOT/restore-<ts>/`. Overwriting the live DB needs both
  `--into-live --yes-i-am-sure` and runs as one transaction.
- **D-E10: boot = systemd oneshot around compose.** `aureon.service` has
  `RequiresMountsFor=/mnt/spasta-data` and `Requires/After=docker.service`, runs
  `docker compose up -d`, then `healthcheck.sh --wait 300` (report only). It does not use
  `up --wait`, because some compose versions treat exited one-shots as failures.
- **D-E11: Redis runs as uid 999** so every capability can be dropped (the entrypoint otherwise
  needs CHOWN/SETUID to step down). This needs a one-time `chown` of the AOF dir (manual-setup
  step 0).
- **D-E12: isolate whatever runs model-written code.** The coding worker is non-dumpable and
  left the `aureon` network. The Docker proxy, whose container-inspect output includes every
  container's env, sits on an internal network with only the API.
- **D-E13: readiness detail stays on the box.** Nginx answers 404 to `/api/health/ready` from
  outside, and the endpoint itself is unchanged. Changing it would break the existing tests and
  monitoring contract, and the leak is minor (Postgres version, model count).
