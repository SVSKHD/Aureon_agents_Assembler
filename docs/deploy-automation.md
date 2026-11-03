# Deploy automation

Every merge to `main` (this repo) or `dev01` (ASTRA-FRONTEND) is built and deployed on the
Spasta server within about a minute, with no inbound ports and no GitHub runner on the box.

## How it works

```
GitHub (merge)                 server (pulls; nothing pushes to it)
  main / dev01  <── git ls-remote, every 60 s ── aureon-deploy.timer
                                                   └─ scripts/ops/deploy-poll.sh
                                                        ├─ backend:  scripts/deploy.sh
                                                        └─ frontend: ASTRA-FRONTEND/scripts/deploy.sh
```

- **Pull, not push.** `aureon-deploy.timer` (systemd, user `svskhd8`, group `docker`) runs
  `scripts/ops/deploy-poll.sh` every minute. It reads each branch head with `git ls-remote`
  (both repos are public: no credentials on the server) and compares it with the last good
  deploy (`/mnt/spasta-data/logs/.deployed-{backend,frontend}`). A moved branch runs that
  repo's `scripts/deploy.sh`.
- **Why no self-hosted runner.** Both repos are public. A runner on the server would run as a
  docker-group user (root-equivalent, can read `.env`), and a fork PR can edit the workflow
  file to target it. With the poller, only code that is already merged runs on the server.
- **CI** (`.github/workflows/ci.yml` in both repos) runs lint and tests on GitHub-hosted
  `ubuntu-latest`, on every pull request and push. It deploys nothing.
- **Deploy history in Spasta.** Each deploy is an agent run of the `deployer` agent
  (`python -m app.workers.deploy_record`, run in a one-shot api container): task
  `deploy:backend` / `deploy:frontend`, `details` = target, SHA, previous SHA, log path,
  `rolled_back`. It shows in the agent monitor (`GET /api/agents`). Recording is best effort:
  if the DB or image is unavailable the deploy still runs (logged as "not recorded").

### Backend: `scripts/deploy.sh`

1. `flock` on `/mnt/spasta-data/logs/.deploy-backend.lock` (a second deploy waits).
2. `git fetch` + `git reset --hard origin/main`. Untracked server files (`.env`,
   `.env.migrate`, `compose.override.yaml`, `scripts/ops/ops.env`) are never touched; local
   edits to tracked files are stashed under a named stash first.
3. `aureon-agentic:dev` (and `aureon-coding:dev`) are tagged `:rollback`.
4. `docker compose build`, `docker compose run --rm migrate`, `docker compose up -d`.
5. Up to 60 s for `GET /api/health/ready` = 200 (Postgres, Redis, Ollama, HDD all ok).
6. On failure: `:rollback` → `:dev`, `git reset --hard <previous SHA>`, `up -d`, exit 1. The
   failed SHA is remembered (`.failed-backend`) and not retried until the next commit.

Migrations are additive (project rule), so the previous image runs on the new schema. Deploys
never touch Postgres data, `.env` or `TRADING_LIVE_ENABLED` / `PHASE_G_APPROVED`.

### Frontend: `ASTRA-FRONTEND/scripts/deploy.sh`

1. `flock`, `git fetch` + `git reset --hard origin/dev01` of `/srv/projects/ASTRA-FRONTEND`.
2. A clean `git archive` of that commit, plus the server-local `.env.production`, is built in a
   `node:22-alpine` container as `svskhd8`: `pnpm install --frozen-lockfile && pnpm build`
   (which runs `verify`: format, stylelint, type-check, vitest). No Node on the host.
3. `dist/` → `/srv/www/spasta/releases/<sha>` (copied to `<sha>.tmp`, then `mv`).
4. Atomic swap: a new symlink renamed over `/srv/www/spasta/current` (`mv -T`).
5. `sudo -n /usr/sbin/nginx -s reload` (one sudoers line; nginx serves the new files without
   it, so a missing entry is only a warning).
6. Smoke check through nginx (`Host: spasta.online` on 127.0.0.1:8088): the live
   `index.html` must be the new one, else the old symlink comes back and it exits 1.
7. The newest 5 releases are kept.

## One-time install (sudo)

```bash
# nginx reload without a password, for the frontend swap (exactly this command, nothing else):
echo 'svskhd8 ALL=(root) NOPASSWD: /usr/sbin/nginx -s reload' | sudo tee /etc/sudoers.d/spasta-deploy
sudo chmod 440 /etc/sudoers.d/spasta-deploy && sudo visudo -cf /etc/sudoers.d/spasta-deploy

# the poller
sudo cp /opt/private-ai/aureon/deploy/systemd/aureon-deploy.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now aureon-deploy.timer
systemctl list-timers aureon-deploy.timer
```

## Operating it

| What | How |
| --- | --- |
| Watch | `tail -f /mnt/spasta-data/logs/deploy.log`, `journalctl -u aureon-deploy -f`, Spasta agent monitor → `deployer` |
| Deploy now (backend) | `/opt/private-ai/aureon/scripts/deploy.sh` (`--force` to redeploy the same commit) |
| Deploy now (frontend) | `/srv/projects/ASTRA-FRONTEND/scripts/deploy.sh` (`--force` likewise) |
| Poll now | `sudo systemctl start aureon-deploy.service` |
| Pause | `touch /mnt/spasta-data/logs/DEPLOY_PAUSED` (no sudo), or `sudo systemctl stop aureon-deploy.timer` |
| Resume | `rm /mnt/spasta-data/logs/DEPLOY_PAUSED`, or `sudo systemctl start aureon-deploy.timer` |
| Roll back frontend | `/srv/projects/ASTRA-FRONTEND/scripts/deploy.sh --rollback` (previous release; instant) |
| Roll back backend | revert the commit on GitHub and merge (deploys in ~1 min); urgently: `touch .../DEPLOY_PAUSED`, then `git -C /opt/private-ai/aureon reset --hard <good sha> && docker tag aureon-agentic:rollback aureon-agentic:dev && docker compose up -d` |
| Retry a failed commit | `rm /mnt/spasta-data/logs/.failed-<target>` or run its `deploy.sh --force` |

A rollback by hand leaves `origin` ahead of the server. The poller does not redeploy the same
commit (it compares with the last *deployed* SHA), but the next merge deploys everything up to
it, bad commit included, so revert it on GitHub as well.
