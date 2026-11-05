# 28 — Dhan operations: credentials, feed reliability, monitoring and recovery

This builds on [11-dhan.md](11-dhan.md) (the integration) and
[27-dhan-audit.md](27-dhan-audit.md) (the audit and its findings). It covers three things:

- verified credentials and a persisted connection state;
- a feed worker that survives Redis, WebSocket and credential failures without silent
  death or restart loops;
- monitoring that opens incidents, recovers market data within bounds, and asks a person
  for everything else.

Dhan stays **read-only**. Nothing here places, modifies or cancels an order, changes an
execution mode or a risk policy, or renews a credential on its own. The trading-side
guarantees are in [29-trading-safety.md](29-trading-safety.md).

## What was reused and what is new

| Area | Reused unchanged | New or changed here |
|---|---|---|
| Login | The consent flow (start → Dhan login → callback → consume), the pending-login state, client-id match on consume, encrypted `DHAN_SESSION_*` rows, audit | Each new token is verified with `GET /profile` before it is stored. Two new routes: `GET /connection` and `POST /verify`. Logout is recorded in `dhan.connections` |
| Credential storage | `core.app_settings` (Fernet), the log scrubber, masking | `dhan.connections` (migration 0024): safe metadata only. An expired manual token is no longer used (finding 4) |
| API errors | `call_adapter` | Broker refused the credential → `409 broker_credentials_invalid`; rate limit → `429 broker_rate_limited` with `Retry-After`; Redis down → `503 unavailable` (finding 7) |
| Feed client | Parser, subscribe batching, backoff with jitter, bounded queue | Token read at every connect (finding 3), on-demand reconnect, failure counters, fingerprint of the token that failed |
| Feed worker | Redis hash and channel names, agent `dhan-feed`, pause/resume, settings re-exec | Packet validation and dedup, Redis-outage hold-and-retry (finding 1), leader lock, crash-loop guard, no exit when unconfigured (finding 2), crash report, control channel, status record |
| Tick consumer | `TickAggregator`, `store_tick_bars` | Supervised loop (resubscribes on Redis errors, keeps bars on DB errors), malformed packets skipped |
| Health | Integrations card | `GET /api/v1/dhan/feed/health`. The card uses the same verdict, so it sees a missing feed and stale instruments (finding 5) |
| Monitoring | Incidents, `incident-scan` recovery (bounded attempts, cooldown, escalation, audit), `alerts-dispatch` (dedup, retry on failed send) | `dhan-session-check` and `dhan-monitor` jobs; incident source `dhan`; one market-data recovery action, `reconnect_dhan_feed` |
| Approvals | The approval engine, LIVE gate, single-use LIVE token | No executor may be registered for live-trading kinds ([29](29-trading-safety.md)) |

The other open findings from docs/27 are unchanged here: 6 (order modifications missed by
reconcile) and 8 (data gaps).

## Credentials and connection state

### Verification

Verification calls Dhan's supported Profile API: `GET {DHAN_BASE_URL}/profile` with the
`access-token` header. It runs at three points:

- on every login, before the token is stored;
- on `POST /api/v1/dhan/auth/verify` (admin);
- from the `dhan-session-check` job (`DHAN_SESSION_CHECK_CRON`, default every 15 min).

What each outcome means:

| Outcome | When | Stored status |
|---|---|---|
| `ok` | Dhan answered for `DHAN_CLIENT_ID` | `connected` |
| `mismatch` | Dhan answered for a different client id (never echoed) | `mismatch` |
| `rejected` | 401, DH-901, or 807–810. Dhan doesn't distinguish an invalid token from a revoked one | `rejected` |
| `expired` | The token's own expiry has passed: the session's `expiryTime`, or the manual JWT's `exp`. No Dhan call is made | `expired` |
| `unavailable` | Timeout, network error, 429/5xx, or an answer that isn't JSON | unchanged; `consecutive_failures` + 1 |
| `no_token` | A client id but no usable token | `disconnected` (after a logout), otherwise `not_configured` |

Rules:

- **An outage never downgrades a verdict.** A credential first seen during an outage is
  `unverified`.
- **At login:**
  - `rejected` or `mismatch` → the token is never stored. The responses are
    `502 dhan_token_rejected` and `403 dhan_client_mismatch`.
  - `unavailable` → stored as `unverified`, because the tokenId is single-use. The response
    says `"verification": "unavailable"`.
- **A replaced credential** (new login, or a new `DHAN_ACCESS_TOKEN`) is noticed through
  `credential_fp`, an HMAC of the token keyed by `SECRET_KEY`. The failure counters and the
  last verification are reset. The fingerprint is never returned.
- **Logout** keeps the row and every broker account. The status becomes `unverified` while
  a manual token is in use, and `disconnected` otherwise.
- **Renewal is manual.** RenewToken only applies to Dhan-Web tokens and its response isn't
  documented. The API-key flow needs the user's own login (consent + OTP). Aureon never
  works around that.

### `dhan.connections` (migration 0024)

There is one row per client id. Every column is safe to show except `credential_fp`, which
the API never returns:

- `status`, `token_source` (`session` / `manual` / `none`)
- `expires_at`, `connected_at` / `connected_by`, `disconnected_at` / `disconnected_by`
- `data_plan`, `data_valid_until`, `active_segments`
- `last_verified_at`, `last_check_at`, `last_check_outcome`, `last_error_code`
- `consecutive_failures`, `version`, `updated_at`

### Auth API contract (`/api/v1/dhan/auth`)

| Method | Path | Who | Response |
|---|---|---|---|
| POST | `/start` | admin + CSRF | unchanged |
| GET | `/callback` | public | unchanged (redirect only) |
| POST | `/consume` | admin + CSRF | `StatusOut` plus `verification: "ok" \| "unavailable"`. Errors: `409 no_pending_login`, `403 dhan_client_mismatch`, `502 dhan_token_rejected`, `502 dhan_refused`, `502 dhan_unavailable`, `503 settings_key_missing` |
| GET | `/status` | signed in | unchanged (`StatusOut`) |
| GET | `/connection` | signed in | `ConnectionOut` (below) |
| POST | `/verify` | admin + CSRF, 10 per admin per 10 min | `ConnectionOut`. `409 dhan_not_configured`, `429 rate_limited`, `503 unavailable` (fails closed if Redis is down). Dhan unreachable is a 200 with `last_check_outcome: "unavailable"` |
| DELETE | `/session` | admin + CSRF | unchanged; also records the logout |

`ConnectionOut` fields:

```json
{"client_id": "••••", "status": "connected", "token_source": "session",
 "expires_at": "…", "connected_at": "…", "disconnected_at": null,
 "data_plan": "Active", "data_valid_until": "2026-12-05 09:37:52.0",
 "active_segments": ["Equity", "Derivative"], "last_verified_at": "…",
 "last_check_at": "…", "last_check_outcome": "ok", "last_error_code": null,
 "consecutive_failures": 0, "renewal": "manual", "action": null, "version": 3}
```

`action` is what a person should do for the current status, for example "log in to Dhan on
/settings". Every error body is `{"detail": {"code", "detail"}}` and never carries a Dhan
body or a token.

Audit events (values never included):

- `dhan_login_completed` (with `verification`)
- `dhan_login_failed` (with `code`, and `step: "profile"` when the check refused it)
- `dhan_credentials_verified` (`outcome`, `source`, `status`)
- `dhan_connection_changed` (scheduler: `from`, `to`, `credential_replaced`)
- `dhan_logout` (with `status`)

## Feed reliability

How the `dhan-feed` worker handles each situation:

| Situation | Behaviour |
|---|---|
| Normal | Subscribes once per connection, then stores and publishes validated packets. Writes `dhan:feed:status` (JSON, 120 s TTL) every 5 s |
| Redis down | A failed write keeps the **newest packet per instrument and kind** and retries with backoff (0.5 s doubling to 10 s). `redis_ok=false` and `pending_writes` show in the heartbeat (Postgres) and in the status once Redis is back. The drain task never ends on an error |
| WebSocket drop | Exponential backoff with jitter, reset by the first data. Code 805 (too many connections) waits the maximum. Every reconnect re-subscribes on the new socket only |
| Reconnect request | `dhan:feed:control` `{"cmd":"reconnect"}` drops and reopens the socket at once, re-reading the token. Ignored inside `DHAN_FEED_RECONNECT_COOLDOWN_SECONDS`, or while the same refused token is still in use |
| Auth failure (806–810) | Stops connecting. Every 5 s it checks whether the token in effect changed (new login, new manual token, expired session falling back to manual); if so it reconnects by itself. A settings change also re-executes the worker |
| No credentials or instruments | Stays up in `waiting_config`: heartbeats, writes the status, logs why. It used to exit 1 and restart forever (finding 2) |
| Crash | Logs `dhan_feed_crashed`, writes the agent log and `dhan:feed:last_crash` (7 days), exits non-zero. Docker restarts it |
| Crash loop | Each start is recorded in `dhan:feed:starts`. At `DHAN_FEED_CRASH_LOOP_STARTS` (5) starts in 10 minutes it waits before connecting: 15 s, doubling to 5 min (`cooldown`) |
| Two workers | Leader lock `dhan:feed:leader` (30 s TTL, renewed every 5 s). The other worker is `standby`: it opens no socket and writes no status, then takes over when the lock lapses |
| Duplicates and order | Dropped and counted: an identical repeat of the last packet (`duplicates`), and a packet older than the last trade time seen for that instrument (`out_of_order`) |
| Bad packets | Dropped and counted per reason (`invalid_by_reason`): `unknown_segment`, `unsubscribed`, `bad_time` (before 2010 or more than 5 min in the future), `bad_price` (negative or non-finite) |
| Staleness | Only while NSE is open: 09:15–15:30 IST, Mon–Fri, not a `DHAN_MARKET_HOLIDAYS` date. An instrument isn't stale before it has had `DHAN_FEED_STALE_SECONDS` of open market. The set clears when the market closes |

The research worker's tick → 1-minute candle loop is supervised the same way:

- Redis errors resubscribe (`tick_resubscribes`);
- malformed packets are skipped (`tick_errors`);
- database errors keep the finished bars, up to 5000, for the next attempt
  (`tick_store_errors`).

The aggregator ignores late packets and identical repeats, so a replay never double counts.

### Feed health API

`GET /api/v1/dhan/feed/health` needs a signed-in user. The response:

```json
{"status": "ok", "healthy": true, "problems": [], "codes": [],
 "market": {"open": true, "session": "open"}, "configured_instruments": 2,
 "feed": {"state": "running", "connected": true, "subscribed": 2, "fresh": 2, "stale": 0,
          "last_packet_age_s": 0.4, "status_age_s": 2.1, "reconnects": 0,
          "consecutive_failures": 0, "redis_ok": true, "pending_writes": 0, "invalid": 0,
          "duplicates": 3, "out_of_order": 0, "dropped": 0, "auth_failed": false,
          "last_disconnect_reason": null, "token_source": "session"},
 "worker": {"registered": true, "state": "busy", "stale": false, "age_s": 4.0, "paused": false},
 "last_crash": null, "checked_at": "…"}
```

What each `status` means:

| `status` | Meaning |
|---|---|
| `ok` | Market open, connected, every instrument fresh. This is the **only** status with `healthy: true` |
| `degraded` | Some instruments stale, subscriptions incomplete, or Redis writes failing |
| `down` | Disconnected or no fresh data while the market is open; auth failed; `waiting_config`, `cooldown` or `standby` with no leader reporting |
| `market_closed` | Weekend, holiday or outside hours. Never `healthy` |
| `not_running` | No fresh status and no fresh heartbeat, or the worker stopped |
| `unknown` | Redis can't be read and the heartbeat is stale |
| `not_configured` | No client id or no feed instruments |

Problem codes, which key the incidents:

- `redis_writes_failing`, `worker_not_ready`, `status_unreadable`, `not_running`,
  `status_missing`, `auth_failed`
- `disconnected`, `no_fresh_data`, `stale_instruments`, `subscriptions_incomplete`

`POST /api/v1/dhan/feed/reconnect` needs an admin and CSRF:

- `200 {"accepted": true, "receivers": n, "detail": …}`. `receivers: 0` means no worker is
  listening.
- `429 cooldown` with `Retry-After`. The cooldown is shared with automatic recovery.
- `409 dhan_feed_not_configured`, or `503 unavailable` when Redis is down.
- Audited as `dhan_feed_reconnect_requested`.

`GET /api/v1/system/integrations` → `integrations.dhan` adds two fields: `connection` (as
above) and `feed_health` (`status`, `healthy`, `market`, `problems`, `fresh`, `stale`). Its
`problems` now include feed problems and unusable credentials.

## Monitoring, incidents and recovery

The `dhan-monitor` job runs every minute (`DHAN_MONITOR_CRON`) whenever `DHAN_CLIENT_ID` is
set, even without a usable token. Each check below opens or bumps one incident (source
`dhan`, fingerprint `dhan|<check>|<client id>`) while failing, and resolves it once it
passes.

| Check | Severity | Recovery |
|---|---|---|
| `auth`: credential `expired` / `rejected` / `mismatch` / `disconnected`, or no usable token | error | none → escalated to a person at once (`renewal: manual`) |
| `auth_unverified`: Dhan unreachable for 3+ checks in a row | warning | none |
| `redis` | error | none (the docker incident covers the container) |
| `feed_disconnected`, `feed_no_fresh_data` (market open) | error | `reconnect_dhan_feed` |
| `feed_stale_instruments`, `feed_subscriptions_incomplete` | warning | `reconnect_dhan_feed` |
| `feed_not_running`, `feed_auth_failed`, `feed_worker_not_ready`, `feed_status_missing`, `feed_redis_writes_failing` | error | none → manual (the incident says what to do) |
| `feed_status_unreadable` | warning | none |

Recovery is the existing `incident-scan` loop (docs/21-self-repair.md), with one Dhan
addition:

- **One action, market data only.** `reconnect_dhan_feed` publishes the reconnect command,
  under the shared cooldown. Its outcomes: `done`, `failed` (no worker listening, or Redis
  down), `skipped` (cooldown).
- **Bounded.** `INCIDENT_MAX_RECOVERY_ATTEMPTS` (2), at least
  `INCIDENT_RECOVERY_COOLDOWN_SECONDS` (120 s) apart, each one audited as
  `incident_recovery_attempt` and kept in `details._recovery`.
- **Escalation.** After the attempts run out the incident escalates (`incident_escalated`,
  `_manual`). Nothing further is tried automatically.
- **The feed agent is still never restarted by recovery** (`NEVER_RESTART_PREFIXES`).

Alerts go through the existing `alerts-dispatch` job to Discord:

- open `warning`/`error` incidents, deduplicated per fingerprint and severity;
- a failed send is released and retried on the next run;
- the "credential expired" incident is not sent a second time, because the existing
  `dhan_session` alert already says it.

## Settings (all in `/settings` → Dhan)

| Key | Default | Purpose |
|---|---|---|
| `DHAN_SESSION_CHECK_CRON` | `*/15 * * * *` | Credential verification (scheduler timezone) |
| `DHAN_MONITOR_CRON` | `* * * * *` | Monitoring |
| `DHAN_MARKET_HOLIDAYS` | empty | NSE holidays, `YYYY-MM-DD,…`. A silent feed on these days is `market_closed` |
| `DHAN_FEED_STATUS_MAX_AGE_SECONDS` | `30` | An older status record = the worker isn't reporting |
| `DHAN_FEED_CRASH_LOOP_STARTS` | `5` | Starts in 10 min before the startup delay |
| `DHAN_FEED_RECONNECT_COOLDOWN_SECONDS` | `60` | Minimum gap between reconnects (admin and recovery) |

## Runbook

| Symptom (health / incident) | Do |
|---|---|
| `auth` expired, rejected or disconnected | `/settings` → Dhan → **Log in to Dhan** (consent + OTP on Dhan), or paste a new `DHAN_ACCESS_TOKEN`. The feed reconnects by itself; the incident resolves within a minute |
| `auth` mismatch | `DHAN_CLIENT_ID` and the account that logged in differ. Fix `DHAN_CLIENT_ID`, then log in with the right account |
| `auth_unverified` | Dhan's API is unreachable from the server. Check outbound HTTPS and the static IP (`scripts/dhan_preflight.py`). Nothing to renew |
| `feed_not_running` | `.env`: add `dhan` to `COMPOSE_PROFILES`, then redeploy or `docker compose --profile dhan up -d dhan-feed`. Check with `docker ps` (`aureon-dhan-feed`) |
| `feed_worker_not_ready` (`waiting_config`) | The agent log says what is missing: client id, token or `DHAN_FEED_INSTRUMENTS` |
| `feed_worker_not_ready` (`cooldown`) | A crash loop. Read `GET /api/v1/dhan/feed/health` → `last_crash`, the agent log and `docker logs aureon-dhan-feed`, and fix the cause. The worker connects after the delay |
| `feed_disconnected` / `feed_no_fresh_data` during market hours | Automatic reconnect ×2, then manual. Check `last_disconnect_reason` (805 = another session holds the connection; 806 = no Data API plan), then **Reconnect feed** or `POST /api/v1/dhan/feed/reconnect` |
| `feed_redis_writes_failing` / `redis` | Check the Redis container. Held ticks (newest per instrument) are written when it is back |
| `market_closed` on a trading day | The date is in `DHAN_MARKET_HOLIDAYS`, or the clock is wrong |
| Duplicate-tick concern | `duplicates` / `out_of_order` in the health show what was dropped. Only the leader publishes (`leader: true` in the status) |

Useful reads:

- `HGETALL dhan:tick:IDX_I:13`
- `GET dhan:feed:status`
- `GET dhan:feed:last_crash`
- `LRANGE dhan:feed:starts 0 -1`
- `GET /api/agents/dhan-feed/logs`
- `/dev/repairs` for the incidents and their `_recovery` history

## Deployment and rollback

**Do not merge or deploy until CI and `make test-db` pass.** Merging to `main` deploys
automatically (deploy-poll).

Deploying:

1. **Migration 0024** (run by the `migrate` service):
   - creates `dhan.connections`;
   - widens `agents.incidents` `ck_incidents_source` to allow `'dhan'`;
   - changes no existing row.
2. **New scheduler jobs.** `dhan-session-check` and `dhan-monitor` appear on the scheduler's
   next start when `DHAN_CLIENT_ID` is set.
3. **Feed worker.** Only with `COMPOSE_PROFILES` containing `dhan` (this server:
   `coding,imaging`, so the worker stays off and `feed_not_running` is raised as soon as
   `DHAN_FEED_INSTRUMENTS` is set). To run it, add `dhan` and redeploy.
4. **Verify:**
   - `alembic current` → `0024`;
   - `POST /api/v1/dhan/auth/verify` → `connected`;
   - in market hours, `GET /api/v1/dhan/feed/health` → `ok`;
   - the `dhan-monitor` job has `last_status` ok.

Rolling back:

- **Code.** The deploy script's rollback, or redeploy the previous image tag. The old code
  ignores `dhan.connections` and the new settings keys (unknown DB keys are ignored).
- **Schema.** `alembic downgrade 0023`:
  - drops `dhan.connections`;
  - puts the narrower incident source check back as `NOT VALID`, so existing `dhan`
    incidents survive. Resolve or ignore them first if you want a clean table.
- **Frontend.** The new response fields are additive.

## Verified, and what still needs the server

Run on a development machine:

- ruff and `mypy --strict app`: clean.
- The non-DB suite: every new test passes. The only failures are the Windows-only ones
  already on `main`: ops shell scripts, POSIX signals and CLI subprocesses.
- What the new tests cover:
  - Profile API outcomes;
  - state transitions and replacement;
  - outage handling;
  - Redis outage and recovery;
  - WebSocket reconnect;
  - leader and standby;
  - crash-loop guard;
  - token change after an auth failure;
  - session fallback;
  - staleness, holidays and market-closed;
  - health codes;
  - recovery outcomes;
  - alert delivery failure;
  - Compose profile shape;
  - trading-safety invariants.

Not run here, because there is no Postgres, Redis or Docker on the machine:

- `tests/test_dhan_hardening_db.py` and the updated `tests/test_dhan_auth_db.py`: login
  verification, restart persistence, logout keeping accounts, replacement through
  `/settings`, reconnect auth and cooldown, monitor → bounded recovery → escalation;
- the migration round-trip (`tests/test_migrations_rollback.py`).

Run `make test-db` before merging.

**Not claimed:** production readiness of the live feed. Nothing here was run against Dhan's
real WebSocket or REST API. Before relying on it:

- confirm `ok` on a trading day;
- confirm recovery after a real disconnect (`docker restart aureon-redis`, then a network
  drop) and after a session expiry.
