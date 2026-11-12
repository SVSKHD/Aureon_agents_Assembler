# 11 — Trading Phase B: DhanHQ v2 (read-only)

**Status:** the adapter, live-feed worker, sync jobs and tests are done. It is **read-only**:
there is no order placement, modification or cancellation code anywhere, and execution stays
disabled (Phase G). No migration is needed, because Phase A already created
`trading.*`, `dhan.*` and `mt5.*`.

## Sources and confidence

The live dhanhq.co pages were not reachable from the build environment. The endpoints, fields
and packet layouts come from two sources, and anything neither confirms is marked
**unconfirmed** below:

- **The official SDK** `dhan-oss/DhanHQ-py` (v2.3.0, 2026-10-08): its paths, headers and
  `struct` formats.
- **A full markdown copy of the v2 docs** (2026-04-30).

Re-check the unconfirmed items against https://dhanhq.co/docs/v2/ before relying on them.
Nothing in the code invents an endpoint.

## Log in with the API key

The normal way to get a Dhan session. It follows
[dhanhq.co/docs/v2/authentication](https://dhanhq.co/docs/v2/authentication/) ("API key &
secret"). It is read-only: it only obtains the data session, and there is no order code.

**One-time setup on Dhan:**

1. Go to web.dhan.co → My Profile → Access DhanHQ APIs, and switch to **API key**.
2. Enter an app name and the redirect URL `https://<this site>/api/v1/dhan/auth/callback`.
   For the Tailscale address that is `https://svskhd8.taila439d5.ts.net/api/v1/dhan/auth/callback`.
3. Dhan shows the API key and secret. They are valid for 12 months.

**On /settings (Dhan group):** set `DHAN_CLIENT_ID`, `DHAN_API_KEY`, `DHAN_API_SECRET` and
`DHAN_REDIRECT_URL`. The two secrets need your password and are stored encrypted.

**Each session ("Log in to Dhan"):**

| Step | Who | What |
| --- | --- | --- |
| 1 | SPA → `POST /api/v1/dhan/auth/start` (admin + CSRF) | Aureon calls `POST {DHAN_AUTH_URL}/app/generate-consent?client_id=…` with the `app_id`/`app_secret` headers, records a one-time pending login bound to the admin's **user** (Redis, 10 min, keyed by the hash of a random 256-bit state), sets that state as the `aureon_dhan_login` cookie (HttpOnly, Secure, `SameSite=Lax`, path `/api/v1/dhan/auth`, 10 min), and returns the login URL |
| 2 | browser | Opens `{DHAN_AUTH_URL}/login/consentApp-login?consentAppId=…`, logs in on Dhan, and Dhan redirects it to the redirect URL with `?tokenId=…` |
| 3 | `GET /api/v1/dhan/auth/callback` (public) | Accepts the tokenId only for a live login started from this browser (the cookie); keeps it server-side, encrypted, first callback wins; redirects (303) to `/dev/settings?dhan_login=ready`. Otherwise `?dhan_error=no_pending_login` (unsolicited), `login_expired`, `duplicate_callback` or `bad_callback` |
| 4 | SPA → `POST /api/v1/dhan/auth/consume` (admin + CSRF) | Claims the pending login atomically and once, only for the admin who started it (even after they signed in to Spasta again). Calls `GET {DHAN_AUTH_URL}/app/consumeApp-consent?tokenId=…`. The token must be for `DHAN_CLIENT_ID`. Stores `accessToken` and `expiryTime` (IST, converted to UTC) as `DHAN_SESSION_TOKEN` / `DHAN_SESSION_EXPIRES_AT` |

Why step 3 is a redirect: the return from auth.dhan.co is a cross-site navigation, and the
session and CSRF cookies are `SameSite=Strict`, so it arrives without a session. The SPA page
it lands on is same-origin, so its `POST` carries both.

Why there is no state parameter in Dhan's URL: Dhan's flow has none, so the state travels in
the login cookie instead (`SameSite=Lax` so it rides Dhan's top-level redirect; the Spasta
cookies are Strict and do not). The pending login is bound to the admin's user id, not the
Spasta session: if the session expired meanwhile, the Settings page asks the admin to sign in
again and then finishes the login (`app/services/dhan_login_handoff.py`). What is refused:

| Case | Answer |
|---|---|
| another user presents the login cookie | `403 dhan_login_other_user`; the login stays for its owner |
| no login started from this browser | `409 no_pending_login` (callback: `?dhan_error=no_pending_login`) |
| older than 10 minutes | `409 login_expired` |
| used already (replay) | `409 login_already_used` |
| Dhan has not sent the browser back yet | `409 login_not_returned` |
| Dhan unreachable during the exchange | `502 dhan_unavailable`, `retry: true`: the tokenId was not spent, the login is kept |
| Dhan refuses the tokenId | `502 dhan_refused`, `retry: false`: start again |

The token's `dhanClientId` must also equal `DHAN_CLIENT_ID` and pass the Profile API, so a
tokenId from someone else's Dhan login is refused (`403 dhan_client_mismatch`). The state is
stored only as a hash and the tokenId only encrypted; neither, nor the consent id, is ever
returned or logged (the log scrubber also redacts `tokenId=` / `consentAppId=` in any URL).

**After login:**

- The session token is used for every Dhan call: the API adapter, the scheduler jobs and
  `dhan-feed`. The workers restart themselves on the settings change.
- When it expires, calls fall back to `DHAN_ACCESS_TOKEN` if one is set (read per request, no
  restart needed). Otherwise there is no usable token.
- `GET /api/v1/dhan/auth/status` (any signed-in user) reports `source`
  (`session`/`manual`/`none`), the expiry, `expires_soon` and `data_stale`.
- `GET /api/v1/system/integrations` reports Dhan `down` with "no valid Dhan session" while
  there is no usable token.
- Discord: an `error` alert once the session has expired ("trading data is stale"), and a
  `warning` within `DHAN_SESSION_ALERT_MINUTES` (60) of expiry. Each expiry time alerts once.
- `DELETE /api/v1/dhan/auth/session` (admin + CSRF) forgets the session. Dhan has no revoke
  call for these tokens, so it simply expires.

The two session keys are **managed**: they appear (masked) on /settings, but
`PUT`/`DELETE /api/v1/settings/{key}` refuse them with `403 managed_key`. Dhan allows 25
consents a day, and Aureon allows 8 login starts per admin per hour.

Every step is audited without values: `dhan_login_started`, `dhan_login_completed` (with the
expiry), `dhan_login_failed` (with an error code) and `dhan_logout`. A Dhan response body is
never returned or logged.

## Automatic login

Dhan tokens last 24 hours. To avoid logging in every day, the scheduler can log in itself
with your Dhan PIN and the TOTP secret from your authenticator setup
(`app/services/dhan_auto_login.py`).

**Set up:**

1. On Dhan, turn on TOTP and add it to an authenticator. Keep the base32 key it shows.
2. On /settings → Dhan, set `DHAN_PIN` and `DHAN_TOTP_SECRET`. Both are encrypted
   secrets: masked everywhere, revealed only with your password, scrubbed from logs.
3. The scheduler restarts on the change and adds the `dhan-auto-login` job.

**How it runs:** every `DHAN_AUTO_LOGIN_CRON` (default every 10 minutes) the job checks the
session. It logs in only when no session token is active, or the active one expires within
`DHAN_AUTO_LOGIN_BEFORE_MINUTES` (default 120). In practice that is about once a day. A login
calls `POST {DHAN_AUTH_URL}/app/generateAccessToken?dhanClientId=…&pin=…&totp=…`, then goes
through the same checks as "Log in to Dhan":

- the Profile API must confirm the token belongs to `DHAN_CLIENT_ID`; otherwise the token is
  not stored;
- the token is stored encrypted as `DHAN_SESSION_TOKEN` / `DHAN_SESSION_EXPIRES_AT`;
- the event is audited as `dhan_auto_login_completed` or `dhan_auto_login_failed`;
- every process (API, feed, scheduler, research) picks the new token up.

**Safety:**

- **A refusal is never retried.** If Dhan refuses the PIN + TOTP, the job remembers that
  pair (as an HMAC) and stops until the PIN or the secret changes, so it cannot lock your
  Dhan account. The job shows as failed in the scheduler, and the `auth` incident says so.
- **A logout sticks.** Logging out of Dhan pauses automatic login until someone logs in to
  Dhan again. That login also clears a remembered refusal.
- There is one login at a time, and never two within an hour.
- The PIN, the secret, the code and the token are never logged, returned or audited.
  `GET /api/v1/dhan/auth/status` shows `auto_login_configured` and `auto_login_last`
  (time, outcome, error code, expiry).
- It obtains the same read-only data session as the manual login. Orders still need LIVE
  mode (off) and Dhan's static IP whitelist.

**The trade-off:** with the PIN and the TOTP secret on the server, anyone who gets the
server *and* `SETTINGS_MASTER_KEY` can log in to your Dhan account. To turn it off, blank
`DHAN_AUTO_LOGIN_CRON` or clear `DHAN_PIN`.

## Credentials and token expiry

> Since the API key login, `DHAN_ACCESS_TOKEN` is only the **manual fallback**. The notes
> below describe that manual token.

- **Env only:** `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` are never stored in the DB or logs, and
  `repr()` of the client and feed omits them. The token is sent as the `access-token` header,
  and in the feed URL, which is never logged.
- **Lifetime:** Dhan web tokens last 24 h. Each health check calls `GET /v2/profile` and parses
  `tokenValidity` (`DD/MM/YYYY HH:MM`, IST) into `credentials_expire_at`.
  `GET /api/trading/accounts/{id}/health` reports:
  - `ok:false` once the token has expired, or Dhan rejects it (DH-901, 807–810)
  - a warning when less than `DHAN_TOKEN_WARN_HOURS` (4 h) is left
  - a warning when the **Data APIs plan** (`dataPlan`) is not `Active`
- **Admin notification:** the scheduled `dhan-reconcile` job writes a `warning` (or `error`) to
  the `scheduler` agent log, visible at `GET /api/agents/scheduler/logs`. An unusable token
  fails that job.
- **No automatic renewal.** `GET /v2/RenewToken` exists, but its response body is
  **unconfirmed**, and a renewal *expires the current token*. Getting that wrong locks the
  account out of the API mid-session. Renew on web.dhan.co, update `.env`, then
  `docker compose up -d` (API, scheduler, dhan-feed).
- **One client per deployment:** the adapter serves only the env-configured client id. A
  `trading.broker_accounts` row for another Dhan client id gets 501 ("different client id")
  rather than another client's data.

## Instrument references

Dhan security ids are unique only **within an exchange segment**, so every Dhan instrument is
referenced as `SEGMENT:SECURITY_ID`, for example `NSE_EQ:2885`, `NSE_FNO:52175` or
`IDX_I:13` (NIFTY 50). The same string is `trading.instruments.broker_instrument_id` and the
`symbols` / `underlying` value for the quotes and option-chain endpoints.

Segments and their feed codes: `IDX_I`=0, `NSE_EQ`=1, `NSE_FNO`=2, `NSE_CURRENCY`=3,
`BSE_EQ`=4, `MCX_COMM`=5, `BSE_CURRENCY`=7, `BSE_FNO`=8.

## What's implemented

| Feature | Dhan endpoint | Our side |
| --- | --- | --- |
| Health / token | `GET /v2/profile` | `/accounts/{id}/health`, `dhan.account_profiles` |
| Funds / margin | `GET /v2/fundlimit` (`availabelBalance` sic) | `/accounts/{id}/funds`, `dhan.fund_snapshots` |
| Holdings | `GET /v2/holdings` | `/accounts/{id}/holdings` |
| Positions | `GET /v2/positions` (`netQty` ≠ 0 only) | `/accounts/{id}/positions`, `trading.positions_snapshots` |
| Order book | `GET /v2/orders` | `/accounts/{id}/orders`, `trading.broker_orders` + `order_events` |
| Trade book | `GET /v2/trades` | `/accounts/{id}/trades`, `trading.executions` |
| Quotes | `POST /v2/marketfeed/quote` (≤ 1000 per request, grouped by segment) | `/quotes?broker=dhan&symbols=NSE_EQ:2885,…` |
| Daily candles | `POST /v2/charts/historical` (`toDate` exclusive) | adapter `candles(…, 1d)`; backfill job |
| Intraday candles | `POST /v2/charts/intraday` (1/5/15/60 min; ≤ 90 days per call, chunked) | adapter `candles(…)`; backfill job |
| Option chain + greeks / OI / IV | `POST /v2/optionchain`, `/optionchain/expirylist` | `/option-chain?underlying=IDX_I:13&expiry=…`, `/option-chain/expiries` |
| Instrument master | `api-scrip-master-detailed.csv` (compact also accepted) | `dhan.instruments` → `trading.instruments` |
| Live feed | `wss://api-feed.dhan.co?version=2&…&authType=2` | `dhan-feed` worker → Redis |

Mapping rules:

- **Orders:** `orderStatus` → normalized status as follows. Unknown values and `CLOSED` (super
  orders) become `UNKNOWN` instead of being guessed. The raw status is always kept.

  | Dhan status | Normalized |
  | --- | --- |
  | TRANSIT, PENDING, TRIGGERED | OPEN |
  | PART_TRADED | PARTIAL |
  | TRADED | FILLED |
  | CANCELLED | CANCELLED |
  | REJECTED | REJECTED |
  | EXPIRED | EXPIRED |

- **Order types:** `STOP_LOSS` → `STOP_LIMIT`, `STOP_LOSS_MARKET` → `STOP`.
- **Timestamps:**
  - Dhan's `YYYY-MM-DD HH:MM:SS` order and trade times are IST and are converted to UTC.
  - Candle `timestamp` values are already UTC epoch seconds. Daily candles are stamped at IST
    midnight.
  - A quote `last_trade_time` of `01/01/1980` means no trade yet, and the fetch time is used.
- **Numbers** go through `Decimal(str(x))`, never float arithmetic.
- **Unsupported timeframes:** Dhan has no 30m or 4h interval, so asking for one returns
  `NotSupported`.

## Rate limits

Limits come from Dhan's documented table. They are enforced in **Redis**, so the API,
scheduler and feed worker share one quota:

| Bucket | Limit | Used by |
| --- | --- | --- |
| non_trading | 20/s | profile, funds, holdings, positions, order book, trade book |
| data | 5/s and 100 000/day | historical + intraday charts |
| quote | 1/s | `/marketfeed/quote` |
| option_chain | 1 per 3 s | option chain + expiry list |

- **Retries:** rate-limit (DH-904/805), 429, 5xx and network errors are retried up to 3 times
  with backoff. Auth errors (DH-901, 807–810, HTTP 401) raise `CredentialsExpired` and are never
  retried.
- **Correlation:**
  - Every request gets an id (`aureon-…`).
  - Reconciliation stores it in `order_events.payload.request_id`, along with the run id
    (`recon-…`).
  - `order_events.correlation_id` is Dhan's `correlationId` when the order had one, otherwise
    the run id.
  - Broker orders are linked to `trading.order_intents` by matching `correlation_id`.

## Live market feed (`app/workers/dhan_feed.py`)

- **Process:** separate (compose service `dhan-feed`, profile `dhan`). It never runs inside the
  API, Ollama or imaging processes.
- **Subscription:** `DHAN_FEED_INSTRUMENTS`, at most 5000, sent as JSON in batches of 100
  (`RequestCode` 15/17/21 for ticker, quote or full).
- **Parsing:** little-endian packed packets with an 8-byte header. Parsed packets:

  | Code | Packet |
  | --- | --- |
  | 2 | Ticker |
  | 4 | Quote (wire order open, **close**, high, low) |
  | 5 | OI |
  | 6 | Prev close |
  | 8 | Full, with 5-level depth |
  | 50 | Disconnect |

  Codes 1 and 7 have no published layout and are skipped and counted, never guessed. A frame
  can contain several packets.
- **Output:** the latest values per instrument go to the Redis hash `dhan:tick:<SEG>:<ID>`
  (24 h TTL), and every packet is published as JSON on the `dhan:ticks` channel.
- **Backpressure:** a bounded queue (`DHAN_FEED_QUEUE_MAX`). When it's full the oldest packet is
  dropped and counted (`dropped`), so a slow consumer never blocks the socket.
- **Reconnects:** exponential backoff with jitter, up to `DHAN_FEED_MAX_BACKOFF_SECONDS`.
  Reason 805 (too many connections) waits the maximum.
- **Auth failures** (806–810: no data plan, expired or invalid token) **stop reconnecting**. The
  agent state becomes `error` and an error log is written. Fix the token and restart the
  worker.
- **Stale data:** an instrument with no packet for `DHAN_FEED_STALE_SECONDS` while NSE is open
  (09:15–15:30 IST, Mon–Fri) gets `stale=1` and is counted in the heartbeat. MCX evening
  sessions are not counted.
- **Registry:** registered as agent `dhan-feed` (kind `trading`). It is visible in
  `/api/trading/agents` and `/api/agents`. Pausing it disconnects; resuming reconnects.

## Static IP

Dhan requires a whitelisted static IP **only for order placement, modification and
cancellation**. That was enforced from 1 April 2026. Every endpoint used here works without
one.

`scripts/dhan_preflight.py` reports the current public egress IP from two echo services over
several samples, whether it's stable, whether it's carrier-grade NAT, and whether it matches
`DHAN_STATIC_IP`. Exit codes: 0 if stable and matching, 1 if not, 2 if unknown.

**JioFiber plus a Cloudflare Tunnel does not satisfy the requirement.** Home JioFiber gets a
dynamic, often CGNAT, IP, and the tunnel only carries inbound traffic. Phase G must not
enable LIVE until a real static egress IP exists and the preflight passes. Nothing here tries
to get around that.

## Scheduled jobs

These are created only when `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` are set. They run on the
Phase 5 scheduler, with retries, backoff and audit logs:

| Job | Default cron (IST) | Does |
| --- | --- | --- |
| `dhan-refresh-instruments` | `11 8 * * 1-5` | Downloads the master into `dhan.instruments` and `trading.instruments`, links them, and deactivates Dhan instruments that have vanished. It refuses to act on an empty file. |
| `dhan-auto-login` | `*/10 * * * *` | Logs in with PIN + TOTP when the session is missing or expires within `DHAN_AUTO_LOGIN_BEFORE_MINUTES` (see "Automatic login"). Only created when `DHAN_PIN` and `DHAN_TOTP_SECRET` are set. |
| `dhan-reconcile` | `*/5 9-15 * * 1-5` | Checks the token (and warns), then for each active `broker_accounts` row with this client id: orders + events, trades → executions, positions + funds snapshots, profile |
| `dhan-backfill-candles` | `41 16 * * 1-5` | `DHAN_BACKFILL_INSTRUMENTS` × `DHAN_BACKFILL_TIMEFRAMES` (`1d,15m`) over the last `DHAN_BACKFILL_DAYS`, upserted into `market_candles` with session tags. Only created when the list is non-empty. |

Reconciliation is idempotent:

- an unchanged order writes no event, and a status or fill change writes exactly one
- trades are deduplicated on `(account, exchangeTradeId)`
- trades for instruments not in the master are counted as `trades_unknown_instrument`, never
  stored against a made-up instrument

## Unconfirmed: verify on the live docs or the first real session

1. The `RenewToken` response body. That is why there's no automatic renewal.
2. The timezone of `tokenValidity` (assumed IST).
3. The maximum range per daily-historical request. We chunk by 365 days.
4. Whether intraday `interval` can be sent as an int. We send an int, as the SDK does; the
   docs example uses a string.
5. Feed:
   - whether the header length counts the header (we rely on fixed packet sizes instead)
   - the layouts of codes 1 and 7 (skipped)
   - whether last-trade time is UTC epoch (assumed, as in the SDK; check against the wall
     clock on the first live tick)
6. Which endpoints fall in the "Non Trading" 20/s bucket.
7. The pairing of HTTP statuses with DH-9xx codes. We key off `errorCode`.

## Deploy and validate

```bash
cd /opt/private-ai/aureon && git pull
nano .env        # DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN (web.dhan.co → DhanHQ Trading APIs), DHAN_FEED_INSTRUMENTS
docker compose build api && docker compose up -d api scheduler   # scheduler seeds the dhan-* jobs
python3 scripts/dhan_preflight.py      # informational for read-only use
```

Then, as admin, reusing `J`, `B` and `CSRF` from the auth smoke test:

```bash
# Register the account once; it is created in OBSERVE mode.
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' \
  -d "{\"broker\":\"dhan\",\"account_id\":\"$DHAN_CLIENT_ID\",\"base_currency\":\"INR\"}" $B/api/trading/accounts
ACC=<id>
curl -sS -b $J $B/api/trading/accounts/$ACC/health        # ok, credentials_expire_at
curl -sS -b $J $B/api/trading/accounts/$ACC/funds
curl -sS -b $J -H "x-csrf-token: $CSRF" -X POST $B/api/scheduler/jobs/dhan-refresh-instruments/run
curl -sS -b $J "$B/api/trading/instruments?broker=dhan&q=NIFTY&limit=5"
curl -sS -b $J "$B/api/trading/quotes?broker=dhan&symbols=IDX_I:13,NSE_EQ:2885"
curl -sS -b $J "$B/api/trading/option-chain/expiries?underlying=IDX_I:13"
curl -sS -b $J -H "x-csrf-token: $CSRF" -X POST $B/api/scheduler/jobs/dhan-reconcile/run
sleep 15; curl -sS -b $J "$B/api/agents/scheduler/runs?limit=3"

# Live feed (separate process)
docker compose --profile dhan up -d dhan-feed
docker compose logs -f dhan-feed                          # dhan_feed_connected
docker compose exec redis redis-cli HGETALL dhan:tick:IDX_I:13
curl -sS -b $J $B/api/trading/agents                      # dhan-feed: busy, packets rising
```
