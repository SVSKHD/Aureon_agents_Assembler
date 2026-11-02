# 17 — Security review (Phase E)

**Date:** 2026-11-02. **Scope:** `main` at the start of Phase E: every router, auth, the
workers, compose, the Nginx snippet and the dependencies. **Method:** I read the code, then
enumerated routes programmatically, then wrote a test for every claim that can be tested.
Runbook: [docs/16-production-hardening.md](16-production-hardening.md).

## Summary of findings

| ID | Severity | Finding | Status |
| --- | --- | --- | --- |
| S-1 | High | The coding agent's test commands run model-patched code as same-uid children of a worker that holds every secret in its environment. Those children could read `/proc/<ppid>/environ`, and they could reach Redis and the Docker proxy, whose `GET /containers/{id}/json` returns every container's env. | **Fixed:** the worker is non-dumpable (`app/core/procsec.py`, tested as an unprivileged user). The coding worker left the `aureon` network. The Docker proxy moved to an internal `aureon-docker` network shared only with the API. |
| S-2 | Medium | `POST /api/trading/strategies/backtest`, `POST /api/trading/signals/{id}/explain` and `POST /api/trading/backtests/{id}/explain` required a session but no CSRF token. SameSite=Strict cookies blunted this; the double-submit check was still missing. | **Fixed** (`CsrfProtected`); `tests/test_route_auth.py` now fails on any unsafe route without CSRF. |
| S-3 | Medium | No log redaction. A token, password or DSN passed to a logger, or carried in an exception message, would be written verbatim. | **Fixed:** the `redact_secrets` structlog processor covers structlog and stdlib logs: sensitive keys at any depth, `user:pass@` in URLs, `Bearer …`. |
| S-4 | Medium | ENV=prod enforced only the HDD marker. A placeholder or low-entropy `SECRET_KEY` (e.g. `"k"*48` passes the length check), `COOKIE_SECURE=false`, a `*` CORS origin, or `TRADING_LIVE_ENABLED=true` all started fine. | **Fixed:** `app/core/prod_checks.py` plus a Settings validator (`PHASE_G_APPROVED=yes`). |
| S-5 | Medium | The image prompt fills its own argv slot. A prompt such as `--output=/elsewhere` would be parsed as an option by the image agent's argument parser. There is no shell, so this is not command injection, but it could change output paths or flags. | **Fixed:** a leading space is added to user values that start with `-` (`generator.py`). |
| S-6 | Low | `/api/openapi.json` was served in prod (only Swagger UI was off). It gave a full API map to unauthenticated callers. | **Fixed:** off in prod unless `API_DOCS_ENABLED` (refused in prod). |
| S-7 | Low | `GET /api/health/ready` is public and returns the Postgres server version and the Ollama model count. | **Mitigated:** the Nginx snippet answers 404 from outside; on-box checks call 127.0.0.1:8000. The endpoint itself is unchanged. |
| S-8 | Low | No security headers. The Nginx snippet set no HSTS/CSP/frame-ancestors/referrer/permissions policy, and the app set only nosniff on image files. uvicorn advertised `server: uvicorn`. | **Fixed:** app middleware (nosniff, DENY, no-referrer, permissions, COOP, `CSP default-src 'none'; frame-ancestors 'none'`), `deploy/nginx/security-headers.conf` (HSTS + SPA CSP), `--no-server-header`. |
| S-9 | Low | Compose hardening was uneven. mount-check, redis, docker-proxy and migrate had no `cap_drop` or limits, workers inherited the API's HTTP healthcheck (permanently unhealthy), logs were unbounded, and nothing ordered migrate before the API. | **Fixed** (docs/16); asserted by `tests/test_compose_hardening.py`. |
| S-10 | Low | `tests/test_paper_db.py` checked order-intent routes by iterating `app.routes`. On FastAPI 0.162 that list holds lazy `_IncludedRouter` entries, so the assertion ran over nothing. | **Fixed:** `tests/routes.py` flattens included routers; the test now asserts it found routes. |
| S-11 | Low | Broker and coding errors are echoed to the client truncated (`detail: str(exc)[:300]`). They could include upstream response text. | Open: admin/owner-only endpoints; the error text comes from Dhan/MT5/git, not secrets (tokens travel in headers). Revisit if an adapter ever puts credentials in URLs. |
| S-12 | Low | `.env.example` lists `http://192.168.29.95:8088` in `CORS_ORIGINS`. With `COOKIE_SECURE=true` that origin cannot hold a session anyway. | Open: remove the http origin from the production `.env` (manual-setup.md, Secrets). |
| S-13 | Low | Only login is rate-limited. Chat, images, backtests and explanations are bounded by queues (429 when full), not per-user rates. Finance writes have no limit. | Open (accepted): single-household deployment; the queues bound resource use. Add per-user Redis limits if accounts are ever opened to others. |
| S-14 | Info | Container images are pinned by tag, not digest (`python:3.12-slim`, `redis:7.4-alpine`, `tecnativa/docker-socket-proxy:0.3.0`). There is no image CVE scan. | Open: pin digests and run `trivy image` on the server (manual-setup.md). |
| S-15 | Info | Backups live on the same HDD as the data. | Open: off-box copy needs a destination from the owner. |
| S-16 | Medium | The news fetcher (`app/services/news.py`, added by the frontend-support phase) followed redirects to any http(s) URL. A feed host could have redirected the scheduler to internal services (Docker service names, loopback, private and link-local addresses), and whatever parsed as a feed would have been published at `/api/public/forex`. | **Fixed:** redirects may only go to public hosts (global IPs or dotted names; never localhost, `.local` or `.internal`). Tested. Feed URLs themselves come only from the owner's `NEWS_FEED_URLS`. |
| S-17 | Low | Receipt attachments accept up to `FINANCE_ATTACHMENT_MAX_BYTES` (10 MiB), but the Nginx snippet capped every `/api/` body at 2 MB, so larger receipts failed at the proxy with a confusing 413. | **Fixed:** an exact `location = /api/finance/attachments` with `client_max_body_size 11m`. Everything else stays at 2 MB. |

Nothing found in: SQL injection, SSRF, path traversal in images and the coding agent,
session handling, and password storage (details below).

## 1. Authentication and authorization on every route

`tests/test_route_auth.py` walks every route, including those behind lazily included routers,
and asserts four things:

- Every route requires `get_current` (a valid, unexpired, server-side session), except this
  allowlist: `GET /api/health`, `GET /api/health/ready`, `POST /api/auth/login`, and the
  read-only `GET /api/public/shares/{doc_id}` and `GET /api/public/forex`. Those two must
  carry the per-IP `rate_limited` dependency, and they strip owner ids from the data they
  return.
- Every unsafe method (POST/PUT/PATCH/DELETE) requires `require_csrf`. The `X-CSRF-Token`
  header must equal the CSRF cookie, and both must equal HMAC(SECRET_KEY, session token).
- The set of `require_admin` routes equals a pinned list, so any change is a deliberate diff.
- In prod there are no docs or openapi routes, and there are no websocket routes or mounts.

Object-level access is enforced in services and covered by the existing DB tests. Finance rows
are scoped by `aureon_user_id`, another user's row is a 404, and trading accounts are filtered
through `visible_accounts`. Image assets and jobs are filtered by `user_id`. Order-intent
approval is admin-only.

Current map (method, path, guard):

| Method | Path | Guard |
| --- | --- | --- |
| GET | `/api/agents` | Current (session) |
| GET | `/api/agents/{agent_id}` | Current (session) |
| GET | `/api/agents/{agent_id}/logs` | AdminRequired (session + CSRF + admin) |
| POST | `/api/agents/{agent_id}/pause` | AdminRequired (session + CSRF + admin) |
| POST | `/api/agents/{agent_id}/resume` | AdminRequired (session + CSRF + admin) |
| GET | `/api/agents/{agent_id}/runs` | Current (session) |
| POST | `/api/auth/login` | **public** |
| POST | `/api/auth/logout` | CsrfProtected (session + CSRF) |
| GET | `/api/auth/me` | Current (session) |
| GET | `/api/auth/sessions` | Current (session) |
| POST | `/api/auth/sessions/revoke-all` | CsrfProtected (session + CSRF) |
| DELETE | `/api/auth/sessions/{session_id}` | CsrfProtected (session + CSRF) |
| POST | `/api/chat` | CsrfProtected (session + CSRF) |
| POST | `/api/code/analyze` | AdminRequired (session + CSRF + admin) |
| GET | `/api/code/tasks` | AdminRequired (session + CSRF + admin) |
| POST | `/api/code/tasks` | AdminRequired (session + CSRF + admin) |
| GET | `/api/code/tasks/{task_id}` | AdminRequired (session + CSRF + admin) |
| POST | `/api/code/tasks/{task_id}/approve` | AdminRequired (session + CSRF + admin) |
| GET | `/api/code/tasks/{task_id}/diff` | AdminRequired (session + CSRF + admin) |
| POST | `/api/code/tasks/{task_id}/reject` | AdminRequired (session + CSRF + admin) |
| GET | `/api/conversations` | Current (session) |
| DELETE | `/api/conversations/{conversation_id}` | CsrfProtected (session + CSRF) |
| GET | `/api/conversations/{conversation_id}` | Current (session) |
| GET | `/api/finance/attachments` | Current (session) |
| POST | `/api/finance/attachments` | CsrfProtected (session + CSRF) |
| DELETE | `/api/finance/attachments/{attachment_id}` | CsrfProtected (session + CSRF) |
| GET | `/api/finance/attachments/{attachment_id}` | Current (session) |
| GET | `/api/finance/attachments/{attachment_id}/file` | Current (session) |
| GET | `/api/finance/changes` | Current (session) |
| POST | `/api/finance/claim` | AdminRequired (session + CSRF + admin) |
| GET | `/api/finance/documents/{namespace}` | Current (session) |
| DELETE | `/api/finance/documents/{namespace}/{doc_id}` | CsrfProtected (session + CSRF) |
| GET | `/api/finance/documents/{namespace}/{doc_id}` | Current (session) |
| PUT | `/api/finance/documents/{namespace}/{doc_id}` | CsrfProtected (session + CSRF) |
| GET | `/api/finance/expenses` | Current (session) |
| POST | `/api/finance/expenses` | CsrfProtected (session + CSRF) |
| DELETE | `/api/finance/expenses/{row_id}` | CsrfProtected (session + CSRF) |
| GET | `/api/finance/expenses/{row_id}` | Current (session) |
| PATCH | `/api/finance/expenses/{row_id}` | CsrfProtected (session + CSRF) |
| GET | `/api/finance/portfolio-profiles/{profile_id}` | Current (session) |
| PUT | `/api/finance/portfolio-profiles/{profile_id}` | AdminRequired (session + CSRF + admin) |
| GET | `/api/finance/secured` | Current (session) |
| POST | `/api/finance/secured` | CsrfProtected (session + CSRF) |
| DELETE | `/api/finance/secured/{row_id}` | CsrfProtected (session + CSRF) |
| GET | `/api/finance/secured/{row_id}` | Current (session) |
| PATCH | `/api/finance/secured/{row_id}` | CsrfProtected (session + CSRF) |
| GET | `/api/finance/totals` | Current (session) |
| GET | `/api/finance/trades` | Current (session) |
| POST | `/api/finance/trades` | CsrfProtected (session + CSRF) |
| DELETE | `/api/finance/trades/{row_id}` | CsrfProtected (session + CSRF) |
| GET | `/api/finance/trades/{row_id}` | Current (session) |
| PATCH | `/api/finance/trades/{row_id}` | CsrfProtected (session + CSRF) |
| GET | `/api/health` | **public** |
| GET | `/api/health/ready` | **public** |
| GET | `/api/images` | Current (session) |
| POST | `/api/images/generate` | CsrfProtected (session + CSRF) |
| GET | `/api/images/jobs/{job_id}` | Current (session) |
| POST | `/api/images/jobs/{job_id}/cancel` | CsrfProtected (session + CSRF) |
| DELETE | `/api/images/{asset_id}` | CsrfProtected (session + CSRF) |
| GET | `/api/images/{asset_id}/file` | Current (session) |
| GET | `/api/models` | Current (session) |
| GET | `/api/models/status` | Current (session) |
| GET | `/api/public/forex` | **public**, per-IP rate limit |
| GET | `/api/public/shares/{doc_id}` | **public**, per-IP rate limit |
| GET | `/api/repos` | AdminRequired (session + CSRF + admin) |
| GET | `/api/repos/{name}/branches` | AdminRequired (session + CSRF + admin) |
| GET | `/api/repos/{name}/commits` | AdminRequired (session + CSRF + admin) |
| GET | `/api/repos/{name}/file` | AdminRequired (session + CSRF + admin) |
| GET | `/api/repos/{name}/tree` | AdminRequired (session + CSRF + admin) |
| GET | `/api/scheduler/jobs` | AdminRequired (session + CSRF + admin) |
| POST | `/api/scheduler/jobs/{job_id}/pause` | AdminRequired (session + CSRF + admin) |
| POST | `/api/scheduler/jobs/{job_id}/resume` | AdminRequired (session + CSRF + admin) |
| POST | `/api/scheduler/jobs/{job_id}/run` | AdminRequired (session + CSRF + admin) |
| GET | `/api/system/status` | AdminRequired (session + CSRF + admin) |
| GET | `/api/trading/accounts` | Current (session) |
| POST | `/api/trading/accounts` | AdminRequired (session + CSRF + admin) |
| GET | `/api/trading/accounts/{account_id}` | Current (session) |
| GET | `/api/trading/accounts/{account_id}/funds` | Current (session) |
| GET | `/api/trading/accounts/{account_id}/health` | Current (session) |
| GET | `/api/trading/accounts/{account_id}/holdings` | Current (session) |
| POST | `/api/trading/accounts/{account_id}/mode` | AdminRequired (session + CSRF + admin) |
| POST | `/api/trading/accounts/{account_id}/mode/live-token` | AdminRequired (session + CSRF + admin) |
| GET | `/api/trading/accounts/{account_id}/orders` | Current (session) |
| GET | `/api/trading/accounts/{account_id}/paper` | Current (session) |
| POST | `/api/trading/accounts/{account_id}/paper/reconcile` | AdminRequired (session + CSRF + admin) |
| GET | `/api/trading/accounts/{account_id}/paper/reconciliations` | Current (session) |
| GET | `/api/trading/accounts/{account_id}/positions` | Current (session) |
| GET | `/api/trading/accounts/{account_id}/risk-policy` | Current (session) |
| PUT | `/api/trading/accounts/{account_id}/risk-policy` | AdminRequired (session + CSRF + admin) |
| GET | `/api/trading/accounts/{account_id}/trades` | Current (session) |
| GET | `/api/trading/agents` | Current (session) |
| GET | `/api/trading/agents/{agent_id}/logs` | AdminRequired (session + CSRF + admin) |
| GET | `/api/trading/agents/{agent_id}/status` | Current (session) |
| GET | `/api/trading/ai-analyses` | Current (session) |
| GET | `/api/trading/backtests` | Current (session) |
| GET | `/api/trading/backtests/{run_id}` | Current (session) |
| POST | `/api/trading/backtests/{run_id}/explain` | CsrfProtected (session + CSRF) |
| GET | `/api/trading/candles` | Current (session) |
| GET | `/api/trading/executions` | Current (session) |
| GET | `/api/trading/indicators` | Current (session) |
| GET | `/api/trading/instruments` | Current (session) |
| PUT | `/api/trading/mt5/instruments/{symbol}` | AdminRequired (session + CSRF + admin) |
| GET | `/api/trading/mt5/status` | Current (session) |
| GET | `/api/trading/option-chain` | Current (session) |
| GET | `/api/trading/option-chain/expiries` | Current (session) |
| GET | `/api/trading/order-intents` | Current (session) |
| POST | `/api/trading/order-intents` | CsrfProtected (session + CSRF) |
| GET | `/api/trading/order-intents/{intent_id}` | Current (session) |
| POST | `/api/trading/order-intents/{intent_id}/approve` | AdminRequired (session + CSRF + admin) |
| POST | `/api/trading/order-intents/{intent_id}/cancel` | CsrfProtected (session + CSRF) |
| GET | `/api/trading/quotes` | Current (session) |
| GET | `/api/trading/risk-decisions` | Current (session) |
| GET | `/api/trading/signals` | Current (session) |
| POST | `/api/trading/signals/{signal_id}/explain` | CsrfProtected (session + CSRF) |
| GET | `/api/trading/strategies` | Current (session) |
| POST | `/api/trading/strategies` | AdminRequired (session + CSRF + admin) |
| POST | `/api/trading/strategies/backtest` | CsrfProtected (session + CSRF) |
| GET | `/api/trading/stream` | Current (session) |

## 2. CSRF, cookies, sessions

- **Cookies:** the session cookie is `__Host-aureon_session` (HttpOnly, Secure, SameSite=Strict,
  Path=/). The CSRF cookie is `__Host-aureon_csrf`, readable by the SPA. `COOKIE_SECURE=false`
  drops the `__Host-` prefix, and the prod check refuses it.
- **Sessions:** a random token (≥ 43 chars) whose SHA-256 is stored, never the token itself.
  The TTL is absolute (12 h). There are logout and revoke-all routes. Login creates a fresh
  session, so session fixation is not possible.
- **Passwords:** argon2id with rehash on login. An unknown email still spends verify time
  (`burn_verify_time`), so timing does not reveal which emails exist, and both cases return
  the same 401 message.
- **CSRF:** double-submit, bound to the session by HMAC; see S-2 for the three routes that
  were missing it.

## 3. Rate limits

Login is limited per (email, IP) and per IP in Redis, with keys hashed and the Retry-After
header set. The client IP comes from uvicorn's proxy headers, which are trusted only from
`FORWARDED_ALLOW_IPS`. Everything else is bounded by queues: Ollama (`OLLAMA_MAX_QUEUE`),
images (`IMAGE_QUEUE_MAX`), research AI (`RESEARCH_AI_QUEUE_MAX`) and backtests
(`BACKTEST_MAX_GRID`). Coding tasks are admin-only. See S-13.

## 4. Path traversal

- **Images:** `safe_asset_file` resolves the stored path and requires it to lie under
  `DATA_ROOT/images` with a `.png` suffix. Asset IDs are UUIDs.
- **Coding agent:** `paths.normalize` rejects absolute paths, `..`, NUL and backslashes. It
  blocks `.git`, `.env*`, keys and secrets. `safe_path` re-checks after resolving symlinks.
  Refs must match a SHA or `^[A-Za-z0-9][A-Za-z0-9._/-]*$` with no `..` or `@{`, so a ref can
  never start with `-` (no git option injection). `git log` puts paths after `--`.
- **Finance attachments** (frontend-support phase): the stored path is
  `finance-attachments/<user uuid>/<attachment uuid>`, built from UUIDs only, so the client's
  filename never reaches the filesystem. The content type is sniffed from magic bytes, and
  `safe_file` re-resolves and requires the path under `DATA_ROOT/finance-attachments`.
  Downloads are scoped to the owner. `BACKUP_DATA_DIRS` entries in the ops scripts are
  validated (no leading `.`), and restore unpacks into a new folder.

## 5. Subprocesses

Every subprocess call uses an argv list with no shell (`create_subprocess_exec`,
`subprocess.run([...])`):

- **imaging:** the template comes from env, placeholders are validated at startup, and see S-5.
- **git:** fixed verbs, hooks disabled, `protocol.allow=never`, scrubbed env.
- **coding tests:** an exact-argv allowlist, a scrubbed env, their own process group (killed on
  timeout); see S-1.
- **migrate:** a fixed `alembic` argv; the DSN is passed via env.

No endpoint executes a shell command.

## 6. Outbound HTTP (SSRF)

No user input becomes a URL. Outbound hosts come from settings only: `OLLAMA_BASE_URL`,
`DHAN_BASE_URL`, `DHAN_FEED_URL`, `DHAN_INSTRUMENT_CSV_URL`, `MT5_STATUS_URL`,
`DOCKER_API_URL` and `NEWS_FEED_URLS`. The httpx clients do not follow redirects (the default,
and explicit for MT5). The news fetcher follows at most 3 redirects by hand, http(s) only and
now only to public hosts (S-16). It caps responses at `NEWS_MAX_FEED_BYTES`, rejects
DOCTYPE/ENTITY before XML parsing, and is off unless the owner sets `NEWS_FEED_URLS`. It runs
in the scheduler container, which cannot reach the Docker proxy.

## 7. Secrets in logs and errors

- Logs: S-3. The redaction processor runs last before rendering, in both chains, and is tested.
- Readiness and system-status errors report only the exception type, never the message (which
  could carry a DSN).
- `Settings` holds `DATABASE_URL`, `SECRET_KEY`, `DHAN_ACCESS_TOKEN` and `MT5_STATUS_TOKEN` as
  `SecretStr`, so they are masked in `repr`.
- The image subprocess receives no secrets (env passthrough is by name). Coding test commands
  receive a scrubbed env (S-1).
- Ops scripts read no application secret. pg_dump runs inside the Postgres container over its
  local socket, and a test asserts that `PGPASSWORD` never reaches argv or output.

## 8. SQL injection

All queries go through the SQLAlchemy ORM or Core with bound parameters. Every `text()` call is
a constant: `SHOW server_version`, the advisory-lock statements, the `pg_database_size`
status query, and the Dhan instrument-link `UPDATE`. Migration `0010` builds DDL with
f-strings, but only over the module constant `OWNED = ("trades", "expenses", "secured",
"astra_documents")`, never user input. The ops scripts interpolate only constants and database
names that match `^[a-z_][a-z0-9_]{0,62}$`.

## 9. CORS and headers

CORS uses an explicit origin allowlist with credentials. Methods and headers are listed, and
prod refuses `*`; see S-12 for the LAN http origin. Headers: S-8. The API's CSP
(`default-src 'none'`) is safe because it only serves JSON, SSE and PNG. Swagger, which is
dev-only, is exempt. The SPA policy is in `deploy/nginx/security-headers.conf`. Because a
location that sets its own `add_header` inherits none from the server block, `aureon-api.conf`
repeats HSTS, nosniff, Referrer-Policy and Permissions-Policy.

## 10. Containers

Every service runs `read_only` with tmpfs, `cap_drop: [ALL]`, `no-new-privileges`, mem/cpu
limits and bounded logs. Images run as non-root (uid 10001; Redis as 999). `docker.sock` is
mounted read-only only in the socket proxy, which allows GET /containers and refuses
POST/EXEC; it sits on an internal network with the API only (S-1). Only `127.0.0.1:8000` is
published. Docker-published ports bypass ufw, which is why the compose test forbids `0.0.0.0`.

## 11. Dependency audit

`pip-audit -l` ran on 2026-11-02 against the project environment with network access to the
advisory database. Result: **No known vulnerabilities found** (only the local package itself
was skipped). Resolved versions: fastapi 0.162.0, starlette 1.10.0, uvicorn 0.54.0,
pydantic 2.14.2, sqlalchemy 2.0.55, asyncpg 0.32.0, alembic 1.20.1, redis 8.1.0, httpx 0.28.1,
h11 0.16.0, structlog 26.1.0, argon2-cffi 25.1.0, websockets 17.4, numpy 2.5.4, pandas 3.1.0.
`pyproject.toml` uses lower bounds, so production gets whatever is current at build time.
Re-run the audit against the built image:

```bash
# throwaway, writable container from the built image (not the read-only service)
docker run --rm -u 0 aureon-agentic:${IMAGE_TAG:-dev} sh -c 'pip install -q pip-audit && pip-audit -l'
```

Image CVEs: S-14 (`trivy image aureon-agentic:<tag>` on the server).

## Tests added

`tests/test_route_auth.py`, `tests/test_prod_hardening.py` (checklist, LIVE flag, redaction,
headers, non-dumpable worker, prompt-as-option), `tests/test_compose_hardening.py` (ports,
isolation, hardening) and `tests/test_ops_scripts.py`.
