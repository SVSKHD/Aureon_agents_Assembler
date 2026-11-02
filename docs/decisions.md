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

## 15 — GitHub foundation

- **D-15-1: the token comes from env only, and only its fingerprint is stored.**
  `GITHUB_TOKEN` is read by Settings and held only by the client. `github.connections` is
  keyed by sha256(token)[:16], so a rotated token simply gets a new row.
- **D-15-2: redaction lives in the client.** Every message the client builds or logs goes
  through `redact()`, which covers the configured token plus any GitHub-shaped credential.
  Routers return only `GitHubError.message`, never `str()` of a cause. This does not rely
  on a global log processor (another phase adds one; both can coexist).
- **D-15-3: the allowlist semantics.** `GITHUB_ALLOWED_REPOS`, when set, is the exact list
  (narrowed by owners if both are set). Otherwise `GITHUB_ALLOWED_OWNERS`. With neither,
  only the token user's own repos. Everything outside is 404, indistinguishable from
  missing, and checked before any GitHub call. The repo listing re-applies the allowlist at
  read time, so a config change takes effect without a sync.
- **D-15-4: the retry budget.** 3 attempts at most. A 429 or secondary limit is waited out
  inside the request only up to `GITHUB_MAX_RETRY_WAIT_SECONDS` (20 s). Longer waits
  become a 429 with `Retry-After` rather than a hung admin request. The primary limit at 0
  is a rate-limit error, not a 403.
- **D-15-5: the rate-limit floor is local and per process.** Below `GITHUB_RATE_LIMIT_FLOOR`
  remaining requests (default 50), the client refuses until the reset time, leaving headroom
  for the scheduler and later phases. The snapshot is stored on `github.connections` after
  checks and syncs.
- **D-15-6: redirects are followed by hand, same host only.** httpx's automatic redirects
  would forward the Authorization header across hosts. A same-host 301 (a renamed repo) is
  followed, and the new full name is re-checked against the allowlist.
- **D-15-7: repositories are never deleted.** A complete sync sets `removed_at` on unseen
  repos and clears it when they reappear. A capped listing (over `GITHUB_MAX_PAGES`) marks
  nothing.
- **D-15-8: the routes live under `/api/v1/github`.** A `/v1/github` router is mounted under
  the existing `/api` router, as the orchestrator asked. Existing `/api/*` routes are
  unchanged.
- **D-15-9: path safety.** Paths are validated as given and once more URL-decoded: `.`, `..`
  and empty segments, leading `/` or `~`, backslashes and control characters are 400
  `invalid_path`. Each segment is then percent-encoded into the GitHub URL. Refs and shas
  follow git ref rules (no `..`, no `@{`, no leading `-` or `/`).
- **D-15-10: text vs binary.** A file is text when it is valid UTF-8 without NUL bytes and
  no larger than `GITHUB_MAX_FILE_BYTES`. Everything else returns metadata only.
- **D-15-11: the agent heartbeat comes from the sync task.** The `github` agent
  (kind `integration`) is registered by the scheduler's `github_sync` task. Its
  `stale_after_s` is `GITHUB_AGENT_STALE_SECONDS` (1 h, three missed 20-minute runs).
  Pausing the agent makes the task skip.
- **D-15-12: tests never use the host's token.** The shared test settings pin
  `GITHUB_TOKEN=None`, so a token in the developer's environment cannot reach GitHub from a
  test.
- **D-15-13: no ETag requests in this phase.** They are optional in the spec. The in-memory
  TTL cache already absorbs repeated browsing, and syncs run every 20 minutes.
