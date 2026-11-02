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
- **D-15-14: the global log processor also masks GitHub token shapes.** After merging Phase E,
  `app/core/logging.py`'s `redact_secrets` also masks bare `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_`/
  `github_pat_` strings in free text. This is a second layer under the client's own
  redaction (D-15-2), for tokens a third-party library might log.
- **D-15-15: a prod guard for the API URL.** With `GITHUB_TOKEN` set, `ENV=prod` refuses to start
  unless `GITHUB_API_URL` is https, so the token is never sent in clear text.

## 16 — GitHub issues and PRs

- **D-16-1: `task_ref` is an opaque string.** Spasta tasks live in the frontend workspace
  document (`public.astra_documents`, namespace `aureon-notes`), not in a backend table.
  `github.issue_links.task_ref` is the id the frontend supplies (1–200 characters of
  `[A-Za-z0-9_.:/-]`), with `task_kind` defaulting to `spasta_task`. The backend checks that the
  GitHub issue exists and stores its `node_id`. It cannot check that the task exists. Repo
  names are stored lower-case so a link is unique regardless of case.
- **D-16-2: a minimal ApprovalGate now, the same interface in Phase 20.** The engine is
  `app/services/approvals.py`:
  - `request(kind, target, payload, artifact)`, `approve(id, admin, reauth_password)` and
    `deny(id)`
  - an executor registry mapping kind → `ExecutorSpec(execute, current_artifact, on_status,
    requires)`
  - tables in the `audit` schema

  Phase 20 adds policies, artifacts tables and notifications without changing the callers.
- **D-16-3: self-approval is allowed only for the only admin.** `APPROVALS_ALLOW_SELF_APPROVAL`
  defaults to true because there is a single owner. Even when it is true, the requester can
  approve their own request only while they are the only active admin. Once a second admin
  exists, someone else must approve. Setting it to false always requires another admin.
- **D-16-4: re-auth is the admin's own password, checked server-side.** It is verified with
  the existing Argon2 hasher in a worker thread, never stored or logged. A wrong password is
  403 `reauth_failed`, written as an `approval_events` row and an `approval_reauth_failed`
  audit event. After `APPROVALS_MAX_REAUTH_FAILURES` (5) failures, the request is denied, so
  a stolen session cannot guess the password against it indefinitely.
- **D-16-5: single use is enforced before execution.** Under a row lock, approve moves the
  request out of `pending` to `approved` and commits before anything runs. Any later
  approve is 409 `approval_already_used`, even after a crash or a failure. A failed
  execution needs a new request.
- **D-16-6: the artifact is recomputed by the same function.** The artifact is canonical JSON
  (sorted keys), hashed with sha256. The function that builds it at request time runs
  again before execution, and any difference makes the request `invalidated` (409, no
  write). For a merge, the artifact is the head sha + the sha256 of the unified diff (+ base
  ref and state). The merge call also sends the approved `sha`, so GitHub itself refuses a
  head that moved in between.
- **D-16-7: the allowlist is re-checked at execution.** Removing a repo from the allowlist
  between request and approval makes the execution fail (`not_found`) with no write.
- **D-16-8: approve answers 200 for `executed` and `failed` alike.** The status and `result`
  say which. 409 is kept for the gate's own refusals (expired, used, invalidated), and 403
  for re-auth and self-approval.
- **D-16-9: a missing token at approval time does not consume the request.** The executor's
  `requires` dependencies are checked before the status changes (503, still pending).
- **D-16-10: POSTs are never retried after a 5xx or a network error.** GitHub may have acted
  already (a duplicate issue or comment). PUT, PATCH and DELETE set state and keep Phase 15's
  retry. Any write clears the GET cache.
- **D-16-11: the diff is streamed and capped.** At most `GITHUB_DIFF_MAX_BYTES` + 1 bytes are
  read, so a huge diff never sits whole in memory. GitHub's own "too large" (406) is
  answered as an empty `truncated` diff with `too_large_upstream`.
- **D-16-12: force-push is a forced ref update.** `PATCH git/refs/heads/{branch}` with
  `force: true` to a sha that must exist in the repo (checked at request time). Its artifact
  is the branch's current sha + the target sha.
- **D-16-13: route shapes.** Branch names can contain `/`, so protection lives at
  `…/branch-protection/{branch}` and branch delete at `…/branches/{branch}` (both
  `{branch:path}`). Force-push takes the branch in the body. Actions enable/disable is
  `…/actions-permissions`, so it does not collide with Phase 17's `/actions/*` routes.
- **D-16-14: bodies are redacted on the way out, the approval payload is exact.**
  - Issue, PR and comment bodies, commit messages, patches and diffs pass through the
    client's `redact()`.
  - `tracked_operations.request_payload` is redacted and capped at 4,000 characters per
    string.
  - `approval_requests.payload` keeps the exact payload, because that is what gets
    executed. It is admin-only.
- **D-16-15: issue lists leave out PRs.** GitHub's issues endpoint returns PRs too. They are
  filtered out unless `include_pulls=true`. Lists return one GitHub page with `has_next`
  from the Link header.

## 17 — GitHub Actions and webhooks

- **D-17-1: logs are fetched without the token, from named hosts only.** The API client keeps
  refusing off-host redirects (D-15-6); for logs it returns the Location instead, and a
  separate HTTP client with no Authorization header fetches it. `GITHUB_LOG_HOSTS` matches
  label by label (`*` is one DNS label), https on 443 only, no URL credentials, every hop
  re-checked. The pre-signed URL is never logged.
- **D-17-2: sanitise in a fixed order: strip ANSI → tail cut → redact → cap.** Stripping first
  stops an escape code hiding a token; cutting at a line start before redacting means the
  cut never splits a token, and redacting only the kept tail bounds the regex work. Tail
  first because failures are at the end.
- **D-17-3: "production" is the explicit list plus a keyword rule.** A workflow whose name,
  file or file stem is in `GITHUB_PROD_WORKFLOWS`, or whose name or file contains `deploy`
  or `prod`, is gated. The keyword rule cannot be switched off: a forgotten list entry must
  fail safe (gated), not open.
- **D-17-4: one allowlist for dispatch and re-run; cancel needs only the repo allowlist.**
  Re-running a deploy run is a deploy, so re-runs use the same allowlist and the same gate
  (kind `github.workflow_dispatch_prod`, payload `action` = dispatch | rerun |
  rerun_failed). Cancel only stops work.
- **D-17-5: a non-numeric workflow is refused before any GitHub call.** A numeric id is
  resolved first (one GET) and then checked against the allowlist by its file; nothing is
  written either way.
- **D-17-6: the dispatch artifact binds the commit.** It includes the sha the ref points at
  (and the inputs), so a push between request and approval invalidates the approval: the
  approver approves deploying a specific commit. The re-run artifact binds the head sha and
  `run_attempt`.
- **D-17-7: `file_gated` takes `op` and `target`.** Kinds registered outside
  `github_ops` (this one) pass their own tracked-operation name and target; the Phase 16
  kinds are unchanged. The executor reuses `github_ops._on_status`.
- **D-17-8: the receiver answers before any processing.** Order: 503 (no secret) → 415 → 413
  → 401 → 400 → insert. Invalid signatures store nothing, only a Redis hash counter per
  reason and a log line. Only `sha256=` signatures are accepted.
- **D-17-9: idempotency is the unique `delivery_id`.** `INSERT … ON CONFLICT DO NOTHING`;
  a conflict is 200 `duplicate`, whatever the earlier row's status. GitHub's "Redeliver"
  keeps the delivery id, so a manual redelivery is also a no-op: to reprocess a failed
  event, set its status back to `pending` in SQL.
- **D-17-10: off-allowlist repos and unhandled events are stored as `ignored`.** They are kept
  for the audit trail (trimmed, redacted) but never queued or processed. With no allowlist
  set, the receiver uses the login of the last successful connection check, never a live
  GitHub call.
- **D-17-11: Redis is a wake-up, the DB is the queue.** The list carries delivery ids only
  and is trimmed to 10,000; the worker also scans due pending rows with `FOR UPDATE SKIP
  LOCKED`, so retries, Redis restarts and enqueue failures lose nothing, and several
  workers never process a row twice.
- **D-17-12: handlers run in a savepoint.** A failure rolls back the handler's partial writes
  while the row stays locked, then `attempts`, `error` (redacted) and `next_attempt_at`
  (base·2^(n-1), ≤ 1 h) are set with a core UPDATE. After `GITHUB_WEBHOOK_MAX_ATTEMPTS` the
  event is `failed` (poison) and never picked up again.
- **D-17-13: out-of-order events never regress a row.** A run upsert applies only when the
  incoming `updated_at` is not older; a job upsert never moves a completed job back to a
  non-completed status of the same attempt. Jobs have no FK to runs.
- **D-17-14: `on_workflow_failed` callbacks fire after commit, once per delivery,
  best-effort.** failure / timed_out / startup_failure conclusions of `completed` runs.
  A raising callback is logged, never retried, and never fails the event. `WorkflowFailure`
  carries branch, event and head sha for Phase 18's recursion guard.
- **D-17-15: push / pull_request / issues are recorded, not fetched.** The API's GET cache
  lives in another process, so there is nothing to drop; `push` updates
  `github.repositories.pushed_at`.
- **D-17-16: the Actions sync reads the pause flag only.** `github_actions_sync` checks the
  `github` agent's `paused`, but does not register or heartbeat it, so it never overwrites
  `github_sync`'s connection details.
- **D-17-17: the worker is its own agent and compose service.** `github-webhooks` (kind
  integration), same image as the scheduler, profile `github`, 0.5 CPU / 256 MB, the shared
  hardening, the heartbeat-file healthcheck, Redis + Postgres networks only, no ports.

## 18 — Self-repair

- **D-18-1: the repair loop lives in the coding worker.** It reuses the coding agent's
  worktree sandbox, patch validator, path checks and argv allowlist, and shares one
  process-wide `HEAVY_MODEL_LOCK` (`app/agents/ollama/heavy.py`) with the coding loop. Each
  loop holds it for a whole task (model call and tests), so code and repair never run a
  model or a test suite concurrently. There was no existing Ollama lock in the coding worker;
  this one is new.
- **D-18-2: repair branches reach GitHub through the REST API, never `git push`.** Blobs →
  tree → commit → `refs/heads/repair/<id12>` → PR. The agent's git keeps
  `protocol.allow=never` and no credential ever exists in a worktree. The local base sha is
  the commit's parent, so the base must already be on GitHub.
- **D-18-3: `REPAIR_MODEL` defaults to empty (disabled).** The spec asks for a default model
  and "unset = disabled". The code default is empty (disabled); `.env.example` suggests the
  Qwen coder model to set. `REPAIR_REPO`, `REPAIR_GITHUB_REPO` and `GITHUB_TOKEN` must also
  be set. Missing any of them: tasks are created `queued` with the reason and never claimed.
- **D-18-4: exceptions are captured in memory, written in the background.** The structlog
  processor sits in both the structlog and the stdlib chains, plus a chained
  `sys.excepthook`. No I/O happens inside a log call. Flushers run in the API, the coding
  worker and the webhook worker; the scheduler flushes inside `incident_scan`. Fingerprint =
  type + innermost `app` frame (module:function). Rate limit: one write per fingerprint per
  `INCIDENT_EXCEPTION_RATE_SECONDS`, at most 200 fingerprints per process.
- **D-18-5: stdlib tracebacks are now rendered before redaction.** While wiring the processor
  we found that stdlib records with `exc_info` (uvicorn's "Exception in ASGI application")
  were serialised as a raw tuple, including the exception's repr, so a token in the message
  bypassed `redact_secrets`. `format_exc_info` now runs in the stdlib chain before
  redaction.
- **D-18-6: Docker health is probed by the API.** Only the API is on the docker-proxy's
  internal network, so the API runs a small loop (`INCIDENT_DOCKER_PROBE_SECONDS`). The
  scheduler's scan uses the pluggable probe, which defaults to "unknown".
- **D-18-7: container restarts are `manual`.** The shipped proxy has `POST: "0"`. The
  `restart_container` action exists, but it only runs when
  `INCIDENT_DOCKER_PROXY_ALLOWS_RESTART=true` and the container is in
  `INCIDENT_RESTART_CONTAINERS`. We did not widen the proxy.
- **D-18-8: "restart a worker loop" = pause then resume through the registry.** Trading and
  broker agents are excluded and become `manual`. The Redis lock map has one entry
  (`imaging`), the only worker lock that exists.
- **D-18-9: the Actions rerun runs once per run id, with the system as actor.**
  `github_ops.Actor.user_id` is now optional (`None` = the system). The column was already
  nullable.
- **D-18-10: escalation is one-shot.** An escalated incident (`details._escalated`) is
  never processed again automatically. A finished repair other than `pr_opened` hands the
  incident back to `open`, still escalated, so nothing loops. Only exception incidents map
  to a code location; other sources escalate to `_manual`.
- **D-18-11: the hard blocks are wider than the spec.** They also cover `app/services/approvals*`,
  `app/core/prod_checks*`, the repair system itself (`repair*`, `incidents*`, the exception
  sink) and the sandbox (`app/agents/coding/**`), so a repair can never weaken its own
  guards. `*secret*` and `*credential*` file names are blocked too. Settings changes are
  allowed, except lines in `app/core/config.py` that name a secret field.
- **D-18-12: tests are required.** A repo with no allowlisted test command can never get a
  repair PR (`tests_failed`, "no allowlisted test commands").
- **D-18-13: the circuit breaker pauses the `repair` agent row.** It upserts the row if the
  worker never registered, is audited once per opening, and stays open until an admin
  resumes the agent. Only `failed` and `tests_failed` count; `blocked` and `cancelled` do
  not.
- **D-18-14: the merge kind is `github.pr.merge`.** The spec writes `github.pr_merge`; the
  kind Phase 16 registered is `github.pr.merge`, and repair PRs use it unchanged.
- **D-18-15: the incident-scan job is a default job** (`INCIDENT_SCAN_CRON`, every minute;
  empty removes it). The existing default-jobs test now expects it and pauses it, so it
  stays out of that test's ticks.
