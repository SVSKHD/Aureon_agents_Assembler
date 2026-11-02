# 21 — Incidents and self-repair

Phase 18 of the roadmap. It detects incidents, tries **safe recovery actions first**, and
only then lets a local Qwen coder model (through the local Ollama, never an external API)
propose a fix. The fix is made in a sandboxed worktree, must pass the repo's allowlisted
tests, and is offered as a pull request. **Nothing is ever merged automatically:** merging
a repair PR is the Phase 16 approval-gated merge.

It builds on:

- the coding agent's sandbox ([05-agents.md, "Coding agent"](05-agents.md#coding-agent))
- the agent registry and scheduler
- the read-only docker-proxy ([16-production-hardening.md](16-production-hardening.md))
- the Phase 15–17 GitHub client, tracked writes, the ApprovalGate, the Actions rerun and the
  `on_workflow_failed` hook ([18](18-github-foundation.md), [19](19-github-issues-prs.md),
  [20](20-github-actions-webhooks.md))

```
 exception / heartbeat / docker / actions / db / api
                 │  (fingerprint, aggregate)
                 ▼
          agents.incidents ──► safe recovery (≤ 2 attempts, audited)
                 │                     │ fixed → resolved by the next probe
                 ▼ escalate            ▼ not fixed / no action
        maps to code? ──no──► manual (marked, stays open)
                 │yes (and not the repair system's own incident)
                 ▼
        agents.repair_tasks (guards: hard blocks, recursion, limits, breaker)
                 ▼  coding worker, one at a time, heavy-model lock held
   worktree repair/<id> → local model diff → hard blocks again → git apply → tests
                 ▼ all tests pass                     ▼ fail
   REST: blobs → tree → commit → refs/heads/repair/<id> → PR      tests_failed, nothing pushed
                 ▼
           pr_opened ──► (human) POST …/pulls/{n}/merge → ApprovalGate github.pr.merge
```

## What it does

### Incident detection (`app/services/incidents.py`, `agents.incidents`)

Every incident has a **fingerprint**. While an incident with that fingerprint is open
(`open`, `recovering` or `repair_queued`, enforced by a partial unique index), repeats only
bump `count` and `last_seen`. `details` always goes through the log scrubber (secret-looking
keys masked, tokens and DSN passwords removed).

| Source | How | Fingerprint | Auto-resolves |
|---|---|---|---|
| `exception` | A structlog processor in both the structlog and the stdlib chain (so uvicorn's "Exception in ASGI application" and asyncio's "Task exception was never retrieved" are seen), plus a chained `sys.excepthook`, hand each logged exception to an **in-memory sink**. No I/O happens inside a log call. A background flusher in the API, the coding worker and the webhook worker (the scheduler flushes during `incident_scan`) writes due entries. | exception type + innermost frame in the `app` package (`module:function`) | no (an admin resolves) |
| `heartbeat` | `incident_scan`: an agent that is not paused and not cleanly `stopped`, with a heartbeat older than **3× its interval** (`details.heartbeat_interval_s`, else `AGENT_HEARTBEAT_SECONDS`) or its own `stale_after_s`, whichever is longer | agent id | yes, once fresh |
| `docker` | the API (the only service that can reach the docker-proxy) polls `GET /containers/json` every `INCIDENT_DOCKER_PROBE_SECONDS`. No proxy = "unknown", no incident. The probe is pluggable (`DockerProbe`). | container name | yes, once running/healthy |
| `actions` | `on_workflow_failed` (subscribed in the webhook worker), plus `incident_scan` reading failed runs from `github.workflow_runs` (fills gaps). The same run is counted once. | repo + workflow file + branch | no |
| `db` | `incident_scan`: `SELECT 1` failing (critical) or slower than `INCIDENT_DB_SLOW_MS` (warning) | constant | yes |
| `api` | `incident_scan`: `INCIDENT_API_READY_URL` (the API's `/api/health/ready`) not answering 200; the failing checks are kept | constant | yes |

Exception capture is **rate-limited**: one write per fingerprint per
`INCIDENT_EXCEPTION_RATE_SECONDS` (repeats in between are counted, not written), and at most
200 distinct fingerprints are held per process. Locals are never read. A failed flush puts
the occurrences back; it logs without `exc_info`, so it can never feed itself.

The scheduler task `incident_scan` runs every minute (`INCIDENT_SCAN_CRON`; empty = no job).
Each probe runs even when another fails, then recovery runs, and a failing probe fails the
run afterwards (normal scheduler retry/backoff).

### Safe recovery first (`app/services/repair_recovery.py`)

An allowlist of in-code actions. Nothing runs a shell or a command line.

| Incident | Action | What it does |
|---|---|---|
| heartbeat | `restart_agent` | Pause, then resume, the agent through the registry. The in-process loop picks the flag up on its next heartbeat. Trading/broker agents (`kind` trading, ids `dhan*`, `mt5*`, `trading*`, `paper*`, `risk*`) are never touched: `manual`. An admin-paused agent is skipped. |
| heartbeat | `clear_queue_lock` | Delete the Redis worker lock the stale agent owns (a fixed map: `imaging` → `aureon:imaging:worker-lock`), so a restarted worker can take over. |
| actions | `rerun_actions` | Re-run the run's failed jobs **once per run id**, through the Phase 17 rerun: the repo allowlist, `GITHUB_DISPATCH_ALLOWLIST` and production gating apply (a production workflow files an approval instead). Tracked in `github.tracked_operations` with the system as actor. |
| docker | `restart_container` | Only when `INCIDENT_DOCKER_PROXY_ALLOWS_RESTART=true` **and** the container is in `INCIDENT_RESTART_CONTAINERS`: `POST /containers/{name}/restart` through the proxy. The shipped proxy has `POST: "0"`, so this records **`manual`**. |

Rules:

- Every attempt is audited (`incident_recovery_attempt`) and kept in `details._recovery`.
- Each action is idempotent.
- An incident gets at most `INCIDENT_MAX_RECOVERY_ATTEMPTS` (2) automatic attempts, at
  least `INCIDENT_RECOVERY_COOLDOWN_SECONDS` apart. The row is locked with `SKIP LOCKED`, so
  two schedulers never act on one incident.
- A `done` action moves the incident to `recovering`. If the probe is healthy on a later
  scan, the incident is resolved.
- After the attempts, or straight away when no action applies (`exception`, `db`, `api`) or
  every action needs a person, the incident **escalates** (`incident_escalated`, once):
  - if it maps to a code location (an exception's innermost app frame) and is not the
    repair system's own incident, a repair task is created (all the guards below apply);
  - otherwise it is marked `_manual` and stays open for an admin.

### Repair tasks (`app/services/repairs.py`, `agents.repair_tasks`)

Statuses: `queued`, `running`, `patch_ready`, `tests_passed`, `tests_failed`, `pr_opened`,
`blocked`, `failed`, `cancelled`.

The repair loop runs **inside the coding worker** (agent `repair`, its own heartbeat) and
reuses the coding agent's machinery:

1. **Claim.** The oldest `queued` task, only when repairs are enabled, the `repair` agent is
   not paused, the circuit breaker is closed and no other repair is mid-flight (**one at a
   time**; the queue is bounded by `REPAIR_QUEUE_MAX`).
2. **Hard-block check #1** on the target files, **before** the model is called.
3. **Worktree** at `CODE_WORKTREE_ROOT/repair-<id>` on a local `repair/<id12>` branch from
   the repo's base branch (git hooks disabled, `protocol.allow=never`). The main checkout is
   never modified.
4. **Model.** The local Ollama `REPAIR_MODEL` sees the incident (title, type, redacted
   message, app frames) and the target files, and must answer with one unified diff. The
   prompt's sha256 is stored as `prompt_digest`; the prompt itself is not stored.
5. **Patch checks:**
   - the coding agent's validator: no absolute paths, no `..`, `.git`, `.env*`, keys, binaries,
     symlinks, submodules or executable modes; size ≤ `CODE_MAX_PATCH_BYTES`
   - **hard-block check #2** on every file the patch touches; settings secret fields changed
     in `app/core/config.py`; added credential-looking strings
   - every path re-checked inside the worktree with symlinks resolved
   - then `git apply --check`, apply, and commit exactly those files → `patch_ready`
6. **Tests.** The repo's allowlisted test commands (`CODE_REPOS[...].tests`, each in
   `CODE_ALLOWED_COMMANDS`) run in the worktree with a scrubbed environment. No configured
   test, or any failure → `tests_failed`; the worktree and branch are deleted and **nothing
   is pushed**. The output is redacted and capped (`REPAIR_TEST_OUTPUT_MAX_CHARS`).
7. **Push and PR (REST only).** The changed files go up through the GitHub API: blobs →
   tree (on the base commit's tree) → commit (parent = the local base sha) →
   `refs/heads/repair/<id12>` → `POST /pulls` (head = that branch, base = the repo's base).
   No git credentials ever exist in the worktree. The ref and PR writes are
   tracked operations (`repair.branch_create`, `pr.create`) → `pr_opened`
   (`repair_pr_opened` audit). The local worktree and branch are then removed.

A repair holds the coding worker's **heavy-model lock** (`app/agents/ollama/heavy.py`) for
the whole task, and so does every coding task, so code and repair never run a model, or a
test suite, at the same time.

When a repair ends in anything but `pr_opened`, its incident goes back to `open`, still
marked escalated, so nothing loops automatically. A `pr_opened` incident stays
`repair_queued` until an admin resolves it after the merge.

**Repairs disabled.** When `REPAIR_MODEL` is unset or empty (or `REPAIR_REPO` /
`REPAIR_GITHUB_REPO` / `GITHUB_TOKEN` is missing), detection and recovery still run. Repair
tasks are created `queued` with the reason in `blocked_reason` and are never claimed;
`GET /api/v1/repairs/status` says why. Once configured, the queued tasks run.

### Hard blocks

A repair whose target or patch touches any of these is `blocked`, with the reason, and
nothing is pushed:

| Area | Paths (case-insensitive) |
|---|---|
| Trading / risk | `app/agents/trading/**`, `app/services/trading*.py`, `app/services/paper*.py`, `app/services/research*.py`, anything under `app/` with `risk` in its path |
| Auth / security | `app/auth/**`, `app/services/auth*`, `app/core/security*`, `app/core/prod_checks*`, `app/services/approvals*` |
| Secrets | base names `.env*` (not `.env.example`), `*.pem`, `*key*`, `*secret*`, `*credential*`; secret fields in `app/core/config.py`; added token / private-key strings |
| Infra | `docker-compose*`, `compose*.y*ml`, `Dockerfile*`, `*.dockerfile`, `deploy/**`, `scripts/ops/**`, `alembic/**`, `alembic.ini`, `.github/workflows/**` |
| The repair system | `app/services/repair*`, `app/services/incidents*`, `app/core/exception_sink*`, `app/agents/coding/**` |

The check runs twice: on the targets when the task is created and again before the model is
called, and on the patch's files after.

### Recursion guard and limits

- **Self-origin.** An exception whose stack has a frame in `app.services.repair*` or
  `app.services.incidents*`, and an Actions failure on a `repair/*` branch (a repair PR's
  own CI), is marked `self_origin` and never gets a repair task (automatic or manual: 409
  `recursion_guard`).
- **Depth ≤ 1.** A retry creates a new task with `parent_repair_id`. A retry of a retry is
  refused.
- **Per fingerprint.** At most `REPAIR_MAX_PER_FINGERPRINT_PER_DAY` repairs per incident
  fingerprint per rolling day (429 `repair_daily_limit`). One active repair per incident
  (409 `repair_active`).
- **Circuit breaker.** More than `REPAIR_BREAKER_MAX_FAILURES_PER_HOUR` repairs ending
  `failed` / `tests_failed` within an hour pauses the `repair` agent (audit
  `repair_circuit_open`, an agent log). An admin resumes it with
  `POST /api/agents/repair/resume`.

### Deploying a repair = merging its PR (approval-gated)

The repair system stops at `pr_opened`. To deploy a repair, an admin reviews the PR and
calls the existing Phase 16 merge:
`POST /api/v1/github/repositories/{owner}/{repo}/pulls/{n}/merge` with the expected head
sha. That files an ApprovalGate request of kind **`github.pr.merge`** (the spec calls it
`github.pr_merge`; the registered kind name is `github.pr.merge`). It runs only after
`POST /api/v1/approvals/{id}/approve` with the admin password re-entered, and only if the
PR head and diff are unchanged. Branch protection on the base branch still applies.
Phase 20 grows the gate into the full engine with the same interface.

## Endpoints

Admin only (`AdminRequired`); POSTs need CSRF; every write is audited.

| Method | Path | Result |
|---|---|---|
| GET | `/api/v1/repairs/status` | `enabled`, the reason when not, `paused` (breaker or admin), model, repo |
| GET | `/api/v1/repairs/incidents?status=&source=&limit=&offset=` | Incidents, newest `last_seen` first |
| GET | `/api/v1/repairs/incidents/{id}` | Incident with redacted `details` (recovery history, escalation) and its repairs |
| POST | `/api/v1/repairs/incidents/{id}/resolve` · `/ignore` | Close it (409 if already closed); `incident_resolved` / `incident_ignored` |
| GET | `/api/v1/repairs?status=&incident_id=&limit=&offset=` | Repair tasks |
| GET | `/api/v1/repairs/{id}` | Repair with the patch and the (capped) test output |
| POST | `/api/v1/repairs` `{incident_id, paths?}` | Manual repair from an incident (`paths` overrides the code location). 201; 409/422/429 on a guard |
| POST | `/api/v1/repairs/{id}/cancel` | Queued/blocked: cancelled at once. Mid-flight: the worker stops before the next step and cleans up |
| POST | `/api/v1/repairs/{id}/retry` | A new task with `parent_repair_id` (from failed / tests_failed / blocked / cancelled) |

## Tables (migration 0015, additive)

- `agents.incidents`: id, fingerprint (unique among open incidents), source, severity,
  title, details jsonb (redacted), first_seen, last_seen, count, status, resolved_at,
  recovery_attempts, last_recovery_at.
- `agents.repair_tasks`: id, incident_id, status, repo, github_repo, target_paths, branch,
  worktree, base_sha, model, prompt_digest, patch, files, test_output (capped), pr_url,
  pr_number, blocked_reason, error, attempts, parent_repair_id, created_by, created_at,
  started_at, finished_at.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `REPAIR_MODEL` | empty | Local Ollama model for repairs (`.env.example` suggests `qwen2.5-coder:7b`). Empty = repairs disabled. |
| `REPAIR_REPO` / `REPAIR_GITHUB_REPO` | empty | The `CODE_REPOS` entry to repair, and its `owner/repo` (must be in the GitHub allowlist). |
| `REPAIR_MODEL_TIMEOUT_SECONDS` | 900 | Model call limit. |
| `REPAIR_POLL_SECONDS` | 10 | Repair loop poll interval. |
| `REPAIR_QUEUE_MAX` | 5 | Queued + running repairs at most. |
| `REPAIR_MAX_PER_FINGERPRINT_PER_DAY` | 2 | Repairs per incident fingerprint per day. |
| `REPAIR_BREAKER_MAX_FAILURES_PER_HOUR` | 3 | More failures than this in an hour pauses the agent. |
| `REPAIR_TEST_OUTPUT_MAX_CHARS` | 32000 | Stored test output cap. |
| `INCIDENT_MAX_RECOVERY_ATTEMPTS` | 2 | Automatic recovery attempts before escalation. |
| `INCIDENT_RECOVERY_COOLDOWN_SECONDS` | 120 | Gap between attempts. |
| `INCIDENT_EXCEPTION_RATE_SECONDS` | 60 | One write per exception fingerprint per this. |
| `INCIDENT_SCAN_CRON` | `* * * * *` | The `incident-scan` job; empty = no job. |
| `INCIDENT_DB_SLOW_MS` | 1000 | `SELECT 1` slower than this is an incident. |
| `INCIDENT_API_READY_URL` | `http://aureon-api:8000/api/health/ready` | Probed by the scheduler; empty = off. |
| `INCIDENT_DOCKER_PROBE_SECONDS` | 60 | API's docker-proxy poll; 0 = off. |
| `INCIDENT_DOCKER_PROXY_ALLOWS_RESTART` / `INCIDENT_RESTART_CONTAINERS` | false / empty | Container restarts, only if the proxy allows POST restart (the shipped one does not). |

Also used: `CODE_REPOS`, `CODE_ALLOWED_COMMANDS`, `CODE_WORKTREE_ROOT`, `CODE_MAX_*`,
`CODE_TEST_TIMEOUT_SECONDS`, `GITHUB_*`, `DOCKER_API_URL`, `AGENT_HEARTBEAT_SECONDS`.

## Deploy and validate

1. Do the manual steps in [manual-setup.md](manual-setup.md#18--self-repair): pull the
   model, check RAM, set `REPAIR_*`, and give the PAT Contents + Pull requests write.
2. `docker compose run --rm migrate` (0015), then `docker compose up -d api scheduler` and
   `docker compose --profile coding up -d coding-worker` (the repair loop lives there;
   `--profile github up -d github-webhook-worker` for the Actions hook).
3. Validate:
   - `GET /api/agents` lists `repair` (idle). `GET /api/v1/repairs/status` →
     `enabled: true`.
   - `GET /api/scheduler/jobs` has `incident-scan`. After a minute its run succeeds.
   - Stop a worker (`docker compose stop imaging-worker` makes it `stopped`, so no
     incident. To see one, `docker kill` it: after ~45 s a `heartbeat` incident appears, with
     two recovery attempts, then `_manual`).
   - `docker compose stop scheduler` for a minute, then start it: no harm. Then
     `GET /api/v1/repairs/incidents` shows what was detected.
   - With `REPAIR_MODEL` set, `POST /api/v1/repairs {"incident_id": …}` on an exception
     incident. Follow `GET /api/v1/repairs/{id}` to `pr_opened` (or `tests_failed` /
     `blocked` with the reason). The PR appears on GitHub from `repair/<id>`; nothing is
     merged.

## Rollback

- **Turn repairs off:** set `REPAIR_MODEL=` (tasks stay queued), or pause the agent with
  `POST /api/agents/repair/pause`.
- **Turn detection off:** pause the `incident-scan` job (`POST /api/scheduler/jobs/incident-scan/pause`)
  and set `INCIDENT_DOCKER_PROBE_SECONDS=0`. Exception capture only writes rows; to silence
  it entirely, roll the code back.
- **Code:** check out the previous commit and rebuild. The tables can stay (nothing else
  reads them), or `alembic downgrade 0014` drops only these two tables.
- **A repair PR you don't want:** close it on GitHub and delete its `repair/*` branch. Nothing
  was merged.

## Tests

| File | What |
|---|---|
| `tests/test_repairs_unit.py` | Hard-block paths (trading, paper, research, risk, auth, security, secrets, infra, the repair system, case-insensitive) and allowed paths; the after-model patch check (settings secret fields, credential strings); repair branch names; exception fingerprint + location + redaction; the sink's rate limit, requeue and cap; the structlog processor; disabled reasons; the `incident-scan` job |
| `tests/test_repairs_db.py` | **db.** Detection per source: exceptions (logged through structlog, aggregated, rate-limited), heartbeats (3× interval, own windows, stopped and fresh agents ignored, auto-resolve), Docker (unknown, unhealthy, exited, the proxy probe, restart only when the proxy allows it and the container is allowlisted), Actions (the hook, same run once, the table scan, a `repair/*` branch is self-origin), DB slowness and API readiness with auto-resolve. Recovery first, escalation after 2 attempts, idempotent, audited, the lock cleared, a single Actions rerun. The **happy path to `pr_opened`** with a fake Ollama returning a unified diff and GitHub mocked (blob content, tree, commit parent, ref, PR head/base, no merge, main checkout untouched, worktree and branch removed). **Hard block on trading paths** before the model (task blocked, model never called) and after it (no GitHub call). Failing tests → `tests_failed`, no push. `REPAIR_MODEL` unset → queued with the reason, never claimed. Recursion guard (self-origin, depth 1, one active repair), the per-fingerprint daily limit, the circuit breaker. The API: lists, detail, resolve/ignore, 404s, **non-admin 403**, anonymous 401 |
| `tests/test_route_auth.py` | The new routes are pinned as admin-only |

```bash
TEST_REDIS_URL=redis://127.0.0.1:6379/2 PG_ADMIN_URL=… scripts/test_db.sh tests/test_repairs_db.py tests/test_repairs_unit.py
```

## Open risks

- **`restart_agent` is a nudge, not a restart.** Pause/resume only helps a loop that is
  alive but stuck on its pause flag. A dead process needs its container restarted, which
  the read-only proxy cannot do, so it ends as `manual`. Compose `restart: unless-stopped`
  and the heartbeat-file healthchecks remain the real restart mechanism.
- **The coding worker holds the GitHub token.** It needs it to open repair PRs. The tests it
  runs get a scrubbed environment, and the process is non-dumpable, but model-patched code
  does run as its child. Use a fine-grained PAT limited to the repair repo (Contents +
  Pull requests write), and protect the base branch.
- **The base commit must be on GitHub.** The local checkout's base sha is the PR commit's
  parent. If the checkout is ahead of GitHub, the repair fails with "push main first".
- **A small model can be wrong.** The tests gate the push, but tests can miss things. Every
  PR still needs a human review and an approved merge.
- **Exception fingerprints follow code.** A fingerprint is type + module + function, so
  moving or renaming a function opens a new incident.
- **Heartbeat intervals.** Agents that heartbeat less often than `AGENT_HEARTBEAT_SECONDS`
  must report `heartbeat_interval_s` or `stale_after_s` in their details (GitHub and MT5 do),
  or they will look stale.
- **Docker probing lives in the API.** Only the API can reach the proxy, so with several API
  processes each one probes (writes are idempotent per fingerprint).
