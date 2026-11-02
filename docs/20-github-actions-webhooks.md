# 20 — GitHub Actions and webhooks

Phase 17 of the roadmap. It builds on the Phase 15 client ([18-github-foundation.md](18-github-foundation.md))
and the Phase 16 tracked operations and approval gate ([19-github-issues-prs.md](19-github-issues-prs.md)),
and adds:

- Actions reads: workflows, runs, a run's jobs, and **sanitised** job and run logs
- **allowlisted** workflow dispatch and run re-runs, plus run cancel. Production workflows
  only run after an admin approves (ApprovalGate kind `github.workflow_dispatch_prod`).
- `github.workflow_runs`, `github.workflow_jobs` and `github.webhook_events`
- an **HMAC-verified, idempotent webhook receiver** (`POST /api/v1/github/webhooks`, public)
  and the `github-webhooks` worker that processes what it stores
- the `github_actions_sync` scheduler job
- `on_workflow_failed(fn)`, the hook Phase 18 (self-repair) subscribes to

## What it does

### Actions reads and logs

- **Same client, same rules.** Every call goes through `GitHubClient` (token from the
  environment, redaction, retry, rate limits) after the repo allowlist check: a repo
  outside it is a 404 and GitHub is never called.
- **Logs.** GitHub answers `GET …/actions/jobs/{id}/logs` and `…/runs/{id}/logs` with a
  302 to a pre-signed blob URL on another host.
  - The API client never follows an off-host redirect. For logs it is asked to hand the
    Location back instead (`request(..., return_redirect=True)`).
  - `fetch_log_blob` then downloads it with a **separate HTTP client that has no
    Authorization header**, only from `GITHUB_LOG_HOSTS` (https only, no credentials in the
    URL, port 443, and `*` matches exactly one DNS label). Every redirect hop is checked
    again. The pre-signed URL is never logged; errors name only its host.
  - At most `GITHUB_LOG_DOWNLOAD_MAX_BYTES` are downloaded. A job log keeps only the last
    2 × `GITHUB_LOG_MAX_BYTES` in memory while streaming (tail-first).
  - Run logs are a zip. It is unpacked in memory, in a worker thread, with
    `GITHUB_LOG_ZIP_MAX_ENTRIES` and `GITHUB_LOG_UNZIP_MAX_BYTES` caps. Each entry is read
    with a bounded read, so a lying header or a zip bomb cannot inflate past the cap. The
    top-level `N_job.txt` files are used (the per-step folders duplicate them), in job
    order, each under a `===== name =====` header. An archive over the download cap is 413
    `log_archive_too_large`; read the job logs instead.
  - **Sanitising** (`app/integrations/github/logs.py`), in this order:
    1. strip ANSI/VT escape codes (CSI, OSC, charset selection) and control characters,
       and fold `\r` into `\n`. Stripping comes first because an escape code inside a
       token would hide it from the redactor.
    2. cut tail-first at a line boundary, so no token is split by the cut
    3. redact: GitHub tokens (`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), the
       configured token itself, AWS access key ids (`AKIA…`, `ASIA…` and the other
       prefixes), JWTs, Slack/Stripe/Google/npm/PyPI token shapes, `Authorization:` (Bearer,
       token, Basic, Digest) and `X-Api-Key` headers, `user:password@` in URLs, private-key
       blocks (also an orphaned key tail whose BEGIN line was cut off), and `NAME=value` /
       `NAME: value` pairs whose name says secret (`*_KEY`, `*TOKEN*`, `*SECRET*`,
       `*PASSWORD*`, …) or whose value is high-entropy (≥ 20 characters, ≥ 2 character
       classes, Shannon entropy > 4 bits/char; paths, URLs and `*sha`/`*id` names excepted)
    4. cap once more to `GITHUB_LOG_MAX_BYTES`, with a `[log truncated: …]` first line
  - The answer is `text/plain` with `X-Log-Truncated: true|false`,
    `X-Log-Original-Bytes` and `Cache-Control: no-store`.

### Dispatch, re-run and cancel

```
POST …/workflows/{workflow}/dispatch {ref, inputs}
  ├─ file name not in GITHUB_DISPATCH_ALLOWLIST ─► 403 workflow_not_allowlisted (no GitHub call)
  ├─ GET the workflow (ids resolve to their file) ─► not allowlisted ─► 403 (nothing written)
  ├─ workflow not active ─► 409 workflow_not_active
  ├─ production workflow ─► ApprovalGate (github.workflow_dispatch_prod)
  │      202 {status: pending_approval, approval: {approval_id, artifact, …}}   zero writes
  │      approve ─► allowlists re-checked ─► artifact recomputed ─► POST …/dispatches once
  └─ otherwise ─► tracked_write: POST …/dispatches ─► 202 {status: sent}
```

- **The allowlist:** `GITHUB_DISPATCH_ALLOWLIST` is a JSON list of
  `"owner/repo:workflow_file"` (case-insensitive). It covers dispatch and both re-runs. A
  run is matched by its workflow file (`path`, or the workflow fetched by `workflow_id`).
- **Production workflows:** a workflow whose name, file or file stem is in
  `GITHUB_PROD_WORKFLOWS`, **or whose name or file contains `deploy` or `prod`**, is
  production (D-17-3). Its dispatch and its re-runs are filed with the gate and sent only
  from the executor after approval. The tracked operation goes `pending_approval` →
  `running` → `succeeded` | `failed` | `invalidated` | `denied` | `expired`.
- **The artifact** (what the approver sees, recomputed at approval; any change invalidates):
  - dispatch: the workflow id, its path and state, the ref, the **commit sha the ref points
    at**, and the inputs. A push to the branch between request and approval invalidates it.
  - re-run: the run id, the workflow file, the run's head sha, its attempt and its status.
    A re-run already started by someone else (a new attempt) invalidates it.
- **Re-run** (`/rerun`, `/rerun-failed` → GitHub's `rerun-failed-jobs`) needs a completed
  run (409 `run_not_completed` otherwise).
- **Cancel** needs only the repo allowlist: stopping a run is the safe direction (D-17-4).
- Every direct write is a `github.tracked_operations` row (`actions.workflow_dispatch`,
  `actions.run_rerun`, `actions.run_rerun_failed`, `actions.run_cancel`) and an audit
  event `github_actions_*`, whatever the outcome.

### Webhooks

```
GitHub ─► POST /api/v1/github/webhooks   (public; nginx /api path; Cloudflare Access bypass)
  ├─ no GITHUB_WEBHOOK_SECRET ─► 503 webhook_not_configured
  ├─ Content-Type not application/json ─► 415
  ├─ body > GITHUB_WEBHOOK_MAX_BYTES ─► 413 (reading stops at the cap)
  ├─ X-Hub-Signature-256 missing / wrong ─► 401   nothing stored: counter + log line only
  ├─ X-GitHub-Event / X-GitHub-Delivery missing, body not a JSON object ─► 400
  └─ INSERT … ON CONFLICT (delivery_id) DO NOTHING
        ├─ conflict ─► 200 {status: duplicate}           (never reprocessed)
        ├─ repo off the allowlist / unhandled event ─► 202 {status: ignored}   (stored, not queued)
        └─ 202 {status: accepted} ─► LPUSH github:webhooks <delivery id>
                                           │
github-webhooks worker ◄── BRPOP ──────────┘   + a DB scan of due pending rows (SKIP LOCKED)
  └─ lock one row ─► savepoint ─► handle ─► processed
                                   └─ error ─► attempts+1, retry after base·2^(n-1) (≤ 1 h)
                                               ─► failed after GITHUB_WEBHOOK_MAX_ATTEMPTS
```

- **Signature:** HMAC-SHA256 of the raw body with `GITHUB_WEBHOOK_SECRET`, compared with
  `hmac.compare_digest`. Only `sha256=` signatures are accepted (no SHA-1).
- **Rejections** are counted in the Redis hash `github:webhooks:rejected` (fields
  `bad_signature`, `too_large`, `content_type`, `bad_headers`, `bad_json`,
  `not_configured`) and logged as `github_webhook_rejected`. Nothing else is written.
- **Stored payload:** token-redacted and trimmed to `GITHUB_WEBHOOK_STORE_MAX_BYTES`. Big
  objects (commits, head_commit, pull_request, issue, sender, …) are cut to a summary
  first; `workflow_run` and `workflow_job` are kept whole as long as possible because the
  worker needs them. `payload_truncated` says when it was trimmed.
- **Allowlist:** the Phase 15 rules. With neither `GITHUB_ALLOWED_REPOS` nor
  `GITHUB_ALLOWED_OWNERS` set, "the token user's own repos" uses the login from the last
  successful connection check (the receiver never calls GitHub).
- **The queue is bounded** (10,000 ids). Ids lost from it (a Redis restart, the trim, Redis
  down at receive time) are still found by the worker's DB scan.
- **Handlers:**
  - `workflow_run` → upsert `github.workflow_runs`. An event with an older `updated_at`
    than the row never overwrites it (out-of-order delivery).
  - `workflow_job` → upsert `github.workflow_jobs` (steps as jsonb, at most 200). A
    completed job is not moved back by a late `in_progress` event of the same attempt.
  - `push` → `github.repositories.pushed_at` for that repo. `pull_request`, `issues`,
    `ping` → recorded (the stored event is the record; the API keeps no cache to drop).
- **Retries and poison events:** the handler runs inside a savepoint, so a failure rolls
  back its partial writes but keeps the row lock. `attempts` goes up and `next_attempt_at`
  moves out; after `GITHUB_WEBHOOK_MAX_ATTEMPTS` the event is `failed` and never picked up
  again. The error text is token-redacted.
- **`on_workflow_failed(fn)`** (`app.services.github_webhooks`, re-exported by the worker):
  `fn(WorkflowFailure)` is awaited for every `workflow_run` `completed` event whose
  conclusion is `failure`, `timed_out` or `startup_failure`, **after** the event is
  committed. A callback that raises is logged and never retried; it never fails the event.
  Nothing subscribes in this phase. **Recursion note for Phase 18:** a repair PR's CI can
  fail and fire this hook again. `WorkflowFailure` carries `branch`, `event` and
  `head_sha` so the subscriber can skip runs on its own repair branches; the recursion
  guard itself belongs to Phase 18.

### Scheduler

`github_actions_sync` (job `github-actions-sync`, `GITHUB_ACTIONS_SYNC_CRON`, only when
`GITHUB_TOKEN` is set) fetches the latest `GITHUB_ACTIONS_SYNC_RUNS` runs of every
allowlisted, synced, non-archived repo (at most 50) into `github.workflow_runs`. Repos with
Actions disabled (404/403) are skipped. It honours the `github` agent's pause flag.

## Endpoints

Admin only, GETs included; POSTs need CSRF. Without `GITHUB_TOKEN` they answer 503
`github_not_configured`. Error shapes as in [18-github-foundation.md](18-github-foundation.md#endpoints).

`R` = `/api/v1/github/repositories/{owner}/{repo}`

| Method | Path | What |
| --- | --- | --- |
| GET | `R/actions/workflows` | Workflows with `file`, `dispatch_allowed` and `production`. |
| GET | `R/actions/runs?workflow=&branch=&status=&per_page=&page=` | One page of runs (`{items, page, per_page, has_next}`). `workflow` is an id or a `.yml` file. |
| GET | `R/actions/runs/{run_id}` | One run. |
| GET | `R/actions/runs/{run_id}/jobs` | The latest attempt's jobs with their steps. |
| GET | `R/actions/runs/{run_id}/logs` | All job logs (zip, unpacked), sanitised text. 413 over the caps. |
| GET | `R/actions/jobs/{job_id}/logs` | One job's log, sanitised text, tail-first. |
| POST | `R/actions/workflows/{workflow}/dispatch` | `{ref, inputs?}` (≤ 25 inputs). 202 `sent` or `pending_approval`; 403 off the allowlist. |
| POST | `R/actions/runs/{run_id}/rerun` | Re-run the whole run (allowlisted workflows; production ones gated). |
| POST | `R/actions/runs/{run_id}/rerun-failed` | Re-run the failed jobs. |
| POST | `R/actions/runs/{run_id}/cancel` | Cancel (tracked). |
| POST | `/api/v1/github/webhooks` | **Public**, HMAC-verified receiver (see above). |

The 202 answer of dispatch / re-run / cancel is `{status: "sent" | "pending_approval",
action, repo, target, approval}`; `approval` is the Phase 16 `GatedOut` when gated.
Approve or deny it with `POST /api/v1/approvals/{id}/approve|deny` as in Phase 16.

## Tables (migration 0014, additive)

- **`github.workflow_runs`:** id (GitHub run id), repo, workflow_id, name, path, event,
  branch, head_sha, status, conclusion, run_attempt, html_url, created_at, updated_at,
  synced_at. Index (repo, created_at).
- **`github.workflow_jobs`:** id (GitHub job id), run_id, repo, name, status, conclusion,
  run_attempt, html_url, started_at, completed_at, steps jsonb, synced_at. Index (run_id).
  No FK to runs: a job event can arrive first.
- **`github.webhook_events`:** id, delivery_id (unique), event, action, repo, received_at,
  processed_at, status (pending, processed, failed or ignored), attempts, next_attempt_at,
  error, payload jsonb (capped), payload_truncated, signature_valid. Indexes
  (status, next_attempt_at), (received_at).

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `GITHUB_LOG_MAX_BYTES` | `1048576` | Largest log text returned (tail-first). |
| `GITHUB_LOG_DOWNLOAD_MAX_BYTES` | `33554432` | Most bytes downloaded from the log blob. |
| `GITHUB_LOG_UNZIP_MAX_BYTES` | `67108864` | Total unzipped size of run logs. |
| `GITHUB_LOG_ZIP_MAX_ENTRIES` | `500` | Files allowed in the run-log zip. |
| `GITHUB_LOG_HOSTS` | `["pipelines.actions.githubusercontent.com","*.actions.githubusercontent.com","productionresultssa*.blob.core.windows.net"]` | Hosts a log redirect may point at. Logs only. |
| `GITHUB_DISPATCH_ALLOWLIST` | `[]` | `"owner/repo:workflow_file"` entries that may be dispatched / re-run. Empty = none. |
| `GITHUB_PROD_WORKFLOWS` | `[]` | Extra production workflow names or files (gated). `deploy`/`prod` always count. |
| `GITHUB_WEBHOOK_SECRET` | unset | HMAC secret. Unset = the receiver answers 503. |
| `GITHUB_WEBHOOK_MAX_BYTES` | `1048576` | Largest webhook body accepted. |
| `GITHUB_WEBHOOK_STORE_MAX_BYTES` | `262144` | Largest stored payload (trimmed beyond). |
| `GITHUB_WEBHOOK_MAX_ATTEMPTS` | `5` | Attempts before an event is `failed`. |
| `GITHUB_WEBHOOK_RETRY_BASE_SECONDS` | `5` | Backoff base (doubles per attempt, ≤ 1 h). |
| `GITHUB_WEBHOOK_POLL_SECONDS` | `5` | Worker BRPOP wait / DB scan period. |
| `GITHUB_ACTIONS_SYNC_CRON` | `*/15 * * * *` | `github-actions-sync` schedule. |
| `GITHUB_ACTIONS_SYNC_RUNS` | `30` | Runs fetched per repo per sync. |

## Deploy and validate

1. Do the steps in [manual-setup.md](manual-setup.md#17--github-actions-and-webhooks): the
   PAT's Actions permission, the webhook secret, the GitHub webhook, the Cloudflare Access
   bypass for exactly `/api/v1/github/webhooks`, and the allowlists.
2. `docker compose run --rm migrate` applies 0014 (three tables).
3. `docker compose up -d api scheduler` and `docker compose --profile github up -d
   github-webhook-worker`. The worker's healthcheck is its heartbeat file, like the others.
4. Validate:
   ```bash
   B=https://api.spasta.online/api/v1/github/repositories/OWNER/REPO
   curl -s -b jar "$B/actions/workflows" | jq '.[] | {file, dispatch_allowed, production}'
   curl -s -b jar "$B/actions/runs?per_page=5" | jq '.items[] | {id, name, conclusion}'
   curl -s -b jar "$B/actions/jobs/$JOB/logs" -D - -o job.log; tail job.log
   # GitHub → the webhook → Recent Deliveries: the ping shows 202.
   docker compose exec redis redis-cli HGETALL github:webhooks:rejected   # should stay empty
   psql … -c "SELECT event, status, count(*) FROM github.webhook_events GROUP BY 1,2"
   psql … -c "SELECT id, name, status, conclusion FROM github.workflow_runs ORDER BY created_at DESC LIMIT 5"
   ```
   A signature test from the host: `curl -X POST -H 'content-type: application/json'
   -d '{}' https://api.spasta.online/api/v1/github/webhooks` must answer 401.
5. Dispatch a non-production allowlisted workflow and check `GET /api/v1/github/operations`
   shows `actions.workflow_dispatch` `succeeded`. Dispatch a production one: 202
   `pending_approval`, and nothing runs until it is approved.

## Rollback

- **Stop receiving:** unset `GITHUB_WEBHOOK_SECRET` (503) or disable the webhook on GitHub;
  `docker compose stop github-webhook-worker`. Pending rows stay and are processed when it
  starts again.
- **Stop dispatches:** set `GITHUB_DISPATCH_ALLOWLIST=[]` (everything 403; pending prod
  approvals fail the allowlist re-check at approval).
- **Code:** redeploy the previous image. Phase 15/16 endpoints are unchanged.
- **Schema:** `docker compose run --rm migrate alembic downgrade 0013` drops only the three
  new tables (export `github.webhook_events` first if you want the history). The `github`
  schema stays.

## Tests

- `tests/test_github_actions_unit.py` (no DB):
  - ANSI stripping (CSI, OSC, charset escapes, `\r`, control characters)
  - redaction of 15 secret shapes (GitHub tokens, AWS key ids and secrets, Bearer / token /
    Basic / X-Api-Key headers, secret-named and high-entropy `KEY=value`, JWT, URL
    credentials, Slack); ordinary lines (shas, URLs, paths, timestamps, `***`) untouched;
    private-key blocks and orphaned key tails
  - the tail cut at a line start; strip → cut → redact → cap order (a token split by an
    escape code is still caught)
  - unzip: top-level job logs in order, the total cap, the entry cap, a bad zip, a zip bomb
  - `log_host_allowed` (12 cases: http, multi-label wildcard, look-alike hosts,
    credentials, port, the API host)
  - the blob fetch sends no Authorization, refuses an off-list redirect hop before
    requesting it, never echoes the signed URL, and caps head- and tail-first
  - the client returns an off-host redirect only when asked
  - signatures (wrong secret, changed body, missing, no prefix, sha1, non-ASCII, empty
    secret); payload trimming; the dispatch policy; settings parsing; the scheduler wiring;
    the callback registry (idempotent, a raising callback does not stop the others)
- `tests/test_github_actions_db.py` (`-m db`, respx):
  - reads, query params, validation, non-admin 403, allowlist 404 with zero calls
  - job logs: token on the API call only, none on the blob, ANSI and secrets gone; the
    tail-first cap; a redirect to another host (or plain http) 502 and never requested;
    run logs unzipped and sanitised; the entry cap and the download cap 413
  - dispatch: not allowlisted 403 with **zero** GitHub calls (and by id, zero writes);
    allowlisted runs directly and is tracked + audited; an inactive workflow 409
  - a production dispatch is 202 with **zero writes**, runs exactly once after approval,
    a second approve 409; a moved ref invalidates; keyword (`deploy`) workflows are gated
    by default; a production re-run is gated too
  - re-run, re-run-failed and cancel tracked; off-allowlist re-run 403; in-progress 409;
    CSRF and admin required; a GitHub failure tracked as `failed`
  - webhooks: valid 202 stored + queued; bad / missing signature or changed body 401 with
    nothing stored and the counter at 3; a duplicate 200 and processed once (also after
    processing); oversize 413; content type 415; missing delivery 400; non-object JSON 400;
    no secret 503; an unallowlisted repo and an unhandled event `ignored`, not queued;
    no session or CSRF needed
  - worker: workflow_run and workflow_job into the tables, an out-of-order event ignored,
    the queue and the DB scan, `on_workflow_failed` fired once with the right run; retries
    roll back partial writes, redact the error and poison after N; the backoff is waited
    out; the heartbeat agent
  - `github_actions_sync` upserts runs and skips a repo without Actions
- `tests/test_route_auth.py` pins the 10 new admin routes and adds the receiver to the
  public allowlist; `tests/test_compose_hardening.py` covers the new service.

## Open risks

- **The receiver is public.** It is protected by the HMAC check and the size cap, and
  Cloudflare Access is bypassed for exactly that path. It has no per-IP rate limit
  (GitHub sends bursts from many addresses); a flood of invalid requests costs one HMAC
  each and a Redis HINCRBY. Cloudflare's WAF/rate rules are the place to add one.
- **Secret rotation** means a short window of 401s: GitHub signs with one secret only.
  Update `.env` and the GitHub webhook together, then use "Redeliver" for any 401s.
- **Redaction is heuristic.** GitHub already masks registered secrets as `***`; this is a
  second net. A secret with a harmless name, under 20 characters or low entropy can slip
  through, and a random-looking value may be redacted needlessly.
- **Log hosts may change.** If GitHub moves log storage, logs answer 502
  `github_log_host_refused` until `GITHUB_LOG_HOSTS` is updated. The default is kept
  narrow on purpose.
- **A text log over `GITHUB_LOG_DOWNLOAD_MAX_BYTES`** is cut there, so its "tail" is the
  tail of the first 32 MiB (flagged truncated).
- **"prod" as a keyword** also gates names like `products-ci.yml`. That is the safe
  direction. Rename such a workflow if the approval gets in the way; the keyword rule cannot
  be turned off by configuration (D-17-3).
- **Callbacks run in the worker process** and after commit: a crash between the commit and
  the callback loses that one notification (the event is still `processed`). Phase 18 can
  also scan `github.workflow_runs` for failures it has not seen.
