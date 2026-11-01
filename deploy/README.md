# Deploy — Phase 1 (Ubuntu 24.04, user svskhd8)

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

## 2. Build, migrate, start

```bash
docker compose build
docker compose run --rm migrate        # creates only public.aureon_alembic_version
docker compose up -d
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

HDD gate test (optional): rename the marker, run `docker compose up -d --force-recreate`,
confirm `mount-check` fails and `api` does not start, then restore the marker.

## Rollback

```bash
docker compose down            # stops api + redis only; volumes and existing services untouched
```

The baseline migration changes no tables. `docker compose run --rm migrate alembic downgrade base`
leaves only an empty version table.
