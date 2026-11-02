# Aureon_Agentic

Self-hosted backend for Spasta: a FastAPI modular monolith on PostgreSQL 17, Redis and a local
Ollama. It is offline-first, has no external AI APIs and does no live trading.

**Status: Phase 2 auth is done; finance is blocked on the live schema.** Phase 1 provides
health, config, logging, async DB and Alembic. Phase 2 adds cookie-session auth with CSRF,
rate limiting and an audit log ([docs/02-auth-and-finance.md](docs/02-auth-and-finance.md)).

## Layout

```
app/
  main.py            app factory + lifespan
  core/              settings, JSON logging, request-ID middleware
  api/v1/            routers (health, auth)
  auth/              password hashing, sessions, CSRF, rate limit, admin bootstrap
  db/                async engine/session, declarative base
  services/          health checks
  models/ schemas/ agents/{ollama,imaging,coding,trading}/ workers/   (later phases)
alembic/             async migrations (version table: public.aureon_alembic_version)
tests/  scripts/  deploy/  docs/
```

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Liveness. Always 200 while the process serves. |
| GET | `/api/health/ready` | 200 if Postgres, Redis, Ollama and the HDD are all OK, else 503 with per-check detail. |
| POST | `/api/auth/login` | Email + password; sets session and CSRF cookies. |
| POST | `/api/auth/logout` | Revokes this session (needs `X-CSRF-Token`). |
| GET | `/api/auth/me` | Current user. |
| POST | `/api/auth/sessions/revoke-all` | Signs out every session of the user (needs `X-CSRF-Token`). |
| GET | `/api/docs` | Swagger UI (disabled when `ENV=prod`). |

## Configuration

Every variable is listed in [.env.example](.env.example).

| Variable | Default | Meaning |
| --- | --- | --- |
| `ENV` | `dev` | `dev`/`test`/`prod`. In `prod` the API refuses to start without the HDD marker. |
| `LOG_LEVEL` | `INFO` | Log level. |
| `DATABASE_URL` | required | `postgresql+asyncpg://…@<postgres-container>:5432/spasta` |
| `REDIS_URL` | `redis://aureon-redis:6379/0` | Redis from this compose file. |
| `OLLAMA_BASE_URL` | `http://ollama:11434` | Existing Ollama container on the private network. |
| `IMAGE_AGENT_PATH` | `/opt/image-agent` | SD 1.5 pipeline (Phase 4). |
| `DATA_ROOT` | `/mnt/spasta-data` | HDD mount. |
| `DATA_ROOT_MARKER` | `.spasta-data-mounted` | File that must exist on the HDD. |
| `SECRET_KEY` | required, ≥ 32 chars | Key for CSRF tokens (HMAC). Changing it invalidates CSRF tokens. |
| `CORS_ORIGINS` | empty | Comma-separated browser origin allowlist. |
| `HEALTH_CHECK_TIMEOUT` | `3` | Seconds per readiness check. |
| `SESSION_TTL_HOURS` | `12` | Absolute session lifetime. |
| `COOKIE_SECURE` | `true` | Secure + `__Host-` cookies. Set `false` only for plain-http dev. |
| `LOGIN_MAX_FAILURES` | `5` | Failed logins per (email, IP) per window. |
| `LOGIN_MAX_FAILURES_PER_IP` | `20` | Failed logins per IP per window. |
| `LOGIN_WINDOW_SECONDS` | `900` | Rate-limit window. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxy IPs whose `X-Forwarded-For` uvicorn trusts. |
| `PRIVATE_AI_NETWORK` | required (compose) | Existing Docker network shared with Postgres/Ollama. |

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
make install
make check                     # ruff + mypy --strict + pytest
PG_ADMIN_URL=postgresql://postgres:pw@127.0.0.1:5432/postgres make test-db
```

`make test-db` creates a throwaway database, runs the migration up/down tests and the full
readiness test, then drops it. Never point it at `spasta`.

## Deploy

See [deploy/README.md](deploy/README.md). Admin bootstrap: `python -m app.auth.bootstrap --email …`. Server facts still to be collected:
[docs/00-inspection.md](docs/00-inspection.md).

## Safety rules (Phase 1)

- Only the API is published, on `127.0.0.1:8000`. Postgres, Ollama and Redis have no host ports.
- The containers run as non-root, read-only, with all capabilities dropped and `no-new-privileges`.
- Migrations are additive. Autogenerate ignores tables that are not in our models.
- Readiness errors report the exception type only, so DSNs and hosts never leak into responses.
