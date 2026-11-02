# 05 — Agent registry, scheduler and system monitoring

**Status:** registry, heartbeats, runs and logs, the scheduler worker and `/api/system/status`
are done (revision `0005`). The **restricted coding agent** (Prompt 6 §5–8) is **not in this
change**. It gets its own PR because it creates git worktrees and runs allowlisted test commands,
which deserves separate review.

## Agents

| Agent ID | Process | Registers | Busy when | Pause effect |
| --- | --- | --- | --- | --- |
| `ollama` | API | at startup, in the background | a chat generation is running | `POST /api/chat` returns 503 (this process: immediately; others: within one heartbeat) |
| `imaging` | `imaging-worker` | at startup | an image job is running | stops claiming new jobs; the running job finishes; `POST /images/generate` still queues |
| `scheduler` | `scheduler` | at startup, before seeding jobs | a scheduled task is running | no scheduled job starts |
| `coding`, `trading-adapter` | later phases | when built | — | — |

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
  `GET /containers/*` on the internal `aureon` network. The API reads `DOCKER_API_URL`
  (`http://aureon-docker-proxy:2375`). Set it empty to turn this section off.
- **systemd units:** the API container can't, and shouldn't, run `systemctl`. A host-side helper,
  `deploy/host-status/aureon-host-status.sh`, runs every 30 s from a systemd timer as `svskhd8`.
  It runs `systemctl is-active` for a **fixed** unit list (`AUREON_HOST_UNITS`, default
  `cloudflared docker`; names are validated) and writes `HOST_STATUS_FILE`
  (`/mnt/spasta-data/status/host-services.json`), which the API reads. A file older than
  `HOST_STATUS_MAX_AGE_SECONDS` (120 s) is reported as `stale`. The unit runs sandboxed:
  no network, a read-only system, and writes only to the status directory.

## Tables (migration `0005`, additive, in schema `agents`)

| Table | Key columns |
| --- | --- |
| `agents.agents` | `id` (slug) PK, `name`, `kind`, `state` (CHECK), `paused`, `version`, `host`, `pid`, `details` JSONB, `registered_at`, `last_heartbeat_at`, `last_activity_at` |
| `agents.agent_runs` | `id`, `agent_id` →, `scheduled_job_id` → (on delete set null), `task`, `status` (CHECK), `attempt`, `started_at`, `finished_at`, `duration_ms`, `error`, `details` JSONB; index on (`agent_id`, `started_at`) |
| `agents.agent_logs` | `id`, `agent_id` →, `run_id` → (on delete set null), `level` (CHECK), `message`, `context` JSONB, `created_at`; index on (`agent_id`, `created_at`) |
| `agents.scheduled_jobs` | `id` (slug) PK, `agent_id` →, `task`, `cron`, `paused`, `max_retries`, `retry_backoff_seconds`, `timeout_seconds`, `attempt`, `consecutive_failures`, `next_run_at`, `last_run_at`, `last_status`, `last_error`, `locked_until` |

The downgrade to `0004` drops only these four tables.

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
