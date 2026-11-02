# Deploy — Phases 1–2 (Ubuntu 24.04, user svskhd8)

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
docker compose run --rm migrate        # 0001 version table; 0002 core.* + audit.* (new tables only)
docker compose up -d
docker compose run --rm -it migrate python -m app.auth.bootstrap --email you@example.com
```

## 3. Validate

```bash
docker compose ps                                   # mount-check exited 0; redis, api healthy
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

HDD gate test (optional): rename the marker, run `docker compose up -d --force-recreate`,
confirm `mount-check` fails and `api` does not start, then restore the marker.

## Rollback

```bash
docker compose down            # stops api + redis only; volumes and existing services untouched
```

Neither migration touches existing tables. To undo auth, run
`docker compose run --rm migrate alembic downgrade 0001`. It drops only `core.*` and `audit.*`
and fails safely if anything else was created in those schemas.
