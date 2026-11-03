# 05 — Agent registry, scheduler and system monitoring

**Status:** registry, heartbeats, runs and logs, the scheduler worker and `/api/system/status`
are done (revision `0005`). The **restricted coding agent** (Prompt 6 §5–8) is done too (revision
`0008`); see [Coding agent](#coding-agent) below.

## Agents

| Agent ID | Process | Registers | Busy when | Pause effect |
| --- | --- | --- | --- | --- |
| `ollama` | API | at startup, in the background | a chat generation is running | `POST /api/chat` returns 503 (this process: immediately; others: within one heartbeat) |
| `imaging` | `imaging-worker` | at startup | an image job is running | stops claiming new jobs; the running job finishes; `POST /images/generate` still queues |
| `scheduler` | `scheduler` | at startup, before seeding jobs | a scheduled task is running | no scheduled job starts |
| `coding` | `coding-worker` (profile `coding`) | at startup | a code task is running | stops claiming new tasks; the running task finishes |
| trading agents | Dhan feed, MT5 sync, research worker | see docs/11–13 | — | — |

- **Heartbeats:** each process heartbeats every `AGENT_HEARTBEAT_SECONDS` (15 s), recording
  its state (`idle`, `busy`, `error` or `stopped`) and a small `details` object. The same call
  reads back the admin's `paused` flag. Clean shutdown sets `stopped`.
- **Reported state:** the API shows an **effective** state, in priority order
  `paused` > `stale` (no heartbeat for `AGENT_STALE_SECONDS`, 60 s) > the reported state.
- **If the registry can't be reached:** registry writes are best-effort. A DB outage logs
  `heartbeat_failed` but never stops a chat or an image job.
- **Runs:**
  - Every chat turn is an `ollama` run (`task=chat`).
  - Every image job is an `imaging` run (`task=image_job`).
  - Every scheduled execution is a `scheduler` run. Its `attempt` is the retry number and its
    `details` hold the task's result counts.
  - Runs store **IDs only**, never prompts or message text, because any signed-in user can read them.

## Endpoints

| Method | Path | Access | Result |
| --- | --- | --- | --- |
| GET | `/api/agents` | signed in | All registered agents |
| GET | `/api/agents/{id}` | signed in | Agent, its scheduled jobs, run counts for the last 24 h by status |
| GET | `/api/agents/{id}/runs?status=&limit=&offset=` | signed in | Runs, newest first |
| GET | `/api/agents/{id}/logs?level=&run_id=&limit=&offset=` | admin | Logs, newest first |
| POST | `/api/agents/{id}/pause` · `/resume` | admin + CSRF, audited | Updated agent |
| GET | `/api/scheduler/jobs` | admin | All scheduled jobs, with `running` when a lease is held |
| POST | `/api/scheduler/jobs/{id}/pause` · `/resume` · `/run` | admin + CSRF, audited | Updated job (`run` makes it due now and resets its retry counter) |
| GET | `/api/system/status` | admin | See below |

Audit events: `agent_paused`, `agent_resumed`, `scheduled_job_pause`, `scheduled_job_resume`
and `scheduled_job_run`, each with the admin's user ID and the target ID.

## Scheduler

`python -m app.workers.scheduler` (compose service `scheduler`).

- **Tasks are code, not commands.** `agents.scheduled_jobs.task` must name an entry in
  `TASKS` in `app/workers/scheduler.py`. An unknown name fails without retrying, so nothing
  arbitrary can be scheduled by editing the table.
- **State lives in Postgres:** `next_run_at`, `attempt`, `consecutive_failures`,
  `last_status`/`last_error`, and a lease (`locked_until` = now + timeout + 30 s). Jobs are
  claimed with `FOR UPDATE SKIP LOCKED`, so even two schedulers run a job once. If a scheduler
  crashes mid-run, the lease expires and the job becomes due again.
- **Retries:** after a failure the job retries after `retry_backoff_seconds × 2^(attempt−1)`,
  capped at 1 h, up to `max_retries` times. After that it counts one consecutive failure and
  waits for its next cron slot. A success resets both counters. Each job also has its own
  `timeout_seconds`.
- **Cron times** are evaluated in `SCHEDULER_TIMEZONE` (`Asia/Kolkata`). They are stored in UTC.

Default jobs, created if missing (later edits such as pausing are kept):

| Job ID | Task | Cron (IST) | Does |
| --- | --- | --- | --- |
| `purge-agent-history` | `purge_agent_history` | `17 3 * * *` | Deletes agent logs older than `AGENT_LOG_RETENTION_DAYS` (30) and finished runs older than `AGENT_RUN_RETENTION_DAYS` (90) |
| `purge-expired-sessions` | `purge_expired_sessions` | `29 3 * * *` | Deletes sessions that expired or were revoked more than 7 days ago |
| `refresh-model-inventory` | `refresh_model_inventory` | `*/30 * * * *` | Re-reads Ollama `/api/tags` into `ai.model_inventory` |

Later phases add their jobs, such as the Dhan instrument refresh and reconciliation, by
registering a task and a default row.

## GET /api/system/status

```json
{"status": "ok | degraded", "generated_at": "…",
 "host":    {"cpu": {"count", "percent", "load_avg"}, "memory": {…}, "disks": {"ssd": {…}, "hdd": {…}}, "uptime_seconds"},
 "postgres":{"version", "database_bytes", "connections"},
 "redis":   {"used_memory_bytes"},
 "ollama":  {"installed_models", "loaded_models"},
 "imaging": {"worker": "idle|busy|paused|stale|absent", "queued", "running"},
 "docker":  {"containers": [{"name","image","state","status"}], "not_running_or_unhealthy": []},
 "host_services": {"units": {"cloudflared": "active", "docker": "active"}, "age_seconds", "stale"}}
```

- **Section results:** every section has `ok` and `latency_ms`, and is time-limited
  (`HEALTH_CHECK_TIMEOUT`). A failing section shows only the exception type, never hosts or DSNs.
- **Thresholds:** `ok` turns false at ≥ 95 % RAM, ≥ 90 % on a disk, a stale or absent imaging
  worker, any container not running or unhealthy, or any host unit not `active`.
- **Host figures:** CPU, RAM and disk come from psutil inside the API container, which reports
  the host's totals. The SSD figure is the container root filesystem, which sits on the SSD,
  and the HDD figure is `DATA_ROOT`.

### Why there's no docker.sock in the API, and no systemctl

- **Docker:** mounting `/var/run/docker.sock`, even `:ro`, lets a process create privileged
  containers, which makes it root on the host. Instead, compose runs
  `tecnativa/docker-socket-proxy` (`aureon-docker-proxy`). It holds the socket and exposes only
  `GET /containers/*` on its own internal `aureon-docker` network, shared only with the API (Phase E). The API reads `DOCKER_API_URL`
  (`http://aureon-docker-proxy:2375`). Set it empty to turn this section off.
- **systemd units:** the API container can't, and shouldn't, run `systemctl`. A host-side helper,
  `deploy/host-status/aureon-host-status.sh`, runs every 30 s from a systemd timer as `svskhd8`.
  It runs `systemctl is-active` for a **fixed** unit list (`AUREON_HOST_UNITS`, default
  `cloudflared docker`; names are validated) and writes `HOST_STATUS_FILE`
  (`/mnt/spasta-data/status/host-services.json`), which the API reads. A file older than
  `HOST_STATUS_MAX_AGE_SECONDS` (120 s) is reported as `stale`. The unit runs sandboxed:
  no network, a read-only system, and writes only to the status directory.

## Tables (migrations `0005` and `0008`, additive, in schema `agents`)

| Table | Key columns |
| --- | --- |
| `agents.agents` | `id` (slug) PK, `name`, `kind`, `state` (CHECK), `paused`, `version`, `host`, `pid`, `details` JSONB, `registered_at`, `last_heartbeat_at`, `last_activity_at` |
| `agents.agent_runs` | `id`, `agent_id` →, `scheduled_job_id` → (on delete set null), `task`, `status` (CHECK), `attempt`, `started_at`, `finished_at`, `duration_ms`, `error`, `details` JSONB; index on (`agent_id`, `started_at`) |
| `agents.agent_logs` | `id`, `agent_id` →, `run_id` → (on delete set null), `level` (CHECK), `message`, `context` JSONB, `created_at`; index on (`agent_id`, `created_at`) |
| `agents.scheduled_jobs` | `id` (slug) PK, `agent_id` →, `task`, `cron`, `paused`, `max_retries`, `retry_backoff_seconds`, `timeout_seconds`, `attempt`, `consecutive_failures`, `next_run_at`, `last_run_at`, `last_status`, `last_error`, `locked_until` |

| `agents.code_tasks` (`0008`) | `id` uuid PK, `kind` (analyze/change), `repo`, `title`, `instruction`, `paths`, `status` (queued/running/ready/failed/approved/rejected), `base_ref`, `base_sha`, `branch`, `worktree`, `commit_sha`, `model`, `result` JSONB, `tests` JSONB, `error`, `created_by` →, `decided_by` →, timestamps |

The downgrade to `0004` drops only the four Phase 5 tables; `alembic downgrade 0007` drops only `agents.code_tasks`.

## Coding agent

A restricted agent that reads your allowlisted repos and makes **reviewed** changes. Every
endpoint is **admin-only** (`AdminRequired`); writes also need CSRF and are audited.

| Endpoint | What |
|---|---|
| `GET /api/repos` | the allowlisted repos: base branch, head, branch count, test commands |
| `GET /api/repos/{name}/tree?ref=&path=` | directory listing from git (`.env`, keys and `.git` hidden) |
| `GET /api/repos/{name}/file?ref=&path=` | one file from git, capped at `CODE_MAX_FILE_BYTES`; binaries are flagged, not sent |
| `GET /api/repos/{name}/commits`, `/branches` | history and branches |
| `POST /api/code/analyze` | queue a question about the code (local Ollama, read-only); the answer lands in the task |
| `POST /api/code/tasks` | queue a change (below) |
| `GET /api/code/tasks[/{id}]`, `GET /api/code/tasks/{id}/diff` | status, test results, the diff |
| `POST /api/code/tasks/{id}/approve` | keep the change on its branch, optionally also as `feature/<name>` or `fix/<name>` |
| `POST /api/code/tasks/{id}/reject` | delete the task branch and its worktree |
| `GET /api/code/git/repos` | `GITHUB_CODING_REPO_ALLOWLIST` with each repo's clone state (see "Git ops") |
| `POST /api/code/git/clone`, `/pull` | `{repo: "owner/name"}`: queue a clone / fetch + fast-forward |
| `POST /api/code/git/push` | `{repo, branch, title?, body?}`: queue a push of one feature branch + a PR |
| `GET /api/code/git/ops[/{id}]` | queued / running / done / failed git ops, with the PR link |

### What a change task does

1. **Isolated worktree:** `git worktree add -b aureon/task-<id>` under `CODE_WORKTREE_ROOT`,
   from the repo's base branch. The main checkout is never modified (tests check this).
2. **Model:** the local Ollama `CODE_MODEL` sees a file listing plus the files you name in
   `paths`, and must answer with one unified diff.
3. **Patch checks before anything is applied:**
   - each path: no absolute paths, no `..`, never `.git`, `.env*`, keys, `secrets/`
   - no binary patches, symlinks, submodules or executable modes; size ≤ `CODE_MAX_PATCH_BYTES`
   - each path re-checked **inside the worktree with symlinks resolved**, so a patch cannot
     write through `node_modules` → the main checkout
   - `git apply --check`
4. **Apply + commit:** only in the worktree, committing exactly the patch's files, as
   "Aureon coding agent". Git hooks never run (`core.hooksPath` points nowhere), commits skip
   hooks (`--no-verify`) and `protocol.allow=never` blocks any fetch or push.
5. **Tests:** the repo's `tests` commands. Each must be an **exact argv match** in
   `CODE_ALLOWED_COMMANDS`, run **without a shell**, in the worktree, with a scrubbed
   environment (only `PATH`, `LANG`, `TZ`, a temp `HOME`, `CI=1`), a hard timeout that kills
   the process group, and capped output. The repo's `node_modules` / `.venv` are symlinked in
   (`link`) so tests run offline; they are never committed.
6. **Review:** the task is `ready` with its diff and test results. **Approve** needs every
   test to pass and never accepts a protected branch (`CODE_PROTECTED_BRANCHES`: main,
   master, dev01, …). It keeps `aureon/task-<id>` and can add a `feature/…` name. **Nothing is
   merged or deployed**, and nothing is pushed unless an admin queues a push (below).

A crash mid-task marks it failed and removes its worktree on the next start.

### Git ops: clone, pull, push + PR

The coding worker can also talk to GitHub, for **allowlisted repos only**:

- **Allowlist.** `GITHUB_CODING_REPO_ALLOWLIST` (JSON list of `owner/repo`, default `[]` =
  nothing). Each repo must also pass the GitHub allowlist (`GITHUB_ALLOWED_OWNERS` /
  `_REPOS`, D-15-3); both are checked when an op is queued and again when it runs.
- **Where.** `CODE_CLONE_ROOT/<owner>/<repo>` (default
  `/mnt/spasta-data/code-worktrees/repos`, on the HDD volume the worker already mounts
  read-write). A clone is registered as the coding repo `gh-<owner>-<repo>` with its default
  branch as base, so analyze / change tasks run on it like on any `CODE_REPOS` entry.
- **Queue.** The API only validates and queues (`agents.code_git_ops`, migration 0019); the
  coding worker claims ops one at a time and runs each under `HEAVY_MODEL_LOCK`, so no
  model-written test process runs while a git child holds the token. One op per repo at a
  time (409 `op_in_progress`).
- **Pull** = `git fetch --prune` + a fast-forward-only update of the default branch.
- **Push rules** (refused before any git runs; 409 `push_refused`): the branch must start with
  one of `CODE_PUSH_BRANCH_PREFIXES` (default `aureon/,feat/`), must be a plain branch name,
  and is never `main`, `master`, `dev01`, the repo's default branch, a
  `CODE_PROTECTED_BRANCHES` name, or a branch GitHub reports as protected. An `aureon/task-*`
  branch is pushable only once its task is **approved**. The push is one explicit
  `refs/heads/<b>:refs/heads/<b>` refspec: no `--force`, no tags, no deletes.
- **Secrets.** Every path touched by **any** commit between `origin/<default>` and the branch
  is checked (so a secret added and removed again still counts); a hit in the shared
  `secrets` category (`app/core/sensitive_paths.py`) or the agent's own blocklist (`.git`,
  `.env*`, keys) refuses the push. Infra / auth / trading paths are allowed but listed in the
  op's `result.sensitive` for the reviewer.
- **PR.** After the push, a PR to the default branch is opened (or the open one for that head
  is reused) through the tracked GitHub client (`github.tracked_operations`, `pr.create`).
  **Merging is not here:** it stays behind the approval engine (`github.pr.merge`).
- **Token.** `GITHUB_TOKEN` (needs contents:write and pull_requests:write on these repos)
  never appears in argv, a remote URL, `.git/config`, the op row or a log. Each network git
  call gets it in its own environment only (`AUREON_GIT_TOKEN`), read by a temporary
  `GIT_ASKPASS` script that holds no secret and is deleted afterwards; credential helpers,
  user and system git config are disabled; output and errors are redacted; and the repo's
  config is checked for credentials after every clone / pull / push. The remote is
  `GITHUB_GIT_URL` (https only; prod refuses anything else).
- **Audit.** `code_git_<op>_queued` (API, with the admin) and `code_git_<op>_done|failed`
  (worker: repo, branch, sha, PR number, error, duration). Each op is also a `coding` agent
  run (`code_git_op`).

```bash
# .env: GITHUB_CODING_REPO_ALLOWLIST=["SVSKHD/ASTRA-FRONTEND"]
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' \
  $B/api/code/git/clone -d '{"repo":"SVSKHD/ASTRA-FRONTEND"}'
curl -sS -b $J $B/api/code/git/ops | python3 -m json.tool          # status, error
# ... a change task on repo "gh-svskhd-astra-frontend", approved ...
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' \
  $B/api/code/git/push -d '{"repo":"SVSKHD/ASTRA-FRONTEND","branch":"aureon/task-0123abcd4567","title":"..."}'
```

### Configure

```bash
mkdir -p /opt/private-ai/repos && cd /opt/private-ai/repos
git clone https://github.com/SVSKHD/ASTRA-FRONTEND.git astra-frontend
git -C astra-frontend switch dev01
(cd astra-frontend && npm ci)          # node_modules the tests will reuse
nano /opt/private-ai/aureon/.env       # CODE_REPOS, CODE_MODEL (see .env.example)
docker exec <ollama-container> ollama pull qwen2.5-coder:7b
docker compose run --rm migrate        # 0008 code_tasks, 0019 code_git_ops (new tables only)
docker compose --profile coding up -d --build coding-worker
curl -sS -b $J $B/api/repos | python3 -m json.tool
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' $B/api/code/analyze \
  -d '{"repo":"astra-frontend","question":"What does src/api/client.ts do?","paths":["src/api/client.ts"]}'
```

### Risks to know

- **Tests run repo code.** `npm test` or `pytest` executes code from the repo and the
  model's patch. The environment is scrubbed, but the process runs as the same user as the
  worker and could read the worker's own environment (`/proc`), which includes `DATABASE_URL`.
  Only allowlist repos you own, and review the diff before approving.
- **Repos are read-write inside `coding-worker`,** because git keeps worktrees and branches in
  `.git`. The agent itself only writes `aureon/task-*` and approved `feature/*` / `fix/*` refs.
- **The token is in the worker's environment** (`env_file: .env`), as it already was for
  self-repair. The process is non-dumpable (D-E12), test processes get a scrubbed env, and git
  ops never overlap a test run (`HEAVY_MODEL_LOCK`). Use a fine-grained token limited to the
  allowlisted repos.
- **The API image has no `git`.** Approving / rejecting a change task runs git in the API
  process (unchanged from Phase 5b), so on the stock image approval of a task fails with
  `git_error`. Pushing a task branch needs the task approved, so the clone → task → approve →
  push flow needs git in the API image (or approval moved into the worker) first. Clone,
  pull and the push rules themselves do not depend on it.
- **Partial replacement for the GitHub proxy:** `/api/repos/*` covers trees, files, commits and
  branches of local checkouts. GitHub issues, pull requests and comments are not served,
  because Aureon makes no external API calls. Stage 5 of the frontend migration uses these
  endpoints for the code browser only.

## Deploy and validate

```bash
cd /opt/private-ai/aureon && git pull
docker compose build api && docker compose run --rm migrate          # applies 0005
docker compose up -d api scheduler docker-proxy
docker compose --profile imaging up -d imaging-worker                 # if enabled (Phase 4)
docker compose logs --tail=20 scheduler                               # "scheduler_started"
```

Host status helper (needs sudo to install a system unit, so confirm before running these):

```bash
mkdir -p /mnt/spasta-data/status
sudo cp deploy/host-status/aureon-host-status.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aureon-host-status.timer
cat /mnt/spasta-data/status/host-services.json
```

Add `nginx` to `AUREON_HOST_UNITS` in the `.service` file if Spasta's Nginx runs on the host
rather than in a container.

Checks, reusing `J`, `B` and `CSRF` from the auth smoke test with an admin login:

```bash
curl -sS -b $J $B/api/agents | python3 -m json.tool          # ollama, scheduler (+ imaging)
curl -sS -b $J $B/api/scheduler/jobs | python3 -m json.tool
curl -sS -b $J -H "x-csrf-token: $CSRF" -X POST $B/api/scheduler/jobs/refresh-model-inventory/run
sleep 10; curl -sS -b $J "$B/api/agents/scheduler/runs?limit=1" | python3 -m json.tool
curl -sS -b $J $B/api/system/status | python3 -m json.tool
curl -sS -b $J -H "x-csrf-token: $CSRF" -X POST $B/api/agents/ollama/pause    # chat → 503
curl -sS -b $J -H "x-csrf-token: $CSRF" -X POST $B/api/agents/ollama/resume
ss -tlnp | grep -E ':(2375|5432|6379|11434)\b' || echo "no internal ports exposed"
```
