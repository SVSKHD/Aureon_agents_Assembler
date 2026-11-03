# Manual setup

These steps need the owner: sudo on the server, real secrets, or outside accounts. They are
never run by an agent. Each phase appends its own section below. Run commands as `svskhd8` on
the server, from `/opt/private-ai/aureon`, unless a step says otherwise.

## This server (found on the first deploy, 2026-11-03)

The steps below assume a slightly different box. On the Spasta server:

- **Paths.** The checkout is `/srv/projects/Aureon_agents_Assembler`; `/opt/private-ai/aureon`
  is a symlink to it, so every path below still works. The frontend checkout is
  `/srv/projects/ASTRA-FRONTEND`; the site is served from `/srv/www/spasta/current`.
- **Docker access.** `svskhd8` must be in the `docker` group (`sudo usermod -aG docker
  svskhd8`, then log in again).
- **sudo.** Run sudo steps in a normal SSH terminal. Claude Code's `!` prefix has no TTY, so
  sudo cannot ask for the password there.
- **Postgres** runs from `/opt/private-ai/compose.postgres.yaml` (container
  `private-ai-postgres-1`, superuser `spasta`, there is no `postgres` role) on the internal
  network `private-ai_spasta_internal`; Ollama is on `private-ai_ai_private`. The untracked
  `compose.override.yaml` joins the DB-using services to the Postgres network
  (`SPASTA_DB_NETWORK` in `.env`); `PRIVATE_AI_NETWORK=private-ai_ai_private`. Postgres needs
  `restart: unless-stopped` for the reboot drill. `scripts/ops/ops.env`: `PG_USER=spasta`.
- **First migration on an existing database** runs as the owner of the legacy `public.*`
  tables (`spasta`), because 0010 adds columns to them: put the owner URL in `.env.migrate`,
  `docker compose run --rm migrate`, re-run `scripts/ops/apply-db-roles.sh` (the new schemas
  move to `aureon_migrator`), then switch `.env.migrate` to `aureon_migrator`.
- **HDD permissions.** The API (uid 10001) must be able to write `DATA_ROOT` itself (prod
  check): `sudo chgrp 10001 /mnt/spasta-data && sudo chmod 775 /mnt/spasta-data`.
- **Nginx** is the host package (`apt install nginx`), not the frontend container. Enable
  `deploy/nginx/spasta-online.conf` and `deploy/nginx/api-spasta-online.conf`; both listen on
  `127.0.0.1:8088`, where cloudflared sends both hostnames.
- **cloudflared** runs with `--token-file` (a dashboard-managed tunnel): set the public
  hostnames in Zero Trust → Networks → Tunnels → the tunnel → Public Hostname, not in
  `/etc/cloudflared/config.yml`.
- **The acceptance list** is in [24-production-integration.md](24-production-integration.md)
  (there is no `22-production.md`).
- **Deploys after this one are automatic**: [deploy-automation.md](deploy-automation.md).

## A, C, D — finance, image agent, frontend

1. **Finance (A).**
   - **Check the live DDL first.** Before `alembic upgrade 0010`, dump the live tables and
     compare them with ASTRA-FRONTEND `supabase/migrations` (decision D-A1):
     ```bash
     docker exec <postgres-container> pg_dump -U <user> -d spasta --schema-only \
       -t public.astra_documents -t public.trades -t public.expenses \
       -t public.secured -t public.portfolio_profiles
     ```
   - **Migrate:** `docker compose run --rm migrate`. It only adds things: the
     `aureon_user_id` column, its indexes and `core.finance_identities`.
   - **Claim the existing rows.** They stay invisible until you do. Run the claim once per
     legacy owner id, as a dry run first, then with `"apply": true`:
     ```bash
     curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' \
       $B/api/finance/claim -d '{"legacy_user_id":"<uid>","user_id":"<aureon user id>"}'
     ```
     Find the legacy ids with `select distinct user_id from public.trades;`.
2. **Image agent (C).** Point the worker at the real Stable Diffusion entrypoint, per
   docs/04-imaging.md:
   - set `IMAGE_GENERATOR=subprocess` and `IMAGE_AGENT_COMMAND=[...]` (a JSON argv with a
     `{output}` placeholder) in `.env`
   - start it with `docker compose --profile imaging up -d imaging-worker`

   Until then only the stub generator runs.
3. **Frontend (D, 21).**
   - **Netlify:** the build defaults are now `VITE_AUTH_PROVIDER=aureon` and
     `VITE_AI_BACKEND=aureon`. Aureon is reached same-origin through nginx on spasta.online.
     Netlify-hosted deploys and previews cannot sign in with Aureon's SameSite=Strict cookies,
     so set `VITE_AUTH_PROVIDER=supabase` and `VITE_AI_BACKEND=proxy` in the Netlify site
     environment if those builds should keep working.
   - **Paper fills:** after deploying, try one paper approval. Approve now asks for the
     admin password, or it files a request that you approve in `/dev` → Inbox.

## D-backend — Frontend support APIs

- **Migration:** run `alembic upgrade 0011` (additive: `core.finance_tombstones`,
  `core.finance_attachments`).
- **HDD directory:** `${DATA_ROOT}/finance-attachments/` is created on first upload. The API
  container's user needs write access to `DATA_ROOT`, the same as for imaging. If the mount is
  root-owned, create the directory and `chown` it to the container user (sudo).
- **Proxy body size:** set the reverse proxy / Cloudflare request body limit to about
  `FINANCE_ATTACHMENT_MAX_BYTES` + 1 MiB (default 11 MiB), so oversize uploads stop at the edge.
- **News feeds (optional, off by default):** set `NEWS_FEED_URLS` to a one-line JSON list of
  RSS/Atom URLs, e.g. `NEWS_FEED_URLS=["https://example.com/fx.rss"]`. Restart the scheduler, so
  the `news-pull` job is created. Outbound HTTPS from the scheduler container must be allowed.
- **Frontend:** create shares with unguessable doc ids (UUIDs) in namespace `aureon-shares`.

## Phase E — production hardening

Runbook: [docs/16-production-hardening.md](16-production-hardening.md). Security review:
[docs/17-security-review.md](17-security-review.md).

### Sudo steps for the owner

Run these in order. Each step is copy-pasteable, and each one is safe to re-run.

0. **Prepare the HDD folders.** Redis now runs as uid 999 with no capabilities, so its data dir
   must be owned by that uid. Backups go on the HDD.

   ```bash
   findmnt /mnt/spasta-data && test -f /mnt/spasta-data/.spasta-data-mounted && echo HDD-OK
   sudo chown -R 999:999 /mnt/spasta-data/redis
   sudo install -d -o svskhd8 -g svskhd8 -m 700 /mnt/spasta-data/backups /mnt/spasta-data/backups/aureon
   ```

1. **Install the stack unit**, which runs `docker compose up -d` after `docker.service` and the
   HDD mount.

   ```bash
   sudo cp deploy/systemd/aureon.service /etc/systemd/system/
   # Optional: opt-in workers, e.g. Dhan + research:
   #   sudo systemctl edit aureon.service   ->   [Service]
   #                                            Environment=COMPOSE_PROFILES=dhan,research
   sudo systemctl daemon-reload
   sudo systemctl enable aureon.service
   sudo systemctl start aureon.service && systemctl status aureon --no-pager
   journalctl -u aureon -b --no-pager | tail -30      # ends with "healthcheck: all good"
   ```

2. **Turn on the backup timer** (nightly at 02:17 IST) and the **restore-drill timer** (Sunday
   04:23 IST).

   ```bash
   cp scripts/ops/ops.env.example scripts/ops/ops.env && chmod 600 scripts/ops/ops.env
   nano scripts/ops/ops.env      # PG_SERVICE / PG_COMPOSE_DIR or PG_CONTAINER (docs/00-inspection.md §1)
   scripts/ops/backup.sh --dry-run
   sudo cp deploy/systemd/aureon-backup.{service,timer} deploy/systemd/aureon-restore-drill.{service,timer} /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now aureon-backup.timer aureon-restore-drill.timer
   sudo systemctl start aureon-backup.service && journalctl -u aureon-backup --no-pager | tail
   sudo systemctl start aureon-restore-drill.service && journalctl -u aureon-restore-drill --no-pager | tail -30
   systemctl list-timers 'aureon-*'
   ```

3. **Log rotation.** Container logs are already capped per service in compose (10 MB × 5).
   This step caps the journal, which holds the systemd units, backups and drills.

   ```bash
   sudo install -d /etc/systemd/journald.conf.d
   sudo cp deploy/host/journald-aureon.conf /etc/systemd/journald.conf.d/aureon.conf
   sudo systemctl restart systemd-journald
   journalctl --disk-usage
   ```

4. **Docker daemon log limits** apply to every container on the box, including the existing
   private-ai stack. `live-restore` keeps containers running while dockerd restarts. Merge by
   hand if `/etc/docker/daemon.json` already exists.

   ```bash
   sudo test -f /etc/docker/daemon.json && sudo cat /etc/docker/daemon.json   # merge if present
   sudo cp deploy/host/docker-daemon.json /etc/docker/daemon.json             # only if absent
   sudo systemctl restart docker      # restarts containers that lack live-restore; do it in a quiet window
   docker info --format '{{.LoggingDriver}} {{.LiveRestoreEnabled}}'
   ```

   New limits apply to containers created after the restart. To recreate the Aureon ones, run
   `docker compose up -d --force-recreate`.

5. **Firewall: allow only 22/80/443 in.** Check your SSH session is on port 22 first.

   ```bash
   sudo ufw status verbose            # note existing rules (e.g. a LAN :8088 rule) before changing
   sudo bash deploy/host/ufw-rules.sh # default deny in, allow 22/80/443, enable
   ss -tlnp | grep -vE '127\.0\.0\.1|::1'   # what is still listening publicly
   ```

   Docker-published ports bypass ufw. That is why compose publishes only `127.0.0.1:8000`, and
   why the private-ai stack must not publish Postgres or Ollama (docs/00-inspection.md §1). If
   the LAN frontend on `:8088` must stay reachable, run `sudo ufw allow from 192.168.29.0/24 to
   any port 8088 proto tcp`.

6. **Unattended security upgrades.**

   ```bash
   sudo apt-get update && sudo apt-get install -y unattended-upgrades
   sudo dpkg-reconfigure -plow unattended-upgrades       # answer "Yes"
   cat /etc/apt/apt.conf.d/20auto-upgrades              # both lines = "1"
   sudo unattended-upgrade --dry-run --debug | tail -5
   ```

   Leave `Unattended-Upgrade::Automatic-Reboot` off. The owner reboots after checking the stack
   with `scripts/ops/healthcheck.sh`.

7. **Nginx security headers** (Phase E). Add the line below at **server** level of the existing
   frontend server block, next to the `aureon-api.conf` include:

   ```nginx
   include /opt/private-ai/aureon/deploy/nginx/security-headers.conf;
   ```

   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   curl -sI https://spasta.online/ | grep -iE 'strict-transport|content-security|x-frame'
   curl -s -o /dev/null -w '%{http_code}\n' https://spasta.online/api/health/ready   # 404 from outside
   ```

   Open the SPA and check the browser console for CSP violations. If the bundle loads anything
   from a CDN, add that origin to the matching directive.

8. **Image CVE scan (optional, recommended).** Install Trivy from its apt repo, then run:

   ```bash
   trivy image --severity HIGH,CRITICAL aureon-agentic:${IMAGE_TAG:-dev}
   trivy image --severity HIGH,CRITICAL redis:7.4-alpine
   ```

### Secrets

Every secret lives only in `/opt/private-ai/aureon/.env` (`chmod 600`, owner `svskhd8`, never
committed). `scripts/ops/ops.env` holds no secrets. After changing `.env`, run
`docker compose up -d` to recreate the containers.

| Variable | Secret part | How to generate / obtain |
| --- | --- | --- |
| `DATABASE_URL` | the role password | Use a dedicated least-privilege role, not the superuser. Generate the password with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`, then in the Postgres container run `CREATE ROLE aureon LOGIN PASSWORD '…'; GRANT CREATE ON DATABASE spasta TO aureon;`. Percent-encode `@ : / %` if present. |
| `SECRET_KEY` | all of it | `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Prod refuses the placeholder, anything under 32 chars, and low-entropy values. Rotating it invalidates CSRF tokens, so users sign in again. |
| `DHAN_ACCESS_TOKEN` | all of it | web.dhan.co → DhanHQ Trading APIs → generate the 24 h token. Paste it into `.env` each day (docs/11-dhan.md). |
| `DHAN_CLIENT_ID` | identifier (treat as sensitive) | Shown on web.dhan.co next to the token. |
| `MT5_STATUS_TOKEN` | all of it | The MT5 bot's `AUREON_STATUS_TOKEN` (read-only status token), never its console token (docs/12-mt5-adapter.md). |
| `PHASE_G_APPROVED` | not a secret: a sign-off | Leave empty. Only the owner sets `yes`, together with `TRADING_LIVE_ENABLED=true`, after Phase G ships. LIVE stays blocked until then anyway. |
| Admin password | not in `.env` | Typed interactively: `docker compose run --rm -it migrate python -m app.auth.bootstrap --email you@example.com`. Use ≥ 16 chars from a password manager. |

Check these too:

- `COOKIE_SECURE=true`.
- `CORS_ORIGINS=https://spasta.online`. Drop the http LAN origin in prod: secure cookies cannot
  work over http.
- `DEBUG` and `API_DOCS_ENABLED` are unset.
- `FORWARDED_ALLOW_IPS` is the Docker bridge gateway (deploy/README.md).

## 15 — GitHub foundation

See [18-github-foundation.md](18-github-foundation.md). Without these steps the integration
stays off: `GET /api/v1/github/connection` says `configured: false` and the other GitHub
endpoints answer 503 `github_not_configured`.

1. **Create a fine-grained personal access token.** On GitHub, go to Settings → Developer
   settings → Personal access tokens → Fine-grained tokens → Generate new token.
   - **Resource owner:** your user, or the organisation that owns the repos (an org may
     have to approve fine-grained tokens first).
   - **Expiration:** 90 days at most. Put a calendar reminder a week before it expires.
     Rotating means a new token in `.env` plus a restart. The old connection row stays as
     history under its own fingerprint.
   - **Repository access:** "Only select repositories". Pick exactly the repos Aureon should
     see (e.g. `Aureon_agents_Assembler` and the frontend repo), never "All repositories".
   - **Repository permissions:**
     - Metadata: **Read** (mandatory)
     - Contents: **Read and write**
     - Pull requests: **Read and write**
     - Issues: **Read and write**
     - Actions: **Read and write**
     - Workflows: **Read and write**, only if workflows will be dispatched or edited (Phase 17);
       otherwise leave it at no access
     - Everything else: no access. This phase only reads; the write permissions are for
       Phases 16–17, so they are asked for once.
   - **Account permissions:** none.
2. **Put it on the server only:** in the backend `.env` next to `compose.yaml`
   (`chmod 600 .env`):
   ```
   GITHUB_TOKEN=github_pat_...
   GITHUB_ALLOWED_OWNERS=["SVSKHD"]
   # optional, an exact list:
   GITHUB_ALLOWED_REPOS=["SVSKHD/Aureon_agents_Assembler"]
   ```
   Never put it in the frontend: no `VITE_*` variable, nothing in the browser, nothing in Git.
   The frontend talks only to `/api/v1/github/*`, which never returns the token.
3. **Restart:** run `docker compose up -d api scheduler`, then, as an admin,
   `POST /api/v1/github/connection/check`. Expect `ok: true` and your login. Then
   `GET /api/v1/github/repositories?refresh=true`.
4. **If it leaks** (it shows up in a log, a paste or a commit): revoke it on GitHub
   immediately, create a new one and restart. Every log line and error is redacted, but
   revoking is the only real fix.

## 16 — GitHub issues and PRs

See [19-github-issues-prs.md](19-github-issues-prs.md). Phase 16 needs nothing beyond
Phase 15's token, except one extra permission for the gated repo-config operations.

1. **Add Administration: Read and write to the fine-grained PAT** (GitHub → Settings →
   Developer settings → Fine-grained tokens → the token → Edit → Repository permissions).
   Branch protection (`PUT`/`DELETE …/branches/{b}/protection`), repo settings (`PATCH
   /repos/{o}/{r}`) and Actions enable/disable (`PUT …/actions/permissions`) need it.
   Without it those approvals end `failed` with `github_forbidden`. Nothing else breaks.
   Issues, Pull requests and Contents read/write (already asked for in Phase 15) cover
   issues, comments, reviews, PR create/update, merge, force-push and branch delete.
2. **Apply migration 0013:** `docker compose run --rm migrate`, then `docker compose up -d api`.
3. **Pick the self-approval policy.** With a single owner, the default
   (`APPROVALS_ALLOW_SELF_APPROVAL=true`) lets the only admin approve their own requests.
   When a second admin account exists, self-approval stops automatically. Set it to
   `false` in `.env` to always need a second person.
4. **First run:** try the gated merge on a throwaway PR (see the doc's deploy steps) and
   check that `GET /api/v1/github/operations` shows `pr.merge` going from
   `pending_approval` to `succeeded`.
5. **Branch protection on GitHub stays the real backstop.** Keep the `main` protection rules
   enabled on GitHub itself. The approval gate controls what Aureon may do; it does not
   replace GitHub's own rules.

## 17 — GitHub Actions and webhooks

See [20-github-actions-webhooks.md](20-github-actions-webhooks.md).

1. **PAT permissions:** add **Actions: Read and write** to the fine-grained PAT (GitHub →
   Settings → Developer settings → Fine-grained tokens → the token → Edit → Repository
   permissions). Read covers workflows, runs, jobs and logs; write covers dispatch, re-run
   and cancel. Without write, those answer 403 `github_forbidden` and are tracked `failed`.
2. **Webhook secret:** `openssl rand -hex 32`, then put it in the server's `.env` as
   `GITHUB_WEBHOOK_SECRET=…`. Never commit it. Restart the API (`docker compose up -d api`).
3. **Create the repo webhook** (repo → Settings → Webhooks → Add webhook), for each
   allowlisted repo (or once at the org level):
   - Payload URL: `https://api.spasta.online/api/v1/github/webhooks` (through nginx's
     `/api/` location; its 2 MB `client_max_body_size` is above the 1 MB app cap)
   - Content type: **application/json** (form-encoded is refused with 415)
   - Secret: the value from step 2
   - SSL verification: enabled
   - Events: "Let me select individual events": **Workflow runs, Workflow jobs, Pull
     requests, Issues, Pushes**
   - Active: yes. GitHub sends a `ping`; Recent Deliveries should show **202**.
4. **Cloudflare Access bypass for exactly that path.** GitHub cannot do Access auth. Zero
   Trust → Access → Applications → the `api.spasta.online` application → add a policy, or a
   separate self-hosted application for `api.spasta.online/api/v1/github/webhooks`, with
   action **Bypass** and include **Everyone**. Scope it to that single path, nothing
   broader (not `/api/v1/github/*`). Optionally narrow it with GitHub's hook IP ranges from
   `https://api.github.com/meta` (`hooks`), and add a Cloudflare rate-limiting rule for
   that path.
5. **Allowlists in `.env`:**
   - `GITHUB_DISPATCH_ALLOWLIST=["OWNER/REPO:ci.yml", …]` for the workflows Aureon may
     dispatch or re-run (empty = none).
   - `GITHUB_PROD_WORKFLOWS=["Deploy", "release.yml"]` for production workflows whose name
     or file does not already contain `deploy` / `prod`. Those need an approval.
6. **Apply migration 0014 and start the worker:** `docker compose run --rm migrate`,
   `docker compose up -d api scheduler`, then
   `docker compose --profile github up -d github-webhook-worker`. Check
   `docker compose ps github-webhook-worker` is healthy and `GET /api/agents` lists
   `github-webhooks`.
7. **Secret rotation:** change `.env` and the GitHub webhook secret together, restart the
   API, and redeliver any 401 deliveries from GitHub's Recent Deliveries page.

## 18 — Self-repair

Detection and recovery need nothing extra. Repairs stay disabled (tasks `queued` with the
reason) until all of this is done. See [21-self-repair.md](21-self-repair.md).

1. **Check RAM first.** A 7B coder model in Q4 needs about 5–6 GB of RAM while loaded. The
   coding worker also runs the tests (`mem_limit: 3g`). Check with `free -h` on the host and
   `docker exec <ollama> ollama ps`. Don't run it next to another large model: Ollama keeps
   a model loaded for a few minutes after use.
2. **Pull the model** into the existing Ollama container (no external API is involved):
   `docker exec <ollama-container> ollama pull qwen2.5-coder:7b`, then
   `docker exec <ollama-container> ollama list`.
3. **Configure `.env`:**
   - `REPAIR_MODEL=qwen2.5-coder:7b`
   - `REPAIR_REPO=<the CODE_REPOS entry, e.g. aureon-agentic>`. That entry must list
     allowlisted `tests` (e.g. `[".venv/bin/python","-m","pytest","-q","-m","not db"]`) and
     usually `"link": [".venv"]`.
   - `REPAIR_GITHUB_REPO=SVSKHD/Aureon_agents_Assembler`, which must also be in
     `GITHUB_ALLOWED_REPOS` / `GITHUB_ALLOWED_OWNERS`
   - optionally `GITHUB_DISPATCH_ALLOWLIST` entries so failed CI runs can be re-run once
4. **PAT permissions.** The fine-grained `GITHUB_TOKEN` needs **Contents: Read and write**
   (blobs, trees, commits and the `repair/*` ref) and **Pull requests: Read and write** on
   the repair repo. Keep it limited to the repos Aureon manages. The coding worker holds this
   token, so give it nothing more.
5. **Protect the base branch** on GitHub (require a PR review), so a repair PR can only land
   through review plus the approval-gated merge.
6. **Network.** The coding worker reaches GitHub's API over its `private-ai` network, so that
   network needs outbound HTTPS. Check with
   `docker exec aureon-coding python -c "import urllib.request;print(urllib.request.urlopen('https://api.github.com').status)"`.
7. **Apply and start:** `docker compose run --rm migrate` (0016),
   `docker compose up -d api scheduler`, `docker compose --profile coding up -d coding-worker`.
   `GET /api/v1/repairs/status` should say `enabled: true`.
8. **Container restarts stay manual.** The docker-proxy is read-only by design. If a
   `docker` incident says `manual`, restart the container yourself
   (`docker compose restart <svc>`) and resolve the incident.

## 19 — Claude review

See [22-claude-review.md](22-claude-review.md). Optional: without these steps every review is
REVIEW_UNAVAILABLE and nothing is sent anywhere.

1. **Install the Claude CLI where the API runs.** This is not added to the image by default.
   - **On the host** (when the API runs on the host), as an admin: install Node 18+ and then
     `npm install -g @anthropic-ai/claude-code` (or the vendor's native installer). Set
     `CLAUDE_CLI_PATH` to the absolute path if the service user's PATH does not include it.
   - **In the API image** (the usual compose setup): add an optional build arg to the
     `runtime` stage of the Dockerfile, for example:
     ```dockerfile
     ARG INSTALL_CLAUDE_CLI=false
     USER root
     RUN if [ "$INSTALL_CLAUDE_CLI" = "true" ]; then \
           apt-get update && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
           && npm install -g @anthropic-ai/claude-code && rm -rf /var/lib/apt/lists/*; fi
     USER aureon
     ```
     Then build with `docker compose build --build-arg INSTALL_CLAUDE_CLI=true api`. Pin the
     CLI version (`@anthropic-ai/claude-code@<version>`) and re-check `claude --help` after
     upgrades (flags in `CLAUDE_CLI_ARGS`).
   - The `aureon` user has no home directory. The CLI needs a writable `HOME` for its config:
     give the api service `HOME=/var/lib/aureon-claude` plus a small named volume mounted
     there (owned by UID 10001), or a tmpfs if you authenticate by env var only. The API
     container's network must allow outbound HTTPS to the vendor's API.
2. **Authenticate non-interactively, as the service user.** Pick one:
   - an OAuth token: run `claude setup-token` once on a machine with a browser, store the
     token in the server's `.env` as `CLAUDE_CODE_OAUTH_TOKEN=…`, and set
     `CLAUDE_CLI_ENV_PASSTHROUGH=CLAUDE_CODE_OAUTH_TOKEN`
   - an API key: put `ANTHROPIC_API_KEY=…` in `.env` and set
     `CLAUDE_CLI_ENV_PASSTHROUGH=ANTHROPIC_API_KEY`
   - an interactive login stored in `$HOME/.claude` of the service user (run `claude` once
     as that user, inside the container with the HOME volume above); no passthrough needed

   Only the names in `CLAUDE_CLI_ENV_PASSTHROUGH` reach the CLI. Names like `GITHUB_*`,
   `DATABASE_*`, `SECRET*` and `REDIS*` are always refused. Never commit the token.
3. **Verify** as the service user: `docker compose exec api claude --version` (or
   `sudo -u <service-user> claude --version` on the host) must print a version and exit 0.
   A real check: `echo 'Reply with {"ok":true}' | docker compose exec -T api claude -p
   --output-format json --tools ""` should print a JSON result.
4. **Enable it:** `CLAUDE_REVIEW_ENABLED=true` in `.env`, then `docker compose run --rm migrate`
   (0015) and `docker compose up -d api`. Availability is cached for 5 minutes, so restart
   the API after fixing the CLI to see the change at once.
5. **Know the data-classification policy** before enabling it:
   - nothing is sent for a PR that touches secrets, infra, trading/risk/auth paths, or that
     contains secret-shaped strings (keys, tokens, `password=`, `.env` values, high-entropy
     literals). Those reviews come back BLOCKED with the reasons.
   - emails, IPv4 addresses and `*.spasta.online` host names are masked in what is sent. Add
     other internal domains to `CLAUDE_REVIEW_INTERNAL_DOMAINS`.
   - diffs over `CLAUDE_REVIEW_MAX_DIFF_BYTES` (200 KiB) are never sent.
   - the review is advisory: it is published only as a COMMENT, and merges still need an
     admin approval with the password re-entered.

## 20 — Approval engine

See [23-approval-engine.md](23-approval-engine.md). Nothing is required: without these steps
the engine works with the seeded policies and no notifications.

1. **Discord webhook (optional).** In Discord: the channel → Edit Channel → Integrations →
   Webhooks → New Webhook → name it (e.g. "Spasta approvals") → Copy Webhook URL. Use a private
   channel only the owner can read. Put it in the server's `.env` (never in Git):
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>
   PUBLIC_APP_URL=https://spasta.online
   ```
   then `docker compose up -d api scheduler`. File a test approval (e.g. the paper approve
   with no body) and check the message arrives and its link opens
   `https://spasta.online/dev/approvals/<id>`. Treat the webhook URL as a secret: anyone with
   it can post to the channel (but never approve anything). To rotate, delete the webhook in
   Discord and create a new one.
2. **Policy tuning.** `GET /api/v1/approvals/policies` lists them; `PUT` upserts by
   `kind_pattern` with your password:
   ```bash
   curl -s -b jar -H "x-csrf-token: $CSRF" -H 'content-type: application/json' -X PUT \
     https://api.spasta.online/api/v1/approvals/policies -d '{"password":"…","policies":[
       {"kind_pattern":"github.pr.merge","required_approvals":1,"reauth_max_age_seconds":300,
        "risk_level":"high"}]}'
   ```
   Guidance:
   - The most specific pattern wins (exact > longer literal > fewer wildcards). Add a narrow
     pattern rather than loosening `*` or `github.*`.
   - `required_approvals` > 1 only once there are that many active admins; otherwise requests
     can only expire. With a single owner keep 1.
   - `reauth_max_age_seconds` lets a recent `POST /api/v1/approvals/reauth` stand in for the
     password (≤ 3600, capped by `APPROVALS_REAUTH_STAMP_MAX_SECONDS`). Keep 0 for
     production deploys, force-push, repo settings and `repair.*`.
   - `require_reauth=false` means the admin session alone approves — avoid it.
   - `expiry_seconds` short for things that go stale (paper intents: 900 s).
   - `notify_channels: []` silences a noisy kind.
   - Edits apply to new requests only; deny pending ones to re-file them under new rules.
   - Disable (`"enabled": false`) instead of deleting; every change is in
     `GET /api/v1/audit/events?event_type=approval_policies`.
3. **Voice (optional).** To let the local model resolve phrases the grammar misses, pull a
   small model on the Ollama box and set `VOICE_OLLAMA_MODEL=<model>`; otherwise leave it
   empty (grammar only).

## 22 — Production integration

See [24-production-integration.md](24-production-integration.md). In order:

1. **Scoped DB roles (needs the Postgres superuser, inside its container).**
   ```bash
   scripts/ops/backup.sh                                  # first
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # x3: app, migrator, readonly
   scripts/ops/apply-db-roles.sh --dry-run
   scripts/ops/apply-db-roles.sh                          # prompts; or AUREON_*_DB_PASSWORD env
   ```
   Then switch the URLs (passwords in a password manager, never in Git):
   - `.env`: `DATABASE_URL=postgresql+asyncpg://aureon_app:<pw>@<postgres-container>:5432/spasta`
   - new `.env.migrate` (`chmod 600`): `MIGRATIONS_DATABASE_URL=postgresql+asyncpg://aureon_migrator:<pw>@<postgres-container>:5432/spasta`
   - `scripts/ops/ops.env`: `AUREON_MIGRATOR_ROLE=aureon_migrator` (rollback drill)
   - `docker compose up -d`, then `GET /api/v1/system/integrations` → `database.scoped: true`.
   - Re-run `scripts/ops/apply-db-roles.sh` (empty passwords = keep) after every deploy that
     adds tables. Backups/analytics may use `aureon_readonly`.
   - If the old app role was the owner of `public.trades` etc., nothing changes for it.
2. **Rollback drill (once now, then before risky upgrades):**
   `scripts/ops/rollback-drill.sh --steps 3` → report in `$BACKUP_DIR/drills/rollback-*.json`.
3. **Cloudflare Tunnel + Access** (dashboard + sudo): follow
   [deploy/cloudflare/README.md](../deploy/cloudflare/README.md) steps 1–8: tunnel routes for
   both hostnames, the Access applications (owner email allowlist, 24 h sessions, the
   `aureon-ops` service token, Bypass only for `api.spasta.online/api/v1/github/webhooks`
   and `*/api/public`), `/etc/cloudflared/config.yml` from `config.yml.example`
   (`sudo cloudflared tunnel ingress validate && sudo systemctl restart cloudflared`), and the
   nginx block:
   ```bash
   sudo ln -s /opt/private-ai/aureon/deploy/nginx/api-spasta-online.conf /etc/nginx/sites-enabled/
   sudo nginx -t && sudo systemctl reload nginx
   ```
   Optionally `.env`: `CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com`,
   `CF_ACCESS_AUD=<web aud>,<api aud>`, then `docker compose up -d api`. Point the GitHub
   webhook at `https://api.spasta.online/api/v1/github/webhooks`.
4. **Enable the alerts.** `DISCORD_WEBHOOK_URL` (Phase 20) must be set; keep
   `ALERTS_CRON=*/5 * * * *` (or your cadence), `docker compose up -d scheduler`. Check
   `GET /api/scheduler/jobs` lists `alerts-dispatch`, and run it once with
   `POST /api/scheduler/jobs/alerts-dispatch/run`. The backup alert needs the status file:
   run `scripts/ops/backup.sh` once (it creates `$DATA_ROOT/ops-status/backup.json`, 0644).
5. **Resources.** `free -h`, `docker stats --no-stream`; do not run the `imaging` and
   `coding` profiles together unless RAM allows (budget table in docs/24).
6. **Run the acceptance list on the server and paste the results** into docs/24 as
   "Results (server, <date>)":
   ```bash
   cd /opt/private-ai/aureon
   export AUREON_ACCEPTANCE_EMAIL=<admin email>          # password: prompted (or AUREON_ACCEPTANCE_PASSWORD)
   export AUREON_ACCEPTANCE_PR=SVSKHD/<repo>#<open test PR>        # optional: merge-gate step
   export AUREON_ACCEPTANCE_WEBHOOK_SECRET=<GITHUB_WEBHOOK_SECRET>  # optional: idempotency half
   export AUREON_ACCEPTANCE_CODE_REPO=<CODE_REPOS entry>            # optional: coding step
   export AUREON_ACCEPTANCE_INSTRUMENT_ID=<mt5 instrument id with a live price>  # paper step
   export AUREON_ACCEPTANCE_DRILL_DIR=/mnt/spasta-data/backups/aureon/drills
   # Runs in the api image (it has httpx), on the compose network, against the API itself:
   docker compose run --rm --no-deps -it \
     -v "$PWD/scripts/ops/acceptance.py:/opt/acceptance.py:ro" \
     -v /mnt/spasta-data/ops-status:/out -v "$AUREON_ACCEPTANCE_DRILL_DIR:/drills:ro" \
     -e AUREON_ACCEPTANCE_EMAIL -e AUREON_ACCEPTANCE_PR -e AUREON_ACCEPTANCE_WEBHOOK_SECRET \
     -e AUREON_ACCEPTANCE_CODE_REPO -e AUREON_ACCEPTANCE_INSTRUMENT_ID \
     -e AUREON_ACCEPTANCE_DRILL_DIR=/drills \
     api python /opt/acceptance.py --base-url http://aureon-api:8000 --report /out/acceptance.json
   ```
   (the password is prompted on the tty; nothing is passed in argv).
   Add `--skip-heavy` to avoid starting chat/image/coding jobs. Through Cloudflare instead,
   use `--base-url https://api.spasta.online` with `CF_ACCESS_CLIENT_ID/SECRET`.
