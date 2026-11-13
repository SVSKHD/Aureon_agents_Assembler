# 36 — MT5 records API (`aureon.mt5.records/v1`, `aureon.mt5.bars/v1`)

A durable home in Postgres for everything the MT5 bot `aureon_mt5_2026` (1.7.x) records, plus
long-history closed candles, and an API to read it back (Spasta, the Claude tools) and export
the EMA 8/32 cross dataset.

- Built inside the existing Postgres **schema** `mt5` (0006, 0018): tables `mt5.records` and
  `mt5.bars` (migration 0031).
- Additive: the older bot's `POST /api/trading/mt5/ingest` (`aureon.mt5.push/v1`, [12](12-mt5-adapter.md) §3),
  its tables (`mt5.pushes`, `mt5.events`, `mt5.candles`, ...) and its behaviour are unchanged.
- Read-only towards trading: nothing here places, modifies or closes an order.

Code: `app/api/v1/mt5_records.py` (routes), `app/services/mt5_records.py` (validation, storage,
reads, purge, tool summary), `app/services/mt5_dataset.py` (pure dataset maths),
`app/schemas/mt5_records.py`, `alembic/versions/20261113_0031_mt5_records.py`.

## 1. Routes

| Route | Auth | What |
|---|---|---|
| `POST /api/trading/mt5/records` | bearer `MT5_RECORDS_TOKEN` | store a batch of records |
| `POST /api/trading/mt5/bars` | bearer `MT5_RECORDS_TOKEN` | upsert closed bars |
| `GET /api/trading/mt5/records` | session | filter + keyset paging, newest first |
| `GET /api/trading/mt5/records/stats` | session | counts per kind, freshness, lag, versions |
| `GET /api/trading/mt5/bars` | session | stored bars, ascending, `json` or `csv` |
| `GET /api/trading/mt5/dataset/crosses` | session | one row per completed EMA 8/32 cross, `json`/`csv`/`jsonl` |

The two POSTs are machine routes: no session, no CSRF, `hidden` in the UI manifest
(`app/api/ui.py` safety rule), exempt from the optional in-app Cloudflare Access check by exact
path **and** method (`MACHINE_POST_PATHS` in `app/core/cf_access.py`; a GET on the same path is
a session read and keeps the Access check). The GETs use the same signed-in session dependency
as the other `/api/trading/mt5` reads.

Errors use the API's shape: `{"detail": {"code": "...", "detail": "..."}}` (422 body errors
add `"fields"`).

| Status | When (POST routes) |
|---|---|
| `200` | stored (see the bodies below; a bad row/bar never fails the request) |
| `401` | missing/bad bearer (`hmac.compare_digest`, evaluated even for a wrong scheme) |
| `413` | body > `MT5_INGEST_MAX_BYTES` (2 MiB), or > `MT5_INGEST_MAX_DECOMPRESSED_BYTES` (8 MiB) after gzip |
| `415` | `Content-Type` not `application/json`, or `Content-Encoding` other than gzip/identity |
| `422` | not JSON (NaN/Infinity refused), wrong `schema`, bad envelope, more than `MT5_RECORDS_MAX_BATCH` rows or 5,000 bars |
| `429` | more than `MT5_RECORDS_RATE_PER_MINUTE` requests a minute for the token (both routes together); `Retry-After` set |
| `503` | `MT5_RECORDS_TOKEN` unset (`records_disabled`) or misconfigured (`records_misconfigured`: reused, or weak in prod), or the database is down (`storage_unavailable`, retry) |

The rate limiter is Redis; if Redis is down it fails open (the token still gates the route),
like push/v1.

## 2. Wire contract: `aureon.mt5.records/v1`

```json
{
  "schema": "aureon.mt5.records/v1",
  "batch_id": "6f1c2a3e-....",
  "rows": [
    {
      "record_id": "5f0c...32 hex chars",
      "kind": "event",
      "utc_time": "2026-11-13T09:30:03+00:00",
      "server_time": "2026-11-13 11:30:03",
      "version": "1.7.1",
      "account_login": 12345678,
      "symbol": "XAUUSD",
      "tf": "M30", "bar_time": "2026-11-13 09:00:00", "label": "CROSS", "side": "SHORT",
      "tag": "below 200", "price": 4038.1, "gap": 0.42, "atr": 6.1, "pair": null
    }
  ]
}
```

Rows are exactly what the bot's `store.build()` makes. The server lifts the seven envelope keys
into columns and stores every other key as `payload` (JSONB):

| Key | Column | Rule |
|---|---|---|
| `record_id` | `record_id uuid` PK | required; uuid4 hex (a dashed uuid is accepted too). The idempotency key |
| `kind` | `kind` | required; `^[a-z_]{1,32}$`. Unknown kinds are accepted (the bot can add kinds without a server change). Today: event, alert, position, sl_change, order, command, weekly, system |
| `utc_time` | `utc_time timestamptz` | required ISO 8601; no zone = UTC; more than 24 h in the future is rejected |
| `server_time` | `server_time timestamp` | broker clock, stored without a zone; may be null |
| `version` | `bot_version` | required, ≤ 64 chars |
| `account_login` | `account_login bigint` | integer (or digit string) or null |
| `symbol` | `symbol` | ≤ 64 chars or null |
| everything else | `payload jsonb` | any JSON; no NUL characters, nesting ≤ 16 |

Also stored: `bot_id` (`aureon_mt5_2026`), `batch_id`, `received_at`.

Each row is validated on its own. Insert is `ON CONFLICT (record_id) DO NOTHING`.

```json
{"accepted": 498, "duplicate": 1, "rejected": [{"record_id": "...", "index": 7, "reason": "kind must match ^[a-z_]{1,32}$"}]}
```

`duplicate` counts rows already stored plus a record_id repeated inside the same batch.
`index` is the row's position in `rows` (useful when the record_id itself is missing).

## 3. Wire contract: `aureon.mt5.bars/v1`

```json
{
  "schema": "aureon.mt5.bars/v1", "symbol": "XAUUSD", "timeframe": "M30", "source": "bot",
  "bars": [{"ts": "2026-11-13T09:00:00Z", "server_ts": "2026-11-13 11:00:00",
            "open": 4040.5, "high": 4044.4, "low": 4037.9, "close": 4038.1,
            "tick_volume": 1830, "spread": 16,
            "ema8": 4041.2, "ema32": 4041.6, "ema200": 4090.3, "atr14": 6.1}]
}
```

- `ts` = bar open time, UTC (no zone = UTC). Closed bars only. Up to 5,000 per request.
- `symbol` `^[A-Za-z0-9._#+-]{1,64}$`; `timeframe` like `M30`, `H1`, `D1` (upper-cased);
  `source` `^[a-z0-9_-]{1,32}$`, default `bot`.
- Required: `ts`, `open`, `high`, `low`, `close` (all prices positive, numeric(18,6) range).
  A bar with `high < low` or a non-positive price is rejected (alone). The rest are optional.
- Upsert on `(symbol, timeframe, ts)`. A resend with different values updates the row (the
  bot's weekly refetch corrects bars); identical values are left alone. A `ts` sent twice in
  one request keeps the last one.

```json
{"inserted": 4990, "updated": 8, "unchanged": 2, "rejected": [{"ts": "...", "index": 3, "reason": "high is below low"}]}
```

`mt5.bars` is never purged. `mt5.candles` (push/v1) is not reused: it is purged after
`MT5_CANDLE_RETENTION_DAYS`.

## 4. Reads

`GET /api/trading/mt5/records`: `kind` (repeatable), `symbol`, `account_login`, `from`, `to`
(on `utc_time`), `label` (matches `payload.label`, served by the GIN index), `limit` (200, max
1000), `cursor`. Newest first, keyset on `(utc_time, record_id)`; pass `next_cursor` back as
`cursor`. Answer: `{"rows": [...], "next_cursor": "..." | null}`.

`GET /api/trading/mt5/records/stats`: per kind the counts for today (UTC), 7 days and all
time with the newest `utc_time` and `received_at`; overall newest times; `lag_seconds` (now −
newest `utc_time`); `bot_versions_24h` (distinct `bot_version` received in the last 24 h).

`GET /api/trading/mt5/bars`: `symbol` (required), `timeframe` (M30), `from`, `to`, `limit`
(5000, max 20,000), `format=json|csv`. Ascending by `ts`.

## 5. The dataset: `GET /api/trading/mt5/dataset/crosses`

One row per **completed** EMA 8/32 cross, computed from `mt5.bars`. Query: `symbol`,
`timeframe` (M30), `from`, `to`, `format=json|csv|jsonl`. The maths are pure functions in
`app/services/mt5_dataset.py` with the bot's `labels.py` (1.7) definitions; on the bot's own
XAUUSD M30 sample they give the same 76 crosses, the same ATR and the same EMAs as `labels.py`.

- **EMA 8/32/200** of the close, `ewm(span, adjust=False)` (the first close seeds). Recomputed
  from the stored closes; the bot's `ema*` columns stay on the bars for display.
- **ATR**: Wilder ATR(14) on cleaned bars, seeded with the mean of the first 14 true ranges.
  **Bad-tick guard**: a lower wick longer than 2.0 × the ATR of the bars before it (below the
  body), or an upper wick longer than 2.5 × that ATR (above the body), ends at the body edge.
- `gap = ema8 − ema32`; a **cross** is a sign change of the gap on a closed bar, counted from
  the bar after the first valid ATR; the first trend of the series is not a cross (it was not
  seen starting). `side = +1` long, `−1` short; `ga = side × gap / atr`.
- **Window**: bars up to `to` are loaded, starting 600 bars before `from` (warm-up). Rows are
  crosses with `cross_ts` in `[from, to]` whose opposite cross also falls on or before `to`;
  the last, still-open cross is excluded.

Features (known at the cross):

| Column | Meaning |
|---|---|
| `symbol`, `timeframe`, `cross_ts` | the bar that closed with the new sign (open time, UTC) |
| `side` | +1 / −1 |
| `price`, `atr` | close and ATR on the cross bar |
| `gap_atr` | `ga` on the cross bar |
| `above_200` | close > ema200 |
| `with_ema200_side` | long above / short below the ema200 |
| `with_weekly_vwap` | close on the trade's side of the weekly VWAP: typical price (cleaned high + cleaned low + close) / 3 weighted by `tick_volume`, week from Monday 00:00 UTC, up to and including the cross bar; null when the week has no volume |
| `hour_utc`, `weekday` | of `cross_ts` (Monday = 0) |
| `steepness` | change of `side × gap` over the 3 bars into the cross, / ATR |
| `bars_since_prev_cross` | null for the first cross seen |

Outcomes (known only afterwards, prefixed `y_` so nobody trains on them by accident):

| Column | Meaning |
|---|---|
| `y_bars_to_opposite_cross` | bars from this cross to the next |
| `y_pts_to_opposite_cross` | side × (close at the opposite cross − close here) |
| `y_widened` | at some bar before the opposite cross, `ga ≥ 1.0` with `side × gap` rising on 4 candles in a row (3 rises) |
| `y_reached_wide_gap` | max `ga` before the opposite cross ≥ 2.5 |
| `y_max_gap_atr` | that max `ga` |
| `y_max_favourable`, `y_max_adverse` | price units, from cleaned highs/lows of the bars after the cross up to the opposite cross (≥ 0) |

JSON answers carry `definitions_version`, `date_from`, `date_to`, `bars_from` (first bar
loaded), `bars_used`, `row_count`, `feature_columns`, `outcome_columns` and `rows`. CSV and JSONL
carry the same in headers: `X-Aureon-Definitions-Version`, `X-Aureon-Row-Count`,
`X-Aureon-Date-From`, `X-Aureon-Date-To`, `X-Aureon-Bars-From`. Change
`DEFINITIONS_VERSION` whenever a definition changes.

## 6. Claude tool

`mt5_records_summary(symbol, days=7)` (agents investments + report, read-only): crosses by side
and by above/below 200, counts per label, positions opened/closed with wins and total result,
SL changes, the share of completed M30 crosses with `y_widened` in the period, freshness.
Aggregates only, never rows; account logins go through `app/ai/redact.py` (`[ACCOUNT_ID]`).

## 7. Settings

| Setting | Default | Meaning |
|---|---|---|
| `MT5_RECORDS_TOKEN` | unset (secret, re-auth) | bearer for the two POSTs; unset = 503. ≥ 32 chars, not a placeholder, and different from `MT5_INGEST_TOKEN`, `MT5_STATUS_TOKEN`, `SECRET_KEY`, `GITHUB_WEBHOOK_SECRET`, `GITHUB_TOKEN`, `SETTINGS_MASTER_KEY` and the `AUREON_API_*` tokens (checked per request, at ENV=prod startup and on a managed-settings write) |
| `MT5_RECORDS_MAX_BATCH` | 500 | rows per POST /records |
| `MT5_RECORDS_RATE_PER_MINUTE` | 60 | write requests per minute for the token |
| `MT5_RECORDS_RETENTION_DAYS` | 0 | 0 keeps records forever; otherwise the `mt5-ingest-purge` job deletes records older than this (by `utc_time`). Bars are never purged |

All four are managed settings (Spasta /settings → MT5). Body limits reuse
`MT5_INGEST_MAX_BYTES` and `MT5_INGEST_MAX_DECOMPRESSED_BYTES`. The purge job
`mt5-ingest-purge` (`MT5_INGEST_PURGE_CRON`) now also exists when the records token is set and
the retention is > 0; its push/v1 part (candles, events, push ids by their own retention) runs
as before.

### Creating the token

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Put the value in the server's `.env` as `MT5_RECORDS_TOKEN=...` (or set it in Spasta /settings
→ MT5, which needs your password) and the same value in the bot's environment. Never reuse
the `MT5_INGEST_TOKEN` value: the server refuses it (503 `records_misconfigured`).

## 8. Network exposure

Exactly like `POST /api/trading/mt5/ingest`: the bot on the Windows PC reaches the server over
Tailscale.

```
bot (Windows) ── HTTPS over Tailscale ──▶ tailscale serve ─▶ nginx :8088 (/api/) ─▶ api :8000
  https://svskhd8.taila439d5.ts.net/api/trading/mt5/records
  https://svskhd8.taila439d5.ts.net/api/trading/mt5/bars
```

- `svskhd8.taila439d5.ts.net` (tailnet devices only): reachable. `deploy/nginx/spasta-tailscale.conf`
  includes `aureon-api.conf`, whose `location /api/` already proxies every `/api/` path with
  `client_max_body_size 2m` (= `MT5_INGEST_MAX_BYTES`). No nginx change.
- `spasta.online` and `api.spasta.online`: **not** reachable for the bot. Both are behind
  Cloudflare Access at the edge, and `api-spasta-online.conf` requires the Access JWT on `/api/`.
  No bypass was added for these paths (none exists for push/v1 either).
- The app's optional Access middleware exempts only `POST` on these two exact paths.

## 9. curl

```bash
export AUREON=https://svskhd8.taila439d5.ts.net
```

```bash
curl -sS -X POST "$AUREON/api/trading/mt5/records" -H "Authorization: Bearer $MT5_RECORDS_TOKEN" -H 'Content-Type: application/json' -d '{"schema":"aureon.mt5.records/v1","batch_id":"'"$(python3 -c 'import uuid;print(uuid.uuid4())')"'","rows":[{"record_id":"'"$(python3 -c 'import uuid;print(uuid.uuid4().hex)')"'","kind":"system","utc_time":"2026-11-13T09:30:03+00:00","server_time":null,"version":"1.7.1","account_login":null,"symbol":null,"event":"curl_check"}]}'
```

```bash
gzip -c bars.json | curl -sS -X POST "$AUREON/api/trading/mt5/bars" -H "Authorization: Bearer $MT5_RECORDS_TOKEN" -H 'Content-Type: application/json' -H 'Content-Encoding: gzip' --data-binary @-
```

The reads need a signed-in session cookie (log in through Spasta, or `POST /api/auth/login`
with a cookie jar):

```bash
curl -sS -c jar -H 'Content-Type: application/json' -d '{"email":"you@example.com","password":"..."}' "$AUREON/api/auth/login"
```

```bash
curl -sS -b jar "$AUREON/api/trading/mt5/records?kind=event&kind=position&symbol=XAUUSD&label=CROSS&limit=50"
```

```bash
curl -sS -b jar "$AUREON/api/trading/mt5/records/stats"
```

```bash
curl -sS -b jar "$AUREON/api/trading/mt5/bars?symbol=XAUUSD&timeframe=M30&from=2026-10-01T00:00:00Z&format=csv" -o xauusd_m30.csv
```

```bash
curl -sS -b jar "$AUREON/api/trading/mt5/dataset/crosses?symbol=XAUUSD&timeframe=M30&format=csv" -D - -o crosses.csv
```

Liveness without the token: `curl -s -o /dev/null -w '%{http_code}\n' -X POST "$AUREON/api/trading/mt5/records"`
→ `401` once the token is set (`503` while it is unset).

## 10. Deploy and rollback

1. Merge; `docker compose run --rm migrate` (0031) and restart `api` + `scheduler` (the deploy
   poller does this).
2. Create the token (§7), set it on both sides, point the bot at the two Tailscale URLs.
3. Check: the liveness curl above answers 401; `GET /records/stats` shows the bot's version
   within a minute of its first batch.
4. Rollback: unset `MT5_RECORDS_TOKEN` (both POSTs answer 503; nothing else changes), or
   `alembic downgrade 0030`, which drops only `mt5.records` and `mt5.bars`.

## 11. Tests

`tests/test_mt5_records_unit.py` (no DB: row splitting, kinds, per-row rejection, token
missing/wrong/reused/weak in prod, tokens do not cross, size and content-type limits, gzip,
rate limit, cursor, bars validation, Access exemption, UI manifest, purge job),
`tests/test_mt5_dataset_unit.py` (synthetic crosses, bad-tick guard, open cross excluded,
golden XAUUSD M30 check), `tests/test_mt5_records_db.py` (storage, duplicates, filters and
paging, stats, bars upsert and correction, retention, the dataset from stored bars, every
route end to end, the Claude tool, migration 0031 up and down).
