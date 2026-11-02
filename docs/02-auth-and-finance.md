# 02 — Auth and finance

**Status:** auth is done and tested (revision `0002`). Finance is blocked until the real table
definitions are in [docs/00-inspection.md §4](00-inspection.md#4-existing-finance-schema-blocks-phase-2).

## Auth

### Tables (migration `0002`, additive)

| Table | Purpose | Key points |
| --- | --- | --- |
| `core.roles` | Role names | Seeded with `admin` (id 1) and `user` (id 2). |
| `core.users` | Accounts | UUID PK. `email` is unique and must be lowercase (CHECK). Argon2id hash. `is_active` flag. |
| `core.sessions` | Server-side sessions | Stores the SHA-256 of the cookie token, never the token. `expires_at`, `revoked_at`, IP and user agent. Deleted with its user. |
| `audit.audit_events` | Append-only audit log | Event type, actor, target, IP, user agent, request ID and JSONB details. |

The migration only creates the two new schemas `core` and `audit`. The database role
needs `CREATE` on database `spasta` once to run it.

### Endpoints

| Method | Path | Auth | CSRF | Result |
| --- | --- | --- | --- | --- |
| POST | `/api/auth/login` | none | no (no session yet; SameSite=Strict) | 200 `{user, csrf_token, expires_at}` and sets cookies · 401 · 429 + `Retry-After` |
| POST | `/api/auth/logout` | session | yes | 204, revokes this session, clears cookies |
| GET | `/api/auth/me` | session | no | 200 `{id, email, role, last_login_at}` · 401 |
| POST | `/api/auth/sessions/revoke-all` | session | yes | 200 `{revoked: n}`, ends every session of the user, including this one |

### Cookies and CSRF

- **Session cookie** `__Host-aureon_session`: `HttpOnly; Secure; SameSite=Strict; Path=/`.
  It expires after `SESSION_TTL_HOURS` (default 12). The expiry is absolute, not sliding.
- **CSRF cookie** `__Host-aureon_csrf`: same flags except it is readable by JavaScript.
  Its value is `HMAC-SHA256(SECRET_KEY, session token)`, and login also returns it as `csrf_token`.
- On every state-changing request with a session, the client sends `X-CSRF-Token: <value>`.
  The server requires header = cookie = the HMAC for *this* session, otherwise 403.
  A token from another session, or a cookie planted by a sibling subdomain, is rejected.
- With `COOKIE_SECURE=false` (plain-http dev only) the cookies drop `Secure` and the
  `__Host-` prefix and are named `aureon_session` and `aureon_csrf`.

Vue client sketch:

```ts
await fetch('/api/auth/login', { method: 'POST', credentials: 'include',
  headers: { 'content-type': 'application/json' }, body: JSON.stringify({ email, password }) })
const csrf = document.cookie.match(/(?:^|; )(?:__Host-)?aureon_csrf=([^;]+)/)?.[1]
await fetch('/api/auth/logout', { method: 'POST', credentials: 'include',
  headers: { 'x-csrf-token': csrf ?? '' } })
```

### Rate limiting

Failed logins are counted in Redis in fixed windows of `LOGIN_WINDOW_SECONDS` (default 900 s):

- **Per email + client IP:** 5 failures (`LOGIN_MAX_FAILURES`) block that pair, even with the right password.
- **Per client IP:** 20 failures (`LOGIN_MAX_FAILURES_PER_IP`) block all logins from that IP.
- A successful login clears the per-pair counter.
- Redis keys are hashed, so raw emails and IPs never appear in Redis.

The client IP comes from `X-Forwarded-For` only when the connecting proxy is in
`FORWARDED_ALLOW_IPS`. Behind Nginx and Docker port publishing, that proxy is the Docker bridge
gateway (see [deploy/README.md](../deploy/README.md)). If this is set wrong, every user shares one IP and
the per-IP limit becomes global.

### Other behaviour

- **Unknown email vs wrong password:** both return the same 401 body. An unknown email still
  runs one Argon2 verification, so response timing doesn't reveal which emails exist.
- **Deactivating a user** (`is_active=false`) ends access on their next request.
- **Role changes** (`AuthService.change_role`) are audited (`role_changed`, from → to) and revoke
  the target's sessions, so the new permissions apply immediately. No HTTP endpoint exposes this yet.
- **Audited events:** `login`, `login_failed`, `login_rate_limited`, `logout`,
  `sessions_revoked_all`, `role_changed`, `admin_bootstrapped`. Passwords and tokens are never logged.

### Admin bootstrap

```bash
docker compose run --rm -it migrate python -m app.auth.bootstrap --email you@example.com
```

The password is read from stdin (asked twice on a terminal) and must be at least 12 characters.
The script refuses to run if an admin already exists, and a Postgres advisory lock serializes
concurrent runs. `scripts/bootstrap_admin.py` is the same tool for a dev checkout.

## Finance (blocked)

Prompt 3 §6–9 needs the real definitions of `public.astra_documents`, `public.trades`,
`public.expenses`, `public.secured` and `public.portfolio_profiles`. Hard rule 1 says to inspect
before changing anything, so no finance migration has been written against guessed columns.

Once the `pg_dump --schema-only` output is in 00-inspection.md §4, the planned work is:

1. **Migration `0003` (additive):** add a nullable `user_id uuid REFERENCES core.users(id)` and an
   index on each of the five tables. Existing columns, JSONB fields, composite PKs, session
   values, buy/sell and recurrence fields stay as they are.
2. **Backfill (documented, reversible):** set `user_id` on existing rows from the `astra_uid()`
   owner mapping to the bootstrapped admin. Make `user_id` `NOT NULL` in a later revision only
   after verifying the backfill. Downgrade drops only the added columns and indexes.
3. **`astra_uid()` default:** the service layer sets `user_id` from the session. The old default
   is left in place until the Vue app stops depending on it.
4. **Models, schemas and CRUD:** `/api/finance/{documents,trades,expenses,secured,portfolio-profiles}`
   with pagination and filters. Every query is scoped to the user in the service layer.
   Money uses `Numeric`/`Decimal`.
5. **Tests:** user A cannot read, update or delete user B's rows on every endpoint. Migrations are
   tested up and down on a copy of the live DB.
