# 35. Market data, screener and watchlists (Dhan, NSE/BSE)

Backend for the ASTRA-FRONTEND market terminal (`docs/market-terminal.md` in that repo):
Stock Screener, Market Explorer, Charts & Indicators, Watchlists, Futures & Options.

Rules that hold everywhere below:

- **No invented market data.** Every price and indicator comes from stored Dhan candles
  (`trading.market_candles`). Missing, short or stale history is reported as such, never filled.
- **A screener run never calls Dhan.** It reads stored candles only. History is fetched by a
  separate, rate-limited queue.
- **Nothing here places, modifies or cancels an order.** OBSERVE/PAPER boundaries are unchanged.
- Cookie session (`__Host-` SameSite=Strict) on every route; every write needs the CSRF header.

## 1. Audit (before this change)

| Area | State before | Now |
| --- | --- | --- |
| Instrument master sync (`dhan_sync.sync_instruments`, `dhan.instrument_sync_runs`) | Implemented | Kept. Fixed: rows without a `segment` were deactivated as `segment_disabled` instead of `missing_from_master`. |
| `GET /api/trading/instruments` | `q` on `symbol` only, one `instrument_type` | Server-side filters: `exchange`, `segment` (repeatable), `instrument_type` (repeatable), `underlying`, `expiry`/`expiry_from`/`expiry_to`, `option_type`, `strike_min`/`strike_max`, `include_inactive`; `q` searches symbol, trading symbol, display and company name, exact/prefix matches first. |
| Instrument sync status | Table only | `GET /api/trading/instruments/status` |
| Candles | Newest row could be the live bar, unmarked | `is_closed` on `CandleOut` |
| Indicators | EMA, MACD, ATR, VWAP, structure (stored, configured set only) | + RSI, ADX (+DI/−DI), SMA, Bollinger; `GET /api/trading/indicators/compute` for any EMA period on demand |
| Watchlists | Tables (migration 0028) only, no code | CRUD API, owner-only |
| Candle backfill queue | Table + settings only, no code | API + research-worker lane + `dhan-universe-backfill` job |
| Screener | Tables + `SCREENER_*` settings only, **no engine, no service, no route** | Engine, service, API, worker lane, status |

Root cause of the missing screener: migration 0028 and the `SCREENER_*`, `WATCHLIST_*`,
`CANDLE_BACKFILL_*`, `DHAN_UNIVERSE_BACKFILL*` settings shipped as "part 1" scaffolding
(CHANGELOG: "still unused"). No service, worker lane or router used them, RSI/ADX did not
exist server-side, and nothing filled candles for whole universes, so the frontend's
`POST /api/trading/screener/run` had nothing behind it.

## 2. Endpoints

All paths are under `/api`. "user" = any logged-in user; writes need `x-csrf-token`.

| Method | Path | Auth | Request | Response | Status |
| --- | --- | --- | --- | --- | --- |
| POST | `/trading/screener/run` | user + CSRF | `ScreenerRequest` | `ScreenerResponse` (200 done / 202 queued) | new |
| GET | `/trading/screener/runs` | user (own) | `limit`, `offset` | `Page[ScreenerRunOut]` | new |
| GET | `/trading/screener/runs/{id}` | user (own) | `sort`, `dir`, `limit`, `offset` | `ScreenerResponse` | new |
| DELETE | `/trading/screener/runs/{id}` | user (own) + CSRF | – | 204 (cancel) | new |
| GET | `/trading/screener/status` | user | – | `ScreenerStatus` | new |
| GET | `/trading/watchlists` | user (own) | – | `WatchlistOut[]` | new |
| POST | `/trading/watchlists` | user + CSRF | `{name, description?}` | `WatchlistOut` 201; 409 duplicate/limit | new |
| GET | `/trading/watchlists/{id}` | owner | – | `WatchlistOut` | new |
| PATCH | `/trading/watchlists/{id}` | owner + CSRF | `{name?, description?}` | `WatchlistOut` | new |
| DELETE | `/trading/watchlists/{id}` | owner + CSRF | – | 204 | new |
| POST | `/trading/watchlists/{id}/items` | owner + CSRF | `{instrument_id}` | `WatchlistItemOut` 201; 409 present/full | new |
| DELETE | `/trading/watchlists/{id}/items/{instrument_id}` | owner + CSRF | – | 204 | new |
| GET | `/trading/instruments` | user | filters above | `Page[InstrumentOut]` | extended (additive) |
| GET | `/trading/instruments/status` | user | – | `InstrumentStatus` | new |
| GET | `/trading/candles` | user | unchanged | `CandleOut[]` + `is_closed` | extended (additive) |
| GET | `/trading/indicators/compute` | user | `instrument_id`, `timeframe`, `specs`, `limit`, `candle` | `IndicatorOut[]` | new |
| POST | `/trading/candles/backfill` | user + CSRF | `{instrument_ids ≤200, timeframe, days}` | `BackfillQueued` 202 | new |
| GET | `/trading/candles/backfill` | user (own; admin all) | `instrument_id`, `status`, paging | `Page[BackfillOut]` | new |
| GET | `/trading/indicators` | user | unchanged | stored rows | unchanged |

Watchlists are personal: another user, admins included, gets 404 for a list, its items, and
for screening it. `instrument: null` on an item means the instrument is inactive (expired,
delisted, missing from the master). Limits: `WATCHLIST_MAX_PER_USER` (50),
`WATCHLIST_MAX_ITEMS` (500), name 1–40 characters.

Per-user budget: `MARKET_USER_REQUESTS_PER_MINUTE` (120) across screener runs, indicator
computes and history requests (429 `rate_limited`).

## 3. Screener

### Universe

| `universe` | Instruments (active, `broker = dhan`) |
| --- | --- |
| `kind: equity` | segment `NSE_EQ`/`BSE_EQ` per `exchange`, `instrument_type = EQUITY` |
| `kind: index` | segment `IDX_I`, `exchange` in the list |
| `kind: future` | `NSE_FNO`/`BSE_FNO`, `FUTIDX`/`FUTSTK`, expiry today or later (IST) |
| `watchlist_id` (uuid) | the caller's server watchlist; `exchange`/`kind` are ignored. A device-only list id (not a uuid) is 422; another user's list is 404 |

More than `SCREENER_MAX_UNIVERSE` (6000) instruments is 422 `universe_too_large`.
Indexed by `ix_trading_instruments_broker_segment_active` (migration 0028).

### Timeframes

Dhan's chart APIs serve **1m, 5m, 15m, 1h** (intraday, up to 90 days per request) and **1d**.
There is no 30m or 4h, and nothing stores 30m/4h NSE/BSE candles, so those are refused with
422 `timeframe_unsupported` rather than screened on empty data. VWAP filters need an intraday
timeframe (VWAP resets each IST day).

### Evaluation (app/agents/research/screener.py)

- **Confirmed vs live.** `confirmed` (default) drops any candle whose period has not ended
  (`ts + timeframe > now`) before computing anything, so a forming bar can neither trigger nor
  cancel a match. `live` evaluates the forming bar and marks the item `candle_closed: false`.
  For 1d a bar is "closed" once its day has ended.
- **No lookahead.** Every indicator value at bar *i* uses rows 0..*i* only (tested by
  recomputing on a truncated series).
- **Window.** The newest `max(SCREENER_LOOKBACK_BARS (300), warm-up)` real candles (gap fillers
  excluded) up to the cutoff, read per chunk with one LATERAL query on `pk_market_candles`.
- **EMA crossover** (`ema_cross`, e.g. 8/32): diff = EMA fast − EMA slow. A bullish cross at
  bar *i* means diff > 0 at *i* and the last non-zero diff before *i* was < 0. A touch at 0
  that turns back is not a cross. `within_bars: N` looks at the last N bars and requires the
  crossed side to still hold on the evaluated bar. `crossed_at` = open time of the crossing
  bar. `macd_cross` uses the same rule on MACD − signal (`within_bars` optional, default 1).
- **Positions:** `ema_position` (fast vs slow), `price_vs_ema` (close vs EMA n),
  `vwap_position` (close vs session VWAP, IST). `signal` is the first cross filter's
  `bullish_cross`/`bearish_cross`, else the first position filter's `above`/`below`.
- **Thresholds:** `rsi` (Wilder), `adx` (Wilder), `volume` (evaluated bar vs the mean of the
  `avg_period` bars *before* it).
- All filters are ANDed. `change_pct` is against the previous trading day's last close in the
  window (previous bar for 1d), `null` when the window does not reach it.
- Indicator keys in `items[].indicators`: `ema:<n>`, `rsi:<n>`, `adx:<n>`, `plus_di:<n>`,
  `minus_di:<n>`, `macd:<f>:<s>:<g>`, `macd_signal:…`, `macd_hist:…`, `volume`,
  `volume_avg:<n>`, `vwap`. Decimal strings.

### Missing and stale data

Instruments that cannot be judged are counted in `coverage`, never reported as matches or
non-matches:

| Outcome | When |
| --- | --- |
| `no_data` | no real candle for the timeframe up to the cutoff |
| `insufficient_data` | fewer bars than the filters need (e.g. EMA 8/32 within 3 needs 35), or a value is undefined (volume/VWAP on an instrument with no volume, such as an index) |
| `stale` | its newest bar is more than `SCREENER_STALE_BARS` (4) periods older than the newest bar in the universe (`universe_data_as_of`) |

Staleness is relative to the universe, so evenings, weekends and holidays do not mark the
whole market stale; `universe_data_as_of` says how old the newest data is. A run over a
universe with no candles at all says so in `detail` and points to the backfill queue.

### Jobs and caching (the contract)

- Universe ≤ `SCREENER_INLINE_MAX` (100): evaluated in the request → **200**, `status: done`.
- Larger: persisted as `queued` → **202** with the same `ScreenerResponse` shape,
  `status: queued`, `items: []`, `total: 0`, `run_id`. **A 202 is not "no matches".** Poll
  `GET /api/trading/screener/runs/{run_id}?sort=change_pct&dir=desc&limit=50&offset=0` until
  `status` is `done`, `failed` or `cancelled`.
- The research worker's `screener` lane claims queued runs (`FOR UPDATE SKIP LOCKED`), works
  them in chunks of `SCREENER_CHUNK` (200) with CPU work in a thread, checks for cancellation
  between chunks, and fails a run past `SCREENER_RUN_TIMEOUT_SECONDS` (300). A run left
  `running` by a stopped worker is failed after twice that. Runs older than
  `SCREENER_RETENTION_DAYS` (14) are purged.
- The same scan (universe + timeframe + candle + filters) while one is queued/running returns
  that run. A **confirmed** scan repeated within `SCREENER_CACHE_SECONDS` (300) *and the same
  bar period* returns the stored run (`cached: true`); sort and paging are applied to the
  stored matches (`trading.screener_results`), never recomputed. Live scans are not cached.
- At most `SCREENER_USER_ACTIVE_MAX` (3) queued/running scans per user (429).
- `GET /screener/status`: `worker` is `ok`/`stale`/`down` from the `screener` agent's
  heartbeat; `down` means queued runs will not start (the research worker is opt-in, §6).

### Agreement with charts

`GET /api/trading/indicators/compute` uses the same functions and the same candle window as
the screener, so for the evaluated bar `ema:8` etc. are identical to the digit (tested). The
RSI/ADX/SMA/BB formulas follow ASTRA-FRONTEND `src/trading/indicators.ts` and are tested
against its vectors (1e-8). Specs: `ema:n`, `sma:n`, `rsi:n`, `adx:n`, `atr:n`,
`macd:f:s:g`, `bb:n:k`, `vwap`; periods 1–500; at most 12 per call; `limit` ≤ the window.

## 4. Candle history (backfill)

- `POST /api/trading/candles/backfill` queues one `trading.candle_backfill_requests` row per
  instrument. Refused before queueing: 30m/4h (422), more than
  `CANDLE_BACKFILL_MAX_DAYS_INTRADAY` (90) / `_DAILY` (3650) days (422), more than
  `CANDLE_BACKFILL_USER_OPEN_MAX` (50) open requests (429). Per instrument `rejected`:
  unknown, not Dhan, inactive/expired (Dhan's chart API does not serve expired contracts),
  segment without chart history (only NSE_EQ, BSE_EQ, NSE_FNO, BSE_FNO, IDX_I, MCX_COMM).
- One open request per (instrument, timeframe): repeats are `already_open`.
- The research worker's `candle-backfill` lane works **one request at a time** through the
  Dhan adapter, so every call passes the shared Redis limiter (data APIs 5/s and 100 000/day,
  across processes). It fetches only what is missing (from the newest stored bar when stored
  history already covers the window), upserts, and flags intraday gaps. Failures are retried
  up to `CANDLE_BACKFILL_MAX_ATTEMPTS` (3) and then `failed` with the error; nothing is stored
  on failure. Without Dhan configured the lane stays idle and requests stay queued.
- `dhan-universe-backfill` (scheduler, `DHAN_UNIVERSE_BACKFILL_CRON`, default `55 16 * * 1-5` in the
  scheduler's timezone) enqueues `DHAN_UNIVERSE_BACKFILL` universes (`nse_equities`, `bse_equities`,
  `indices`, `fno_stocks`, `watchlists`) for `DHAN_UNIVERSE_BACKFILL_TIMEFRAMES` (default
  `1d`) and `DHAN_UNIVERSE_BACKFILL_DAYS` (400, capped at 90 for intraday). Cost: NSE equities
  daily ≈ 2 000–2 500 instruments → one call each per refresh (≈ 8 minutes at 5/s); the first
  400-day load is two calls each. Intraday for a whole exchange costs one call per instrument
  per refresh; prefer the `watchlists` universe for intraday.

## 5. Settings

New: `SCREENER_RUN_TIMEOUT_SECONDS` (300), `DHAN_UNIVERSE_BACKFILL_TIMEFRAMES` (`1d`). Now
read by code (declared in 0028): `SCREENER_*`, `WATCHLIST_*`, `CANDLE_BACKFILL_*`,
`DHAN_UNIVERSE_BACKFILL*`, `MARKET_USER_REQUESTS_PER_MINUTE`. The Dhan ones are editable in
Settings → Dhan.

## 6. Rollout

Inspection only (changes nothing):

```bash
docker compose ps
docker compose --profile research ps research-worker
docker compose exec api python -m alembic current
docker compose exec api python -m alembic history -r 0027:head
# Read-only queries with a read-only DSN for the production database (no compose service):
psql "$READONLY_DSN" -c "SELECT segment, count(*) FROM trading.instruments WHERE broker='dhan' AND is_active GROUP BY 1"
psql "$READONLY_DSN" -c "SELECT status, finished_at, error FROM dhan.instrument_sync_runs ORDER BY started_at DESC LIMIT 3"
```

Changes (after review and the normal deploy):

```bash
# 1. Migrations 0028 (if not yet applied) and 0029: additive. 0029 builds its index
#    CONCURRENTLY (no write lock on market_candles).
docker compose run --rm migrate
# 2. The API picks up the new routes on its normal redeploy.
# 3. Queued screener runs and history requests need the research worker (opt-in profile).
docker compose --profile research up -d research-worker
# 4. Optional: keep universes' daily candles current (Settings → Dhan, or env):
#    DHAN_UNIVERSE_BACKFILL=nse_equities,indices,watchlists
```

Rollback: `alembic downgrade 0028` drops only the 0029 index; `downgrade 0027` additionally
drops the 0028 tables (watchlists, runs and queued requests are lost; candles and instruments
are untouched).

## 7. Frontend integration (ASTRA-FRONTEND)

- The request body is `buildRequest()` from `src/trading/screener.ts`, unchanged. Flip
  `SCREENER_AVAILABLE` after `pnpm api:gen`.
- Handle **202**: show "scanning", poll `GET /screener/runs/{run_id}` (2–5 s), stop on
  `done`/`failed`/`cancelled`. Paging and sorting after `done` use the same endpoint.
- Show `coverage` (no data / insufficient / stale) next to the results, and
  `universe_data_as_of`. `candle_closed: false` marks live, unconfirmed results.
- `universe.watchlist_id` must be a server watchlist id (§2), not a device list id.

Illustrative only (shape, not real market data):

```http
POST /api/trading/screener/run
x-csrf-token: <token>

{"universe": {"exchange": ["NSE"], "kind": ["equity"], "watchlist_id": null},
 "timeframe": "15m", "candle": "confirmed",
 "filters": [{"type": "ema_cross", "fast": 8, "slow": 32, "direction": "bullish", "within_bars": 3}],
 "sort": {"field": "change_pct", "dir": "desc"}, "limit": 50, "offset": 0}
```

```json
{
  "as_of": "2026-11-11T06:46:02Z", "candle": "confirmed", "timeframe": "15m",
  "universe_size": 2150, "total": 1,
  "items": [{
    "instrument": {"id": 101, "broker": "dhan", "broker_instrument_id": "NSE_EQ:0000",
                   "symbol": "EXAMPLE", "trading_symbol": "EXAMPLE", "exchange": "NSE",
                   "segment": "NSE_EQ", "instrument_type": "EQUITY", "currency": "INR",
                   "lot_size": "1", "tick_size": "0.05", "expiry": null, "strike": null,
                   "option_type": null, "underlying": "EXAMPLE", "display_name": "EXAMPLE",
                   "name": "EXAMPLE LTD", "isin": null, "series": "EQ", "is_active": true},
    "last": "123.45", "change_pct": "1.2345",
    "indicators": {"ema:8": "122.1", "ema:32": "121.9"},
    "signal": "bullish_cross", "crossed_at": "2026-11-11T06:15:00Z",
    "data_as_of": "2026-11-11T06:15:00Z", "candle_closed": true
  }],
  "run_id": "00000000-0000-0000-0000-000000000000", "status": "done",
  "coverage": {"scanned": 2150, "matched": 1, "no_data": 0, "insufficient_data": 3, "stale": 2},
  "universe_data_as_of": "2026-11-11T06:15:00Z", "cached": false, "detail": null
}
```
