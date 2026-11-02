# 02 — Auth and finance

**Status:** auth (revision `0002`) and finance (revision `0010`) are done and tested.

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

## Finance

Revision **0010**; the decisions are in [decisions.md](decisions.md#a--finance-prompt-3).

The DDL comes from ASTRA-FRONTEND `supabase/migrations/` (D-A1). The migration is additive:

- **Ownership:** a nullable `aureon_user_id uuid` column (FK `core.users`, `ON DELETE SET NULL`)
  plus an index on `trades`, `expenses`, `secured` and `astra_documents`.
- **Identity map:** the new table `core.finance_identities` (Aureon user ↔ legacy uid).
- **Missing tables:** created only if absent (fresh DB / CI), without the Supabase-only bits.

Existing columns, composite PKs, RLS policies and `astra_uid()` defaults are untouched.
Downgrade removes only the added column, index and table.

| Method | Path | Notes |
| --- | --- | --- |
| GET / POST | `/api/finance/{trades,expenses,secured}` | paged; `date_from`, `date_to`; trades: `symbol`, `session`, `side`; expenses: `category`, `kind` |
| GET / PATCH / DELETE | `/api/finance/{trades,expenses,secured}/{id}` | client-minted ids kept (409 on replay) |
| GET | `/api/finance/totals` | exact sums (trade P&L, expenses, secured) for a range |
| GET | `/api/finance/documents/{namespace}` | the user's documents in a namespace |
| GET / PUT / DELETE | `/api/finance/documents/{namespace}/{doc_id}` | PUT = Firestore `setDoc`, `merge` flag |
| GET / PUT | `/api/finance/portfolio-profiles/{id}` | read: signed in; write: admin (audited) |
| POST | `/api/finance/claim` | admin: attach a legacy owner's unclaimed rows; dry run unless `apply` (audited) |

**Rules that apply to every endpoint:**

- Every query is scoped to the signed-in user in `app/services/finance.py`.
- Another user's row is a 404.
- Writes need CSRF.
- Money is Decimal on the wire, converted to the legacy `double precision` at the boundary
  (D-A3).

**Claiming the existing rows on the server:**

```bash
# legacy uid = the user_id text the old app wrote (Supabase auth.users id or Firebase uid)
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' $B/api/finance/claim \
  -d '{"legacy_user_id":"<uid>","user_id":"<aureon user id>"}'                 # dry run: counts
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' $B/api/finance/claim \
  -d '{"legacy_user_id":"<uid>","user_id":"<aureon user id>","apply":true}'    # apply
```

**Tests:** `tests/test_finance_db.py` covers:

- CRUD with exact decimals; client ids; 409 replays; filters; totals
- user B gets 404 on GET, PATCH and DELETE of every resource, and 0 in lists and totals; admins
  are scoped too
- CSRF and 401
- validation: negative money, recurring without a day, bad session or side, client-chosen
  owner, bad ids
- legacy rows: invisible until claimed; dry run versus apply; new rows keep the legacy owner
  id; no double claim; audit
- documents: set, merge and ownership
- portfolio: admin-only writes

## Frontend support

Revision **0011** (additive: two new tables, no existing table altered). These are the small
APIs the ASTRA-FRONTEND app needs to move off Supabase / Firebase (its stages 3 and 6).
Decisions: [decisions.md](decisions.md#d-backend--frontend-support-apis) (D-Dbe-*).

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/finance/{trades,expenses,secured}?updated_since=` | also on `/api/finance/documents/{namespace}`: rows with `updated_at > updated_since` |
| GET | `/api/finance/changes?since=<iso with offset>` | `{cursor, truncated, trades, expenses, secured, documents, deleted}` for the signed-in user |
| GET | `/api/auth/sessions` | own non-revoked, non-expired sessions: `id, created_at, expires_at, ip, user_agent, current` |
| DELETE | `/api/auth/sessions/{id}` | CSRF; revoke one own session (another user's or unknown id: 404); audited `session_revoked` |
| POST | `/api/finance/attachments` | multipart `kind`, `ref`, `file`; CSRF; the row must be yours (404); audited |
| GET | `/api/finance/attachments?kind=&ref=` | your attachments (newest first, max 500) |
| GET / DELETE | `/api/finance/attachments/{id}` | metadata / delete (file removed after commit; audited) |
| GET | `/api/finance/attachments/{id}/file` | the file, `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff` |
| GET | `/api/public/shares/{doc_id}` | no auth; namespace `aureon-shares` with `data.isPublic == true`, else 404; `ownerId`/`userId` stripped |
| GET | `/api/public/forex?limit=` | no auth; ownerless namespace `forex` documents, newest `published` first (max 200) |

**Change sync.**

- Every delete of a trade, expense, secured row or document writes a tombstone to
  `core.finance_tombstones` (`kind` = `trades|expenses|secured|document`, `ref` = row id or
  `namespace/doc_id`).
- `/changes` returns rows and documents with `updated_at > since` and tombstones with
  `deleted_at > since`, oldest first, at most `FINANCE_CHANGES_MAX_ROWS` per list.
  - Store `cursor` and send it as `since` next time.
  - When `truncated` is true, call again at once with the returned cursor.
  - A row id can appear both as a row and in `deleted` (deleted, then re-created with the same
    id). Apply the delete only when its `deleted_at` is newer than the row's `updated_at`.
- The `purge-agent-history` job prunes tombstones older than
  `FINANCE_TOMBSTONE_RETENTION_DAYS` (30). A client offline for longer must do a full re-sync
  (list endpoints without `updated_since`).

**Attachments.**

- Files are stored at `${DATA_ROOT}/finance-attachments/<user uuid>/<attachment uuid>`: a path
  built from UUIDs only. The client's filename is cleaned and kept as metadata only.
- Uploads need the HDD marker, through the same `check_data_root` the imaging code uses (503
  without it).
- The type is sniffed from the magic bytes (PNG, JPEG, WebP, PDF); anything else is 415. The
  header the client sent is ignored.
- Size limits: over `FINANCE_ATTACHMENT_MAX_BYTES` is 413; past
  `FINANCE_ATTACHMENTS_MAX_PER_USER` is 409 `attachment_limit`.
- Deleting a finance row deletes its attachments (records and files).

**Public shares (owner side).** Use the existing documents API with namespace
`aureon-shares`:

| Action | Request |
| --- | --- |
| Publish | `PUT /api/finance/documents/aureon-shares/<random id>` with `{"data": {..., "isPublic": true}}` |
| Update | the same `PUT`, or with `"merge": true` |
| Unpublish | `PUT ... {"data": {"isPublic": false}, "merge": true}`, or `DELETE` |

- Use an unguessable doc id (e.g. a UUID): anyone with the id can read a public share.
- Another user cannot overwrite your share (404).
- Both public endpoints are rate limited per client IP (`PUBLIC_RATE_LIMIT_PER_MINUTE`, Redis;
  429 with `Retry-After`).

**News feed job (`news_pull`).** Offline by default.

- When `NEWS_FEED_URLS` (JSON list) is empty, the job is not added to the default jobs and a
  manual run does nothing.
- What it does:
  - fetches each RSS / Atom URL (timeout `NEWS_HTTP_TIMEOUT`, max `NEWS_MAX_FEED_BYTES`)
  - follows at most 3 redirects, and only to http(s) URLs
  - refuses any feed with a DOCTYPE or ENTITY declaration (uses defusedxml when installed)
  - keeps items with an http(s) link, at most `NEWS_MAX_ITEMS` (200) per run
- Items are upserted into `public.astra_documents` namespace `forex`: `doc_id` = sha1(link),
  no owner, data `{title, link, published, source}`. Unchanged items and user-owned rows are
  never rewritten.
- Errors are recorded per feed host, never with the URL (it may contain a token).

**Config.**

| Variable | Default | Meaning |
| --- | --- | --- |
| `FINANCE_ATTACHMENT_MAX_BYTES` | `10485760` | max upload size (413 above) |
| `FINANCE_ATTACHMENTS_MAX_PER_USER` | `2000` | attachments per user (409 above) |
| `FINANCE_TOMBSTONE_RETENTION_DAYS` | `30` | tombstone retention |
| `FINANCE_CHANGES_MAX_ROWS` | `5000` | rows per list in one `/changes` call |
| `PUBLIC_RATE_LIMIT_PER_MINUTE` | `60` | `/api/public/*` requests per IP per minute |
| `NEWS_FEED_URLS` | `[]` | JSON list of RSS/Atom URLs; empty = job off |
| `NEWS_PULL_CRON` | `*/30 * * * *` | schedule of `news-pull` (scheduler timezone) |
| `NEWS_HTTP_TIMEOUT` | `10` | seconds per feed request |
| `NEWS_MAX_ITEMS` | `200` | items per run |
| `NEWS_MAX_FEED_BYTES` | `2097152` | max feed size |

**Deploy and validate.**

```bash
docker compose run --rm aureon-api alembic upgrade 0011
curl -sS -b $J $B/api/auth/sessions
curl -sS -b $J "$B/api/finance/changes?since=2026-01-01T00:00:00Z" | jq '.cursor, (.deleted|length)'
curl -sS -b $J -H "x-csrf-token: $CSRF" -F kind=expenses -F ref=<expense id> \
  -F file=@receipt.pdf $B/api/finance/attachments
curl -sS $B/api/public/forex?limit=5
```

**Rollback.** `alembic downgrade 0010` drops only the two new tables (the attachment files on
the HDD stay in place). Then redeploy the previous image.

**Tests.**

- `tests/test_frontend_support_db.py` covers:
  - changes and tombstones, `updated_since`, and tombstone pruning
  - sessions list and single revoke: another user's session is 404, CSRF is required, and it
    is audited
  - attachments: another user gets 404 on all four routes and cannot upload against your row;
    a fake PNG is 415; a PDF is sniffed; oversize is 413; the per-user cap; files are removed on
    delete and on row delete
  - public shares: unpublished is 404, owner ids are stripped, and unpublish works
  - forex: ordering, and user-owned docs are hidden
  - the rate limit
  - the news upsert, with respx
- `tests/test_news_unit.py` covers:
  - RSS and Atom fixtures
  - refusing DOCTYPE/XXE and billion-laughs payloads
  - the redirect scheme check and the size cap
  - errors carry no URL
  - the job is off when the feed list is empty

**Open risks.**

- The upload size is checked while reading the spooled multipart body. The proxy body limit
  (Cloudflare / nginx) is what stops a huge request before it is spooled.
- The `/changes` cursor is the API server clock. A write that commits after the cursor, but
  carries an earlier `updated_at`, can be missed until a full re-sync. In practice that window
  is milliseconds.
- `/api/public/forex` lists only rows with no owner. Legacy forex documents that were written
  with an owner are not shown until they are re-pulled.
