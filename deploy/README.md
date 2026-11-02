# Deploy — Phases 1–5 + Trading A–D (Ubuntu 24.04, user svskhd8)

The API joins the existing Postgres/Ollama network. Redis is added. Only `127.0.0.1:8000`
is published. Nothing existing is stopped or modified.

## 0. Prerequisites (once)

```bash
findmnt /mnt/spasta-data                       # must show the 2 TB HDD
touch /mnt/spasta-data/.spasta-data-mounted    # marker the API/compose check for
mkdir -p /mnt/spasta-data/redis                # Redis AOF data on the HDD
docker network ls                              # find the private-ai network name
```

If `mkdir` under `/mnt/spasta-data` needs root, ask before using sudo.

## 1. Clone and configure

```bash
cd /opt/private-ai
git clone https://github.com/SVSKHD/Aureon_agents_Assembler.git aureon
cd aureon
cp .env.example .env
chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into SECRET_KEY
nano .env   # DATABASE_URL, OLLAMA_BASE_URL, PRIVATE_AI_NETWORK from docs/00-inspection.md
docker compose config -q && echo OK
```

Use a **dedicated, least-privilege Postgres role** for `DATABASE_URL`, not the superuser.
Migration `0002` creates the `core` and `audit` schemas, so that role needs `CREATE` on the
database once: `GRANT CREATE ON DATABASE spasta TO <role>;` (run by the DB owner).

**Client IP for rate limits:** Nginx reaches the API through Docker's published port, so
requests arrive from the bridge gateway. Find it and set `FORWARDED_ALLOW_IPS` in `.env`
(make sure Nginx sends `X-Forwarded-For`):

```bash
docker network inspect aureon -f '{{(index .IPAM.Config 0).Gateway}}'
```

## 2. Build, migrate, start

```bash
docker compose build
# Phase E: `up` runs the migrate one-shot first (waits for Postgres, then upgrade head) and
# starts the API only after it succeeds. Running it explicitly first is still fine:
docker compose run --rm migrate        # 0001 version table; 0002 core.*/audit.*; 0003 ai.*; 0004 agents.image_*; 0005 agents registry/scheduler; 0006 trading.*/dhan.*/mt5.*; 0007 trading.indicator_values; 0008 agents.code_tasks (new tables only)
docker compose up -d                   # api, redis, scheduler, docker-proxy
docker compose run --rm -it migrate python -m app.auth.bootstrap --email you@example.com
```

## 3. Validate

```bash
docker compose ps                                   # mount-check + migrate exited 0; everything else healthy
scripts/ops/healthcheck.sh                          # Phase E: health, ready and compose health in one go
curl -fsS http://127.0.0.1:8000/api/health
curl -sS  http://127.0.0.1:8000/api/health/ready | python3 -m json.tool
docker compose logs --tail=50 api
ss -tlnp | grep -E ':(5432|6379|11434|8000)\b'      # only 127.0.0.1:8000 may appear
```

Expected: `/api/health` → `{"status":"ok"}`; `/ready` → HTTP 200 with every check `ok: true`.
A 503 names the failing dependency in `checks`.

Auth smoke test. Run it over HTTPS (the tunnel) or set `COOKIE_SECURE=false` for a local
http check. Secure cookies are not sent back over plain http.

```bash
J=/tmp/aureon.jar; B=https://spasta.online
curl -sS -c $J -H 'content-type: application/json' \
  -d '{"email":"you@example.com","password":"…"}' $B/api/auth/login
curl -sS -b $J $B/api/auth/me
CSRF=$(awk '/aureon_csrf/{print $7}' $J)
curl -sS -o /dev/null -w '%{http_code}\n' -b $J -X POST $B/api/auth/logout                       # 403
curl -sS -o /dev/null -w '%{http_code}\n' -b $J -H "x-csrf-token: $CSRF" -X POST $B/api/auth/logout  # 204
```

Ollama chat smoke test: see [docs/03-ollama.md](../docs/03-ollama.md#deploy-and-validate).

Frontend support APIs (migration 0011, attachments on the HDD, optional news feeds): see
[docs/02-auth-and-finance.md](../docs/02-auth-and-finance.md#frontend-support) and
[docs/manual-setup.md](../docs/manual-setup.md).

Dhan (read-only adapter, feed worker, jobs): see [docs/11-dhan.md](../docs/11-dhan.md#deploy-and-validate).

MT5 bot (read-only status export, sync job): see [docs/12-mt5-adapter.md](../docs/12-mt5-adapter.md#deploy-and-validate).

Frontend routing (nginx `/api/*` → 127.0.0.1:8000) and the trading stream: see
[docs/14-frontend.md](../docs/14-frontend.md).

Coding agent (migration 0008, opt-in `coding-worker`): see
[docs/05-agents.md](../docs/05-agents.md#coding-agent).

Research agents (migration 0007, opt-in worker): see [docs/13-research-agents.md](../docs/13-research-agents.md#deploy-and-validate).

Paper trading (migration 0009, scheduler jobs `paper-process` / `paper-reconcile`): see
[docs/15-paper-trading.md](../docs/15-paper-trading.md#deploy-and-validate).

Production hardening (Phase E): startup checklist, compose start order and limits, backups,
restore drill, systemd boot units, firewall and log limits: see
[docs/16-production-hardening.md](../docs/16-production-hardening.md), the sudo steps in
[docs/manual-setup.md](../docs/manual-setup.md#phase-e--production-hardening) and the security
review [docs/17-security-review.md](../docs/17-security-review.md). Then run
`scripts/ops/healthcheck.sh --wait 300`.

GitHub foundation (migration 0012, `github-sync` job only with `GITHUB_TOKEN`; PAT steps in
[docs/manual-setup.md](../docs/manual-setup.md#15--github-foundation)): see
[docs/18-github-foundation.md](../docs/18-github-foundation.md#deploy-and-validate).

GitHub issues, PRs and the approval gate (migration 0013; the extra PAT permission is in
[docs/manual-setup.md](../docs/manual-setup.md#16--github-issues-and-prs)): see
[docs/19-github-issues-prs.md](../docs/19-github-issues-prs.md#deploy-and-validate).

GitHub Actions and webhooks (migration 0014; the `github-actions-sync` job only with
`GITHUB_TOKEN`; the webhook worker is opt-in: `docker compose --profile github up -d
github-webhook-worker`; the webhook, its secret and the Cloudflare Access bypass are in
[docs/manual-setup.md](../docs/manual-setup.md#17--github-actions-and-webhooks)): see
[docs/20-github-actions-webhooks.md](../docs/20-github-actions-webhooks.md#deploy-and-validate).

Incidents and self-repair (migration 0015; the `incident-scan` job runs every minute; the
repair loop runs in the opt-in coding worker, `docker compose --profile coding up -d
coding-worker`; pulling the model, `REPAIR_*` and the PAT permissions are in
[docs/manual-setup.md](../docs/manual-setup.md#18--self-repair)): see
[docs/21-self-repair.md](../docs/21-self-repair.md#deploy-and-validate).

Agents, scheduler, system status and the host-status helper: see
[docs/05-agents.md](../docs/05-agents.md#deploy-and-validate).

Image worker (opt-in, after the /opt/image-agent inspection): build, start and validate it as in
[docs/04-imaging.md](../docs/04-imaging.md#deploy-and-validate).

Nginx must not buffer the chat stream. The API sends `X-Accel-Buffering: no`, which Nginx
honours by default; also set `proxy_read_timeout 900s;` on `/api/`.

HDD gate test (optional): rename the marker, run `docker compose up -d --force-recreate`,
confirm `mount-check` fails and `api` does not start, then restore the marker.

## Rollback

```bash
docker compose down            # stops api + redis only; volumes and existing services untouched
```

No migration touches existing tables. To undo Phase D's table, run `alembic downgrade 0006`
(drops only trading.indicator_values). To undo the trading foundation, run
`alembic downgrade 0005` (drops only trading.*, dhan.*, mt5.*). To undo the agent registry, run `alembic downgrade 0004`,
which drops only the four Phase 5 tables. To undo imaging, run `alembic downgrade 0003`, which drops
only `agents.image_jobs` and `agents.generated_assets`. To undo chat, run `alembic downgrade 0002`,
which drops only `ai.*`. To also undo auth, run `alembic downgrade 0001`, which drops `core.*` and `audit.*`.
Run both with `docker compose run --rm migrate …`. Schemas are dropped without CASCADE, so a
downgrade fails safely if anything else was created in them.
