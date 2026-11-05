# 27 — Dhan integration audit (2026-11-04)

A read-only review of the DhanHQ v2 integration ([11-dhan.md](11-dhan.md)) against what
Spasta needs. Dhan stays **read-only**: no order placement, modification, cancellation or
LIVE execution exists, and this audit adds enforcement and tests for that rather than any
write path.

"Operational" can only be confirmed on the server. Nothing in this table claims it: each row
names the check that would prove it. The feed's health is separate from the API's health
(see row 7).

## Progress matrix

| # | Capability | Implemented | Tested (non-DB, run) | Configured by default | Operational: how to verify |
|---|---|---|---|---|---|
| 1 | Client id, API key/secret | yes: env + runtime settings (secrets, re-auth to write) | registry/scrubber tests pass | no: needs `SETTINGS_MASTER_KEY` and the keys | `/settings` → Dhan → Test (GET /fundlimit) |
| 2 | Consent login (start/callback/consume) | yes: admin + CSRF; a pending key per session in Redis (10 min, GETDEL); client-id match | URL/expiry tests pass; CSRF/pending tests are DB-only | no: `DHAN_REDIRECT_URL` must be registered on web.dhan.co | one real login |
| 3 | Expiry, warnings, reconnect | yes: session token, else manual token; health warns; Discord alert; client rebuilt on change | pass | `DHAN_TOKEN_WARN_HOURS=4`, `DHAN_SESSION_ALERT_MINUTES=60` | an alert near a real expiry |
| 4 | Funds, holdings, positions, orders, trades | yes (GET only) | pass (fixtures) | needs a `broker_accounts` row | GET `/api/trading/accounts/{id}/funds` etc. |
| 5 | Daily + intraday candles, backfill | yes: chunked 365 d / 90 d | chunking passes; backfill upsert is DB-only | backfill off until `DHAN_BACKFILL_INSTRUMENTS` is set | `trading.market_candles` rows after the job |
| 6 | Quotes, option chain, Greeks | yes: Decimal throughout | pass | none | needs an active Dhan Data API plan |
| 7 | WebSocket feed worker | yes: binary parser, backoff + jitter, Redis hash + pub/sub, stale marking | parser/reconnect tests pass | **no: `profiles: [dhan]`, and this server's `COMPOSE_PROFILES=coding,imaging` does not start it** | `docker ps` shows `aureon-dhan-feed`; fresh `received_at` in `HGETALL dhan:tick:IDX_I:13` during market hours |
| 8 | Rate limits, reconciliation | yes: Redis Lua windows match Dhan's table; reconcile every 5 min, 09:00–15:55 IST | limiter tests need Redis (skipped); reconcile is DB-only | jobs exist only when Dhan is configured | `dhan-reconcile` job `last_status` |
| 9 | Persistence, response shapes | yes: `dhan.*` and `trading.*` tables (migration 0006); Decimal → JSON strings | schemas covered | — | `alembic current` = head |
| 10 | Health / integration status | partial: settings-based session status + feed heartbeat + reconcile status; live token check only in `/accounts/{id}/health` | DB-only | — | GET `/api/v1/system/integrations` |
| 11 | Read-only guarantee | **now enforced in code**: `assert_read_only` in `DhanClient._request` (GET, or POST only to the 5 market-data paths) | **new:** `tests/test_dhan_read_only.py` (AST + runtime refusal before any request) | — | — |

## Fixed in this change

- `DhanClient._request` refuses every method and path except GET and the market-data POSTs
  (`/marketfeed/quote`, `/charts/historical`, `/charts/intraday`, `/optionchain`,
  `/optionchain/expirylist`). The refusal happens before a request is built.
- `tests/test_dhan_read_only.py` pins the guarantee:
  - no order-named function in the package or on `DhanAdapter`/`DhanClient`;
  - every `_request` call in the source is a read;
  - no direct `post`/`put`/`patch`/`delete`;
  - writes are refused with nothing sent;
  - LIVE cannot load without `PHASE_G_APPROVED=yes`.
- The client's docstring was stale about where the token comes from; corrected.

## Open findings

Inferred from the code, not reproduced live. Findings 1–5 and 7 were fixed afterwards in
[28-dhan-operations.md](28-dhan-operations.md) (marked **fixed** below); 6 and 8 remain open.

1. **(fixed in 28)** **The feed's Redis writer can die silently** (`workers/dhan_feed.py`, the drain task). If
   one Redis error ends it, the socket and heartbeat keep going while the queue fills and
   drops ticks, and the health card stays green.
2. **(fixed in 28)** **Crash loop when unconfigured.** The worker exits 1 without Dhan credentials or
   `DHAN_FEED_INSTRUMENTS`, and `restart: unless-stopped` restarts it forever.
3. **(fixed in 28)** **The feed token is read once.** After the login session expires, the feed doesn't switch
   to a valid manual `DHAN_ACCESS_TOKEN`. It stops until the next settings change.
4. **(fixed in 28)** **An expired manual token keeps jobs alive.** `Settings.dhan_access_token()` returns an
   expired manual token (it never checks `exp`), so jobs keep running and failing every
   5 minutes with `CredentialsExpired`.
5. **(fixed in 28)** **The integrations card is blind to a missing feed.** If the `dhan-feed` agent was never
   registered (profile off), it reports no problem, and stale instruments are not counted as
   a problem.
6. **Order modifications are missed.** Reconciliation skips an order whose price or quantity
   changed while its status and filled quantity did not, so `broker_orders` keeps the old
   values.
7. **(fixed in 28)** **Rate-limit and Redis errors become 500s.** `RateLimitExceeded` and Redis errors in
   `call_adapter` are not mapped to a clean API error.
8. **Data gaps.** Holdings, quotes and option chains are never persisted, and ticks live only
   in Redis.

## Operator configuration still required

- **`.env`:**
  - `SETTINGS_MASTER_KEY`.
  - Add `dhan` to `COMPOSE_PROFILES` to run the feed worker.
- **`/settings` → Dhan:**
  - `DHAN_CLIENT_ID`, `DHAN_API_KEY`, `DHAN_API_SECRET`.
  - `DHAN_REDIRECT_URL` = `https://<site>/api/v1/dhan/auth/callback` (also register it on
    web.dhan.co).
  - `DHAN_FEED_INSTRUMENTS`.
  - `DHAN_BACKFILL_INSTRUMENTS`.
  - Optionally `DHAN_ACCESS_TOKEN` as the manual fallback.
- **Then:**
  - "Log in to Dhan".
  - `POST /api/trading/accounts` to register the account.
  - Run the instrument refresh.
  - Confirm the Data API plan is active.
