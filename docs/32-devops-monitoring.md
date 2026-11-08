# DevOps monitoring for ASTRA-FRONTEND

One place to answer: **what merged, what is building, what failed, which commit is deployed,
which services are running, whether deployments are healthy, and what changed while the
dashboard was closed.** Admin only (like the GitHub data it is built from). Everything is
served from PostgreSQL: no dashboard request calls GitHub or Docker.

## What is reused, what is new

| Need | Source | New? |
|---|---|---|
| Builds (Actions runs, jobs) | `github.workflow_runs` / `workflow_jobs`, kept by webhooks + `github_actions_sync` | reused |
| Webhook intake (HMAC, dedup by delivery id, retries, backoff) | `POST /api/v1/github/webhooks`, `github.webhook_events` | reused; now also handles `deployment`, `deployment_status` |
| PRs opened / closed / merged, GitHub deployments | `devops.repo_events`, `devops.deployments` (source `github`) | new |
| Server deploys | `scripts/deploy.sh` → `deploy-poll.sh` → `deploy_record` → `devops.deployments` (source `server`) + the existing `deployer` agent run | extended |
| Runtime | host collector → `devops.services`, `devops.service_snapshots` | new |
| Live feed, notifications | `devops.events`, `devops.notification_prefs`, `devops.notification_reads` | new |

Migration `0025` creates the `devops` schema (additive; nothing existing changes).

## Four statuses, never mixed

| Field | Values | Meaning |
|---|---|---|
| PR status | `OPEN` `MERGED` `CLOSED` `UNKNOWN` | from GitHub |
| CI status | `QUEUED` `RUNNING` `SUCCEEDED` `FAILED` `CANCELLED` `UNKNOWN` | a workflow run; no progress percentage exists |
| Deployment status | `RUNNING` `SUCCEEDED` `UNVERIFIED` `FAILED` `ROLLED_BACK` `UNKNOWN` | a deploy, never inferred from a merge or a green build |
| Deployment `health` | `VERIFIED` `FAILED` `NOT_VERIFIED` `UNKNOWN` | whether the release passed its readiness check after deploying |
| Runtime status | `HEALTHY` `UNHEALTHY` `STOPPED` `STARTING` `UNKNOWN` | a compose service as the collector last saw it |

- A server deploy is `SUCCEEDED` **only** with a recorded health verification (`deploy.sh`
  writes `health_ok` after its readiness gate). A deploy whose script records no phases (the
  frontend's today) is `UNVERIFIED`, shown as a warning, never as a success.
- `ROLLED_BACK`: the release failed and the previous one is back and healthy. `FAILED` with
  `rollback_status: "unhealthy"`: the rollback is broken too; a human is needed.
- A GitHub deployment's status is what GitHub reports (`health: UNKNOWN`): Assembler does not
  verify those.
- A service the collector has not reported for `DEVOPS_RUNTIME_STALE_SECONDS` (180 s) is
  `UNKNOWN` with `stale: true`, whatever it was last.
- `RepositoryStatusOut.deployed` is the latest deployment that is `SUCCEEDED` (verified): the
  release actually running. `deployment` is the latest attempt, whatever its outcome.

## REST API (`/api/v1/devops`)

All GETs need an admin session; writes also need the `x-csrf-token` header. Lists are
`Page<T>` (`items, total, limit, offset`); `limit` ≤ 200. Time filters are ISO 8601,
`since` inclusive, `until` exclusive.

| Endpoint | Filters | Returns |
|---|---|---|
| `GET /overview` | | `OverviewOut`: recently merged PRs, running/queued builds, failures of the last 2 days (with failed jobs), latest deployment per target/environment, services + counts, collector freshness, GitHub configured/last backfill, unread count |
| `GET /repositories` | | per repo: last merged PR, latest CI on the default branch, latest deployment, last verified deployment |
| `GET /events` | `repo`, `branch`, `type` (`pr.opened`, `pr.merged`, `deployment_status`, …), `status`, `since`, `until` | normalized GitHub activity |
| `GET /builds` | `repo`, `branch`, `status` (CI status), `since`, `until` | workflow runs |
| `GET /deployments` | `repo`, `environment`, `status` (deployment status), `source` (`server`/`github`), `target`, `since`, `until` | deployments with phase timestamps |
| `GET /services` | `status` (runtime status), `host` | services |
| `GET /feed` | `after_id`, `type` (repeatable), `repo`, `limit` | `FeedOut {items, last_id, has_more}`: the polling fallback |
| `GET /stream` | `type`, `repo`, `last_event_id` | Server-Sent Events (below) |
| `GET /notifications` | `unread_only`, `before_id`, `limit` | `NotificationsOut {items (each with read), unread_count, next_before_id}` |
| `POST /notifications/read` | body `{event_ids?: int[], up_to_id?: int}` | `{unread_count}` |
| `GET/PUT /notifications/preferences` | body `{enabled, failures_only, repos: ["owner/name"], types: EventType[]}` | the preferences |

No response ever contains a token, secret, DSN or container environment.

## Live events

Feed event types: `build.started`, `build.completed`, `build.failed`, `pr.merged`,
`deployment.started`, `deployment.completed`, `deployment.failed`, `deployment.rolled_back`,
`service.unhealthy`, `service.recovered`. Each is persisted in `devops.events` with a
monotonically increasing `id`, so a client that was offline loses nothing.

- **One event per real transition.** Every event has a unique key (e.g. one "done" per run
  attempt, one `pr.merged` per PR, one `service.unhealthy` per status change), so webhook
  redeliveries, out-of-order deliveries, the backfill and repeated collector reports never
  notify twice. Facts older than `DEVOPS_EVENT_MAX_AGE_HOURS` (48) create no event, so the
  first backfill does not flood the feed.
- **Stream:** `GET /api/v1/devops/stream`. SSE event name = the type; `id:` = the feed id;
  `data:` = `EventOut` JSON. `hello` carries the starting cursor; `: ping` keeps the
  connection alive; `bye` ends it (session ended / `DEVOPS_STREAM_MAX_SECONDS`) and the
  browser reconnects. Reconnecting with `Last-Event-ID` (EventSource does it) or
  `?last_event_id=` replays everything after it, in order. Without a cursor the stream
  starts at the newest event: load history with `/feed` or `/notifications` first.
- **Polling fallback:** `GET /feed?after_id=<last_id>`; repeat at once while `has_more`.
- A database restart does not end the stream: that poll is skipped and the next one resumes
  from the same cursor.

## Notifications

Notifications are the feed filtered per user: `enabled`, `failures_only` (severity `error`),
repository subscriptions (`repos`; runtime events have no repo and are always included) and
`types`. Unread = not covered by `read_up_to` (mark-all) and not individually marked. Counts
are per user; one admin reading does not mark it read for another.

## Sources in detail

**Webhooks.** Point the GitHub webhook (repo or org) at `POST /api/v1/github/webhooks` with the
`GITHUB_WEBHOOK_SECRET` and the events *Workflow runs, Workflow jobs, Pull requests,
Deployments, Deployment statuses* (plus Pushes / Issues as before). Out-of-order delivery:
a run older than the stored one is not applied (and creates no event); an older deployment
status never moves a deployment back.

**Backfill** (`devops_backfill`, `DEVOPS_BACKFILL_CRON`, every 15 min): recent PRs and
deployments of the allowlisted repos (`DEVOPS_BACKFILL_PER_REPO`, `DEVOPS_BACKFILL_MAX_REPOS`).
Actions runs are backfilled by `github_actions_sync`, which feeds the same transitions. A
GitHub rate limit stops the run early; the next run continues. Without `GITHUB_TOKEN` the job
is not scheduled and `overview.github.configured` is false; server deploys and the runtime are
still monitored.

**Server deploys.** `scripts/deploy.sh` writes `name=timestamp` phases
(`build_started`, `build_finished`, `migrate_finished`, `deploy_started`, `deploy_finished`,
`health_ok`, `rollback_started`, `rollback_finished`) and a one-line `failure` reason to
`$DEPLOY_LOG_DIR/.deploy-backend.phases`. `deploy-poll.sh` passes them and the exit code to
`deploy_record finish --exit-code N --phases-json '{…}'`. Repo and branch come from
`DEPLOY_TARGET_REPOS`; the environment from `DEVOPS_ENVIRONMENT` (or `--environment`). Images
are built with `GIT_SHA` → OCI label `org.opencontainers.image.revision`.

**Runtime collector.** `scripts/ops/runtime-collect.sh` runs on the host every minute
(`deploy/systemd/aureon-runtime-collect.{service,timer}`, as the deploy user in group
`docker`). It reads `docker compose ps --all`, a fixed field list from `docker inspect` (id,
image id, revision label, start time; never the container environment) and
`/api/health/ready`, and pipes one `aureon.runtime/v1` JSON document into
`python -m app.workers.runtime_record` in the running API container (or a one-shot API
container when the API is down, so the outage itself is recorded). The API process gets no
Docker access and no new endpoint or token. `RUNTIME_EXPECTED_SERVICES` lists services that
must exist (a missing one is `STOPPED`); `DEVOPS_ONESHOT_SERVICES` (`mount-check`, `migrate`)
are healthy when they exited 0. The collector heartbeats the `runtime-collector` agent.

`GET /api/system/status` (Phase 22) still exists: a live read through the read-only docker
proxy, without history. The dashboard uses the collector's data instead.

Install once on the server:

```bash
sudo cp /opt/private-ai/aureon/deploy/systemd/aureon-runtime-collect.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aureon-runtime-collect.timer
```

## Retention

`devops_purge` (`DEVOPS_PURGE_CRON`, daily): feed events and repo events after
`DEVOPS_RETENTION_DAYS` (90), collector snapshots after `DEVOPS_SNAPSHOT_RETENTION_DAYS` (7),
finished deployment records after `DEVOPS_DEPLOYMENT_RETENTION_DAYS` (365). Current service
rows are kept.

## Database access

The `devops` schema is reachable only through the API and workers (the app role). Migration
`0025` grants it to every role that can already use the `github` schema, with the same kind
of access, and `deploy/sql/roles.sql` lists it for future runs. No port or network changed.
