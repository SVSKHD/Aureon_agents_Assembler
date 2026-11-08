# Live deploy progress

The Spasta `/dev/actions` "Deploys and CI" card (ASTRA-FRONTEND `CcActionsMonitor.vue`) shows
what a backend or frontend deploy is doing right now, stage by stage, with its log, from what
the deploy scripts actually report. Nothing is simulated or estimated, and a deploy is shown as
deployed only after Aureon recorded it as finished with a passed health check
(docs/deploy-automation.md, docs/32-devops-monitoring.md).

## Stages

| Stage | Backend (`scripts/deploy.sh`) | Frontend (`ASTRA-FRONTEND/scripts/deploy.sh`) |
|---|---|---|
| queued | the poller opened the deploy; waiting for the deploy lock | same |
| pulling | `git fetch` + reset to the commit | same, + `git archive` of the commit |
| validating | the checkout is the commit; `docker compose config` is valid | `pnpm install --frozen-lockfile` |
| building | `docker compose build` (the images carry the commit as their OCI revision) | `pnpm build` (format, stylelint, type-check, tests, vite) |
| migrating | `migrate` + `migrate --check` (backend only) | — |
| restarting | `docker compose up -d` | publish `releases/<sha>`, switch `current` |
| health_checking | `/api/health/ready` passes twice in a row | the smoke check through nginx |
| verifying | the running api container's image is the commit (revision label) | `current` is `releases/<sha>`, built from the commit |
| rolling_back | only after a failure that changed the running stack | only after a failed smoke check / verification |

Each stage is `pending` (not reported yet), `running`, `succeeded`, `failed` (with a sanitized
one-line error) or `skipped` (the deploy ended before it). A stage that was still `running`
when the deploy ended is recorded as `failed` ("ended without reporting the end of this
stage"), never as succeeded. The outcome of the deploy (`running`, `completed`, `unverified`,
`failed`, `rolled_back`) is the recorded deploy status (deploy_record), not a guess from the
stages. Exit codes: backend and frontend scripts both use 0 deployed, 1 failed and rolled back
healthy, 2 failed and the rollback is not healthy, 3 failed before anything changed.

## How it is reported

```
deploy.sh ──stage lines──▶ $DEPLOY_LOG_DIR/.deploy-<target>.phases
deploy-poll.sh (runs deploy.sh in the background)
   every 4 s when the phases file or deploy.log changed, at least once a minute:
     docker compose exec -T api python -m app.workers.deploy_progress   (stdin: report)
   at the end, always:
     docker compose run --rm api python -m app.workers.deploy_progress   (final report)
     docker compose run --rm api python -m app.workers.deploy_record finish ...
```

A report (`aureon.deploy.progress/v1`) carries the whole stage list, the commit the script
checked out (`sha=`), the PR number from GitHub's merge subject (`pr=`), and this deploy's own
lines of `deploy.log` (the lines tagged with the target). Because every report is complete, the
reports missed while a backend deploy restarts the API are healed by the next one; a stage only
moves forward and a finished stage never changes, so a late, older report cannot move it back.
Stages are rows in `devops.deployment_stages` (migration 0026), so they survive API restarts.

The log is scrubbed before it is stored (every credential this process knows, URL credentials,
bearer tokens, GitHub tokens, one-time ids such as `tokenId=`), capped to the last 600 lines /
128 KB, and never returned to non-admins.

## API (admin only, read-only, DB only)

The deploy ids are the monitor's existing ones (`deploy:<run id>` in `GET
/api/v1/github/actions/monitor`).

| Endpoint | Returns |
|---|---|
| `GET /api/v1/github/actions/monitor` | additive per deploy row: `stage` (running now), `stale`, `stages_completed`, `stages_total`; `status` comes from the recorded deploy (`success` only when health-verified, `unverified` otherwise) |
| `GET /api/v1/github/actions/monitor/deploys/{run_id}/progress` | `DeployProgressOut`: target, repo, branch, environment, `expected_sha` (what the poller expected), `deployed_sha` (what the script checked out and verified), `commit_verified`, `pr_number`, `outcome`, `health_verified_at`, `rolled_back`, `failure_reason`, `stale`, `current_stage`, `stages_completed` / `stages_total`, `stages[]` |
| `GET /api/v1/github/actions/monitor/deploys/{run_id}/logs` | `DeployLogOut`: `text`, `truncated`, `updated_at`, `running` |

`commit_verified` is true only when the verifying stage passed and the deployed commit is the one
the poller expected (a newer commit merged during the deploy is flagged, not hidden).
`pr_number` comes from a recorded `pr.merged` event for the commit when there is one, else from
GitHub's "Merge pull request #N" subject. `stale` is true for a running deploy whose host has not
reported for `DEVOPS_DEPLOY_STALE_SECONDS` (180 s): the progress is shown with that warning and
the deploy is never assumed to have finished.

## The dashboard

`CcActionsMonitor.vue` keeps its Deploys / CI filter and repo scope. Deploy rows can be selected;
a running deploy is selected automatically and stays selected after it finishes. Wide screens
(≥ 1024 px) show the timeline on the left and the progress stack on the right; phones and
tablets get a "View progress" bar that opens the stack in a bottom sheet. The stack shows each
stage, a progress bar of finished / planned stages, the commit check, the PR link, and the log
(LogViewer). Polling: every 4 s while anything deploys, every 30 s when idle, paused while the
tab is hidden. When a deploy that was running finishes, a notice says whether it deployed,
failed or was rolled back.
