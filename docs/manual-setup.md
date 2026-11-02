# Manual setup

These steps need the owner: sudo on the server, real secrets, or outside accounts. They are
never run by an agent. Each phase appends its own section below. Run commands as `svskhd8` on
the server, from `/opt/private-ai/aureon`, unless a step says otherwise.

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
