# 38. Live market: quotes, live candles, movers, live screener, browser WebSocket

Real-time NSE/BSE data for the market terminal, built on the existing Dhan feed worker
(docs/28), the screener (docs/35) and Stock Signals (docs/37).

Rules that hold everywhere below:

- **Deterministic and read-only.** No AI model computes anything here. Nothing places, drafts,
  modifies or cancels an order; LIVE trading, MT5, risk controls and the approval path are
  unchanged.
- **One upstream Dhan socket.** Browsers never cause a Dhan connection. They register *demand*;
  the single `dhan-feed` worker subscribes what is wanted on its one connection.
- **No invented data.** A minute without trades has no bar. A missing previous close is `null`.
  A bar the server did not watch from its start is `partial: true`. Missing history is never
  bridged.
- **Confirmed stays confirmed.** Tick-built bars live in Redis only; they are never written to
  `trading.market_candles`. Screener runs, Stock Signals, notifications and backtests keep
  reading broker candles, and an unfinished candle can never produce a confirmed signal.
- Cookie session on every route, including the WebSocket handshake.

## 1. Audit (before this change)

| Area | Before | Now |
| --- | --- | --- |
| Dhan binary parser (`agents/trading/dhan/feed.py`) | All documented packets (ticker, quote, OI, prev close, full, disconnect) | Kept |
| Feed worker (`workers/dhan_feed.py`) | One socket, Redis leader lock, reconnect/backoff, auth-failure handling, validation, dedupe, out-of-order drop, Redis-outage buffering, stale marking, status record | Kept. **Added** managed subscriptions (§2), `prev_close_received_at`, subscription metrics |
| Subscriptions | Static `DHAN_FEED_INSTRUMENTS` only | Static + browser demand + live universes, applied as diffs on the open socket |
| Live % change | None: rankings used the last closed candle | Live quotes vs the **previous trading session's close** (§3) |
| Tick → candles | 1m bars only (research worker), possibly partial, stored | 1m/5m/15m/1h/1d session-aligned live bars with partial flags, in Redis (§4) |
| Gainers / losers / unusual volume / trending | Closed-candle Stock Signals run (15m) | Kept as `basis=confirmed` (default). **Added** `basis=live` (§5) |
| Screener | Confirmed/live modes, EMA/MACD/RSI/ADX/VWAP/volume | **Added** `change_pct` filter; `candle: live` now sees today's live bars and the forming bar on every timeframe (§6) |
| Chart | `GET /trading/candles` (stored rows) | **Added** `GET /trading/chart`: candles + EMA 8/32 + session VWAP + crossover markers + sessions (§7) |
| Browser streaming | SSE `/trading/stream` polls Redis hashes per tab; SSE `/stock-signals/stream` for notifications | Both kept unchanged. **Added** WebSocket `/trading/live/ws` with Redis pub/sub fan-out (§8) |

Not verified from the repository: the production feed's data plan, subscription limits on the
account, and how much 15m/1d history is stored per universe. Check them after deploying (§12).

## 2. Managed subscriptions (feed worker)

With `DHAN_FEED_DYNAMIC=true` the feed worker rebuilds its wanted set every
`DHAN_FEED_RECONCILE_SECONDS` (5 s) from, in priority order:

1. `DHAN_FEED_INSTRUMENTS` (static),
2. browser demand: Redis sorted set `dhan:feed:demand` (`SEG:ID` → expiry epoch), written by
   every API process while a WebSocket client watches the instrument (refreshed every
   TTL/3, `DHAN_FEED_DEMAND_TTL_SECONDS` = 90 s),
3. the market-live universes: Redis set `dhan:feed:universe`, written by the market-live worker.

Capped at `DHAN_FEED_MAX_INSTRUMENTS` (≤ 5000, Dhan's per-connection limit); what does not fit
is counted as `over_capacity`. Only the difference is sent on the open socket (unsubscribe
first, then subscribe, ≤ 100 instruments per message); a reconnect subscribes the whole current
set once. Expired demand is purged by the leader. A Redis error keeps the current set. With
nothing wanted the worker holds no socket (`state: waiting_demand`).

With `DHAN_FEED_DYNAMIC=false` (the default) behaviour is exactly as before.

Metrics in the status record (`dhan:feed:status`, also `GET /api/trading/live/status`):
`desired`, `subscribed`, `capacity`, `subscriptions.{static,demand,universe,over_capacity,
invalid,changes,subscribe_messages,unsubscribe_messages,reconciles,reconcile_errors}`,
`connects`, `last_connected_at`, plus the existing packets/duplicates/out_of_order/stale.

## 3. Quotes and the previous close

The market-live worker merges every `dhan:ticks` packet into a quote per instrument (LTP, last
trade time, day OHLC, cumulative volume, ATP, OI, Dhan's previous close).

**Previous close** = the close of the trading session before the session of the *last trade*
(so on Saturday the change is Friday vs Thursday):

1. `feed`: Dhan's prev-close packet, if it was received on that session's IST date before its
   15:30 close (the receipt time is stored as `prev_close_received_at`),
2. `daily_candle`: the stored daily candle of the previous trading day (weekends and
   `DHAN_MARKET_HOLIDAYS` skipped),
3. otherwise `null` (and no change is computed).

**Status:** `closed` outside the session (silence is expected, never "stale"), `stale` when the
market is open and nothing arrived for `DHAN_FEED_STALE_SECONDS` (or the feed marked it),
`no_data` without a price, else `live`. Status and `age_seconds` are re-judged at read time.

## 4. Live candles

`app/agents/research/live_market.py` `CandleBook`, run by the market-live worker.

- Buckets are IST session-aligned: 09:15, 09:20, …; the 15:15 hourly bar ends at 15:30; a daily
  bar is the IST date (open/high/low from Dhan's day OHLC when the feed mode carries it).
- Volume = the change in cumulative day volume between consecutive packets.
- **Duplicates** (same time, price, volume) are ignored. **Out-of-order** packets inside the
  forming bar only widen high/low. **Late** packets for a bar that already closed are dropped
  and counted.
- Bars close on a later packet or when the clock passes their end + `MARKET_LIVE_CLOSE_GRACE_SECONDS`
  (2 s), so the session's last bar closes at 15:30 without a further packet.
- **Continuity:** the first packet for an instrument after a start, a feed reconnect (its
  `connects`/`last_connected_at` changed) or a lost Redis subscription only sets the volume
  baseline: the bar it lands in is `partial` with unknown (`null`) volume. Forming bars at a
  reconnect become `partial`. Forming bars restored after a worker restart are `partial`.
- Dhan quote packets can coalesce trades, so a live bar's high/low may lie inside the exchange's
  true range. Broker candles replace live bars on the chart once fetched.

## 5. Market movers

Every `MARKET_LIVE_RANK_SECONDS` (5 s) the worker stores one row per universe instrument (quote
view + `avg_turnover` + time-of-day `rvol`) in `market:movers:<universe>` and publishes the top
10 on `market:rankings`. The API re-ranks the stored rows with the caller's filters.

- **Universe selection:** `MARKET_LIVE_UNIVERSES` (`fno_stocks` default; `nse_equities`,
  `bse_equities`, `indices`, `watchlists`), capped at `MARKET_LIVE_MAX_UNIVERSE`, by id.
- **Gainers / losers:** change > 0 largest first / < 0 smallest first, ties by instrument id.
- **Unusual volume:** `rvol ≥ STOCK_SIGNALS_UNUSUAL_RVOL`. `rvol` = today's volume ÷ the average
  cumulative volume earlier sessions had traded by the same time of day (from stored 15m
  candles, ≥ 3 complete sessions of the last 10, linear within a 15-minute slot). `null` in the
  first 15 minutes or without that history.
- **Liquidity filters:** `min_price` (`MARKET_LIVE_MIN_PRICE`, 10) and `min_avg_turnover`
  (`MARKET_LIVE_MIN_AVG_TURNOVER`, ₹5 crore: mean close × volume of the last 20 daily candles;
  unknown turnover is excluded and counted as `liquidity_unknown`).
- Only `live` quotes are ranked (`closed` outside the session, labelled by `market`);
  `include_stale=true` adds stale ones.
- **Coverage and scope:** `coverage` counts universe size, with quote, live, stale, closed,
  no data, no previous close, illiquid, liquidity unknown, evaluated. `scope.market_wide` is
  true **only** for `nse_equities`, uncapped, with every instrument quoted; anything else is
  labelled as the universe it is. `quotes_as_of` = newest trade time, `computed_at` = ranking time.
- **Trending:** confirmed (default) is unchanged. `basis=live` ranks the live EMA/VWAP state
  (§6) by the live trend score, with the same agreement rule (state, EMA order, score > 50 /
  < 50).

## 6. Screener: live and confirmed states

- **Confirmed** (unchanged): screener `candle: confirmed`, Stock Signals runs and their
  notifications use closed broker candles only. A finished Stock Signals run is announced on
  the WebSocket as a `screener` event with `basis: confirmed`.
- **Live state** (new): every `MARKET_LIVE_SCREENER_SECONDS` (15 s), for
  `MARKET_LIVE_SCREENER_TIMEFRAMES` (default 15m), the worker joins stored broker candles with
  today's live bars and the forming bar and runs the **same** `stock_signals.evaluate` with the
  forming bar treated as closed at its end. Results go to `market:screener:<tf>`; changes are
  published with `basis: live`, `candle_closed: false`. A gap in trading time between stored
  history and live bars makes the state `UNAVAILABLE` (never bridged). Live states are never
  stored as signals and never notify anyone.
- **Screener `candle: live`** now appends today's live bars and the forming bar after the newest
  stored closed candle (all intraday timeframes and 1d), unless that would bridge a gap, a live
  bar has unknown volume, or Redis is down (then: stored candles only, as before).
- **New filter** `{"type": "change_pct", "op": ">"|"<", "value": n}`: % change against the
  previous session's last close in the window (`insufficient_data` when the window doesn't
  reach it). Other filters: `ema_cross`, `ema_position`, `price_vs_ema`, `vwap_position`, `rsi`,
  `adx`, `macd_cross`, `volume` (relative volume vs the preceding bars).

## 7. Chart data

`GET /api/trading/chart?instrument_id=&timeframe=1m|5m|15m|1h|1d&limit=10..2000&include_live=true`

- Stored broker candles (`source: broker`), then today's live bars after the newest stored
  closed candle (`source: ticks`, `partial`), then the forming bar (`is_closed: false`). A stored
  candle still forming gives way to the live forming bar.
- Per candle: `ema_fast` (8), `ema_slow` (32) (`STOCK_SIGNALS_EMA_FAST/SLOW`), `vwap` (session,
  IST, intraday only; `null` on bars without volume), plus volume.
- Indicators use `limit` + 200 warm-up candles, then the warm-up is cut, so values don't depend
  on the window start and match the screener and Stock Signals for the same bars (tested).
- `markers`: EMA crossovers (`bullish_cross`/`bearish_cross`), `confirmed: false` on the forming
  bar. Leaving "no side yet" (equal EMAs) is not a cross, as in the screener.
- `sessions`: every trading session in range (`date`, `open`, `close`, UTC), for boundaries.
- Continue live with the WebSocket topic `candle:<instrument_id>:<tf>`.

## 8. Browser streaming

### Why a WebSocket (and the SSE streams stay)

Subscriptions change constantly (scrolling a watchlist, switching charts). SSE fixes the
symbols in the URL, so every change means a reconnect, and each EventSource holds one of the
browser's ~6 HTTP/1.1 connections per origin. A WebSocket allows subscribe/unsubscribe on one
connection and per-connection backpressure. `GET /api/trading/stream` and
`GET /api/trading/stock-signals/stream` are unchanged.

### Connection

`wss://<host>/api/trading/live/ws`, same cookie session as REST. The server accepts, then:

| Close code | Meaning | Client action |
| --- | --- | --- |
| 4401 | not signed in | go to login, don't retry |
| 4403 | Origin not in `CORS_ORIGINS` (cross-site WebSocket hijacking guard) | configuration error |
| 4429 | more than `MARKET_WS_MAX_CONNECTIONS_PER_USER` (8) sockets (counted across API processes) | reuse one socket per tab |
| 4008 | slow consumer | reconnect (backoff) |
| 4001 | session ended (re-checked every `MARKET_WS_SESSION_CHECK_SECONDS`) | login |
| 1008 | more than `MARKET_WS_MESSAGES_PER_MINUTE` (120) client messages | reconnect later |
| 1001 | server shutting down | reconnect |

Before a refusal close an `error` message carries the code (`unauthorized`,
`forbidden_origin`, `too_many_connections`); before 4001/1008 a `bye` says why.

### Client → server

```json
{"op": "subscribe", "id": "c1", "topics": [
  {"kind": "quote", "instrument_id": 101},
  {"kind": "candle", "instrument_id": 101, "timeframe": "5m"},
  {"kind": "rankings", "universe": "fno_stocks"},
  {"kind": "screener", "timeframe": "15m"}]}
{"op": "unsubscribe", "id": "c2", "topics": [{"kind": "quote", "instrument_id": 101}]}
{"op": "ping", "id": "c3"}
```

Topic names: `quote:<id>`, `candle:<id>:<tf>`, `rankings:<universe>`, `screener:<tf>`. At most
`MARKET_WS_MAX_TOPICS` (200) per connection. Unknown instruments, universes and timeframes are
rejected per topic; the connection stays open.

### Server → client

Every message has `type` and a per-connection `seq` (1, 2, 3, … no gaps). Schemas are in
OpenAPI as `Ws*` models; `GET /api/trading/live/protocol` returns limits and close codes.

```json
{"type":"hello","connection_id":"…","server_time":"…","heartbeat_seconds":15,"max_topics":200,"market":{"open":true,"session":"open","now":"…"},"seq":1}
{"type":"ack","op":"subscribe","id":"c1","accepted":["quote:101","candle:101:5m"],"rejected":[{"topic":"rankings:x","reason":"unknown_universe"}],"seq":2}
{"type":"snapshot","topic":"quote:101","kind":"quote","available":true,"data":{…LiveQuoteOut…},"seq":3}
{"type":"snapshot","topic":"candle:101:5m","kind":"candle","available":true,"closed":[…today's live bars…],"forming":{…},"seq":4}
{"type":"quote","topic":"quote:101","instrument_id":101,"key":"NSE_EQ:2885","ltp":"2510.5","ltt":"…","prev_close":"2500","prev_close_source":"feed","change":"10.5","change_pct":"0.4200","volume":"1200345","status":"live","age_seconds":0.2,"received_at":"…","seq":5}
{"type":"candle","topic":"candle:101:5m","instrument_id":101,"key":"NSE_EQ:2885","timeframe":"5m","ts":"2026-11-02T04:30:00+00:00","end":"…","open":"2505","high":"2512","low":"2504","close":"2510.5","volume":"5320","ticks":41,"partial":false,"closed":false,"last_trade_at":"…","source":"ticks","seq":6}
{"type":"rankings","topic":"rankings:fno_stocks","universe":"fno_stocks","computed_at":"…","quotes_as_of":"…","market":{…},"coverage":{…},"scope":{"universe":"fno_stocks","market_wide":false,…},"top":{"gainers":[…],"losers":[…],"unusual_volume":[…]},"seq":7}
{"type":"screener","topic":"screener:15m","basis":"live","timeframe":"15m","computed_at":"…","changes":[{"instrument_id":101,"state":"BULLISH_PENDING","previous_state":"MIXED","candle_closed":false,…}],"seq":8}
{"type":"screener","topic":"screener:15m","basis":"confirmed","timeframe":"15m","run_id":"…","boundary":"…","changes":[…],"seq":9}
{"type":"heartbeat","server_time":"…","market":{…},"feed":{"connected":true,"state":"running","redis":"ok"},"seq":10}
{"type":"status","redis":"down","resync":false,"seq":11}
{"type":"status","redis":"ok","resync":true,"seq":12}
{"type":"error","code":"bad_message","detail":"…","seq":13}
{"type":"bye","reason":"session_ended","seq":14}
```

### Delivery guarantees

- **Snapshot first.** Each accepted topic gets a `snapshot` after the `ack`; updates arriving
  while it is read are held and delivered after it.
- **Coalescing.** Quotes, forming candles and rankings keep only the newest per topic for a
  slow client (never an overflow). A closed candle removes a queued forming copy of the same bar.
- **Bounded queue.** Closed candles, screener events, snapshots and acks queue up to
  `MARKET_WS_OUTBOX_MAX` (1000); overflow, or one send blocked for
  `MARKET_WS_SEND_TIMEOUT_SECONDS` (10), closes with 4008.
- **Redis fan-out.** Each API process holds one Redis subscription to `market:quotes`,
  `market:candles`, `market:rankings`, `market:screener`. On loss: `status redis=down` to every
  client, retry with backoff; on recovery `status redis=ok resync=true` and a fresh snapshot of
  every topic.
- **Heartbeat** every `MARKET_WS_HEARTBEAT_SECONDS` (15) when nothing else was received.
- Subscriptions are per connection: after a reconnect the client subscribes again.

## 9. Endpoints

| Method | Path | Auth | Request | Response | Status |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/trading/live/quotes` | user | `instrument_id` (repeatable, ≤ 100) | `LiveQuotesOut` | new |
| GET | `/api/trading/live/status` | user | – | `LiveStatusOut` | new |
| GET | `/api/trading/live/protocol` | user | – | `LiveProtocolOut` | new |
| GET | `/api/trading/chart` | user | `instrument_id`, `timeframe`, `limit`, `include_live` | `ChartOut` | new |
| WS | `/api/trading/live/ws` | session cookie + Origin | §8 | §8 | new |
| GET | `/api/trading/stock-signals/market-movers` | user | + `basis`, `universe`, `min_price`, `min_avg_turnover`, `include_stale` | `RankingOut` (+ `basis`, `scope`, `market`; live fields on items) | extended (additive) |
| GET | `/api/trading/stock-signals/trending` | user | + `basis` | `RankingOut` | extended (additive) |
| POST | `/api/trading/screener/run` | user + CSRF | + `change_pct` filter | unchanged | extended (additive) |

`basis=live` answers 503 until the market-live worker has published (never an empty list
presented as "no movers"), 422 for a universe or timeframe it doesn't compute. REST routes
share the per-user budget `MARKET_USER_REQUESTS_PER_MINUTE`.

## 10. Redis layout

| Key / channel | Writer | What |
| --- | --- | --- |
| `dhan:tick:<ref>` (+ `prev_close_received_at`), channel `dhan:ticks` (+ `received_at`) | dhan-feed | unchanged otherwise |
| `dhan:feed:demand` (zset), `dhan:feed:universe` (set) | API / market-live | §2 |
| `market:quote:<ref>` | market-live | quote view (24 h) |
| `market:bar:<tf>:<ref>`, `market:bars:<tf>:<ref>` | market-live | forming bar / today's closed bars (≤ 500) |
| `market:movers:<universe>`, `market:screener:<tf>` | market-live | ranking rows / live states |
| `market:live:status`, `market:live:leader` | market-live | status record, leader lock |
| `market:ws:conns:<user>` (zset) | API | live sockets per user across processes |
| channels `market:quotes`, `market:candles`, `market:rankings`, `market:screener` | market-live | fan-out |

## 11. Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `DHAN_FEED_DYNAMIC` | false | Managed subscriptions (§2) |
| `DHAN_FEED_MAX_INSTRUMENTS` | 5000 | Subscription cap |
| `DHAN_FEED_DEMAND_TTL_SECONDS` | 90 | Browser demand lifetime |
| `DHAN_FEED_RECONCILE_SECONDS` | 5 | Reconcile cadence |
| `MARKET_LIVE_ENABLED` | true | The market-live worker |
| `MARKET_LIVE_UNIVERSES` | `fno_stocks` | Ranked / subscribed universes |
| `MARKET_LIVE_MAX_UNIVERSE` | 2500 | Cap per universe |
| `MARKET_LIVE_TIMEFRAMES` | `1m,5m,15m,1h,1d` | Live bars |
| `MARKET_LIVE_SCREENER_TIMEFRAMES` | `15m` | Live EMA/VWAP state |
| `MARKET_LIVE_RANK_SECONDS` / `_SCREENER_SECONDS` | 5 / 15 | Cadences |
| `MARKET_LIVE_QUOTE_THROTTLE_MS` | 250 | Publish/coalesce interval |
| `MARKET_LIVE_CLOSE_GRACE_SECONDS` | 2 | Clock close grace |
| `MARKET_LIVE_REFERENCE_REFRESH_SECONDS` | 900 | Universe, daily closes, profiles reload |
| `MARKET_LIVE_MIN_PRICE` / `_MIN_AVG_TURNOVER` | 10 / 50 000 000 | Liquidity defaults |
| `MARKET_WS_MAX_TOPICS` / `_MAX_CONNECTIONS_PER_USER` | 200 / 8 | Limits |
| `MARKET_WS_OUTBOX_MAX` / `_SEND_TIMEOUT_SECONDS` | 1000 / 10 | Backpressure |
| `MARKET_WS_HEARTBEAT_SECONDS` / `_SESSION_CHECK_SECONDS` / `_MESSAGES_PER_MINUTE` | 15 / 60 / 120 | |

No new secrets. The Dhan ones and the live-universe/liquidity ones are editable in Settings → Dhan.

## 12. Deploying (manual; nothing deploys automatically)

1. No migrations. Normal API redeploy picks up the routes.
2. Start the worker: `docker compose --profile dhan up -d market-live` (service added to
   `compose.yaml`, profile `dhan`, no ports, hardened like `dhan-feed`).
3. Turn on managed subscriptions: `DHAN_FEED_DYNAMIC=true` (Settings → Dhan re-executes the
   feed worker). Keep the universe within the account's subscription limit.
4. Keep daily and 15m history for the universe (previous closes, turnover, relative volume, live
   state): `DHAN_UNIVERSE_BACKFILL` should include the live universe for `1d`, and
   `STOCK_SIGNALS_UNIVERSES`/`_TIMEFRAMES` should cover `MARKET_LIVE_SCREENER_TIMEFRAMES`.
5. A reverse proxy in front of the API must pass WebSocket upgrades on `/api/trading/live/ws`
   (`Upgrade`/`Connection` headers, read timeout > 15 s).
6. Check during the session: `GET /api/trading/live/status` (feed `ok`, `subscriptions.demand`,
   `over_capacity` 0, `market_live.last_rank_at` fresh), and
   `GET /api/trading/stock-signals/market-movers?basis=live` (`coverage.live` ≈ universe size).

Rollback: stop `market-live`, set `DHAN_FEED_DYNAMIC=false`. Nothing persistent was written.

## 13. Tests

| File | Covers |
| --- | --- |
| `tests/test_live_market.py` | Session buckets; baseline/partial bars; no synthesized minutes; duplicates, out-of-order, late packets; timer close at 15:30; reconnect continuity; daily OHLC; previous close (feed vs daily candle, weekend, holiday, after-close receipt); quote status (live/stale/closed); time-of-day rvol; mover sorting, ties, liquidity, stale; EMA/VWAP agreement with the signal engine, VWAP session reset, no lookahead; markers; live state with forming bar; gap → unavailable |
| `tests/test_feed_subscriptions.py` | Priority and cap; diff-only (un)subscribe on the open socket; full resubscribe after reconnect; 100-per-message batching; demand + universe on one socket; demand expiry; Redis outage keeps the set; no socket without demand; static-only when off; prev-close receipt time |
| `tests/test_market_live_worker.py` | Ticks → Redis quotes/bars/channels; market closed; stale; feed reconnect → partial; Redis outage during flush; lost tick subscription; rankings snapshot and scope; market-wide rule; live screener (forming bar, gaps, change events); universe for the feed; restart restore; chart merge; screener live overlay never bridges a gap |
| `tests/test_live_hub.py` | Coalescing and order; closed supersedes forming; outbox overflow and blocked send → slow consumer; routing; snapshot before updates; topic limit; Redis interruption → status + resync; demand registration; per-user connection limit across processes |
| `tests/test_live_api.py` | Sign-in on every route; live quotes; protocol; chart; live movers (503 before data, stale, scope, 422) and trending; WebSocket refusal without session and with a foreign Origin; subscribe → ack → snapshot → update; topic limit; bad messages; rate limit; connection limit |
| `tests/test_route_auth.py` | The WebSocket path is pinned (the only non-HTTP route) |

## 14. Limitations

- **Rankings cover the subscribed universe only** (`fno_stocks` by default); `scope` says so.
  All NSE equities need `nse_equities`, enough subscription capacity and daily history.
- **Live high/low** come from feed updates, which may coalesce trades (§4).
- **The first bar after a start or reconnect is partial**; its volume is unknown.
- **Live state needs stored history up to the session**: with the default Stock Signals settings
  that is 15m for the Stock Signals universe; other instruments/timeframes are `UNAVAILABLE`.
- **Holidays** come from `DHAN_MARKET_HOLIDAYS`, as everywhere else.
- **Subscriptions are per WebSocket connection** and are not restored by the server.

## 15. Frontend integration (ASTRA-FRONTEND)

1. Regenerate the client (`pnpm api:gen`); new types: `LiveQuotesOut`, `ChartOut`,
   `LiveStatusOut`, `LiveProtocolOut`, `Ws*`, `RankingScopeOut`, `ChangePctFilter`.
2. **One socket per tab**, opened lazily: `new WebSocket(`${wsBase}/api/trading/live/ws`)`
   (same origin, cookie sent automatically). Keep a map `topic → refcount` so components share
   subscriptions; send `subscribe`/`unsubscribe` only on 0↔1 transitions. Unsubscribe charts and
   quotes of hidden tabs (`visibilitychange`).
3. **Reconnect** with exponential backoff + jitter (1 s → 30 s) on 1001/1006/1008/4008; on 4401
   or 4001 go to login; on 4403/4429 show an error. After every (re)connect, re-send one
   `subscribe` with all current topics. Treat no message for 2 × `heartbeat_seconds` as dead.
4. **Apply** `snapshot` then updates. Track `seq`; a gap means a lost message → re-subscribe that
   topic. On `status redis=down` show "live data paused"; on `resync=true` replace state from the
   snapshots that follow.
5. **Quotes:** show `ltp`, `change`, `change_pct`; badge by `status` (`live` / `stale` /
   `closed` / `no_data`); tooltip with `prev_close`, `prev_close_source`, `prev_close_date`,
   `age_seconds`.
6. **Charts:** load `GET /api/trading/chart`, then subscribe `candle:<id>:<tf>`. Key bars by `ts`:
   replace the bar with the same `ts`; `closed: true` finalizes it. Draw `partial` bars and
   `source: ticks` bars distinctly (e.g. hollow), markers with `confirmed: false` dashed.
   EMA/VWAP for the forming bar: re-fetch the chart on each closed bar, or compute the forming
   value client-side for display only (the server values are authoritative).
7. **Rankings:** subscribe `rankings:<universe>` or call `market-movers?basis=live`. Always show
   `scope.label` (never "market" unless `scope.market_wide`), `quotes_as_of`, and coverage.
   Outside the session label lists "last session".
8. **Screener / signals:** label `basis: live` as "forming bar, may change"; only
   `basis: confirmed` (and the existing notifications stream) are signals.
9. `candle: live` screener requests now include the forming bar; `candle_closed: false` marks it.
