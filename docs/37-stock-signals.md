# 37. Stock Signals: EMA 8/32 + session VWAP, rankings, notifications, portfolio map

The server watches NSE/BSE equities for EMA 8/32 crossovers confirmed by the session VWAP,
ranks the market, notifies users, and maps their Dhan holdings against those signals. The
browser displays results; it never computes or monitors signals.

Rules that hold everywhere below:

- **No invented market data.** Everything comes from stored Dhan candles and Dhan account
  snapshots. Missing, short or stale data is reported as `UNAVAILABLE` with the reason.
- **Closed candles only, no lookahead.** A candle still forming is dropped before any
  calculation, and nothing after the evaluation time is read.
- **Nothing here places, drafts, modifies or cancels an order.** Classifications and the
  rebalance preview are advisory. LIVE trading stays disabled; the only order path is still
  `propose_trade` → a person's approval → a PAPER intent.
- **Claude explains, never computes.** Prices, scores, allocations and charges come from
  deterministic code; Claude's tools return those numbers as text.
- Cookie session on every route; every write needs the CSRF header.

## 1. Audit (Phase 1, before this work)

| Finding | Where | Action |
| --- | --- | --- |
| Claude's screener tool always said "No finished screener run yet": it filtered runs by status `succeeded`, but screener runs end as `done`. It also read metric keys (`close`, `volume`) that screener results never store. | `app/ai/tools/investments.py` | Fixed (PR #70). Other `succeeded` uses (scheduler jobs, instrument sync, deploys) are separate domains and correct. |
| EMA 8 and 32 were in the stored indicator set, but only computed for instruments listed in a strategy; nothing evaluated a universe or reacted to a candle close. | `services/research.py` | New `stock-signals` lane, driven by candle closes. |
| 5m/15m/1h candles exist only from Dhan history (no resampling from ticks). | `services/research.py` | Runs fetch the history they need through the existing backfill queue. |
| Holdings were fetched live only, never stored. | `services/dhan_sync.py` | Snapshotted by the existing reconcile job (`trading.holding_snapshots`). |
| No sector or industry data anywhere. | instrument master | Sector exposure is reported as unavailable. |
| No Nifty 50 constituent list (the index itself is `IDX_I:13`). | | The index is the relative-strength benchmark. |
| `research-worker` is under the `research` compose profile; `dhan-feed` under `dhan`; the scheduler always runs. | `compose.yaml` | Confirmed. The signals need the research worker running. |
| Known, unrelated: the paper-intents allowlist test conflicts with `app/ai/approvals.py`. | `tests/test_paper_db.py` | Out of scope here. |

What was **not** verified from the repository: the live server's candle freshness, instrument
sync state and session data. Check them after deploying (§10).

## 2. The signal engine

`app/agents/research/stock_signals.py` (pure). It reuses `indicators.ema` (SMA-seeded),
`indicators.vwap` (anchored to the IST day, so it resets at each NSE/BSE session) and the
screener's `find_cross`, so a signal agrees with the chart, the stored indicators and the
screener to the digit.

| State | Rule (on the newest closed candle) |
| --- | --- |
| `BULLISH_CONFIRMED` | EMA 8 crossed above EMA 32 within the last `STOCK_SIGNALS_CONFIRM_BARS` bars (default 4), and a close on the cross bar or after it was above the session VWAP. |
| `BULLISH_PENDING` | That cross, with no close above VWAP yet (or no VWAP). |
| `BEARISH_CONFIRMED` / `BEARISH_PENDING` | The mirror. |
| `BULLISH_TREND` | No recent cross; EMA 8 > EMA 32 and close > VWAP. |
| `BEARISH_TREND` | No recent cross; EMA 8 < EMA 32 and close < VWAP. |
| `MIXED` | Anything else, including no VWAP. |
| `UNAVAILABLE` | Fewer than 2 × slow (64) closed candles, or the newest closed candle is more than `STOCK_SIGNALS_STALE_BARS` bars behind, counted in trading time (a weekend or a holiday is not staleness). |

- The **cross time** (close of the cross bar) and the **confirmation time** (close of the first
  bar beyond VWAP) are stored separately.
- A touch of zero carries the previous side (as in the screener), so EMA 8 = EMA 32 is not a
  cross.
- **Daily candles** have no intraday VWAP: each day's closing session VWAP is computed from that
  day's stored 15-minute candles. Without them a daily cross stays `PENDING` and a daily trend is
  `MIXED`.
- Supported timeframes: 5m, 15m (default), 1h, 1d. Sessions are 09:15-15:30 IST on weekdays not
  in `DHAN_MARKET_HOLIDAYS`; the last hourly bar (15:15) closes at 15:30.

## 3. Runs, the worker and schedules

`app/services/stock_signals.py`. A run evaluates one timeframe at one candle close.

1. **Universe:** `STOCK_SIGNALS_UNIVERSES` (default `fno_stocks`, `watchlists`, `holdings`),
   NSE/BSE equities only, capped at `STOCK_SIGNALS_MAX_UNIVERSE`.
2. **History:** one `system` request per instrument (plus the Nifty 50 benchmark) on the existing
   candle-backfill queue. The candle-backfill lane does the Dhan calls under the shared 5/s
   data budget and fetches only missing bars; a person's own chart request still goes first.
   The run waits up to `STOCK_SIGNALS_FETCH_TIMEOUT_SECONDS`, then evaluates what is stored.
3. **Evaluate** each instrument at the run's candle close (deterministic for a given close).
4. **Store** the latest state per instrument and one event per crossover.

**Trigger:** the research worker's `stock-signals` lane checks every 5 s and queues a run
`STOCK_SIGNALS_RUN_DELAY_SECONDS` (20 s) after each candle close during the session. Runs are
unique per (timeframe, close), so the trigger, the scheduler's safety net and retries never
evaluate the same close twice. A run left `running` for an hour is requeued; a failed run is
retried up to `STOCK_SIGNALS_MAX_ATTEMPTS`. The lane heartbeats its counters (runs, events,
confirmations, notifications) like every other lane.

**Dhan budget:** about 200 F&O stocks is about 200 history calls per 15-minute close (about 40 s
at 5/s). `nse_equities` (2,000+) every 15 minutes would use the whole budget; opt in knowingly.

**Scheduler jobs (IST):**

| Job | Cron | What |
| --- | --- | --- |
| `stock-signals-scan` | `31 9 * * 1-5` | Safety net: queues the first 15m close if the worker's trigger missed it. |
| `stock-signals-premarket` | `45 8 * * 1-5` | The previous session's signal states of every held instrument. |
| `stock-signals-eod` | `45 15 * * 1-5` | The day's crossovers and confirmations; queues the daily run when `1d` is configured. |
| `stock-signals-weekly` | `15 16 * * 5` | Replacement candidates for each holder (notifications, once a week). |

Job results are stored in the scheduler's run history like every other job.

## 4. Rankings and the trend score

`app/agents/research/stock_rankings.py` (pure) and `app/services/stock_rankings.py` (queries).
Every list reads the states stored by the **latest finished run**: no Dhan call per dashboard
refresh, the same numbers for everyone, and an explicit data time.

- **Gainers / losers:** by the real % change from the previous session's last close, never by
  the score.
- **Trending bullish / bearish:** by the score, among instruments whose state and EMAs agree with
  the direction.
- **Unusual volume:** relative volume ≥ `STOCK_SIGNALS_UNUSUAL_RVOL` (2.0). Relative volume is
  today's volume so far compared with the same bars of earlier sessions (time-of-day adjusted).
- **Recent crossovers:** the events list (§7).
- **Excluded from every list:** `UNAVAILABLE` instruments and illiquid ones (average turnover
  below `STOCK_SIGNALS_MIN_DAILY_TURNOVER`, ₹5 crore a session by default). `coverage` reports
  how many were left out.

**Score (0-100), for ranking only.** It is not a probability of profit and is never presented
as one. Each feature is scaled to −1 … +1 (positive = bullish):

| Feature | Scaling | Default weight |
| --- | --- | --- |
| `ema` | (EMA 8 − EMA 32) / EMA 32, in %, ÷ 2 % | 0.25 |
| `vwap` | (close − VWAP) / VWAP, in %, ÷ 1.5 % | 0.20 |
| `volume` | (relative volume − 1) ÷ 2, clipped 0 … 1, signed by the EMA direction | 0.10 |
| `rsi` | (RSI 14 − 50) ÷ 25 | 0.10 |
| `adx` | (ADX 14 − 15) ÷ 25, clipped 0 … 1, signed by +DI vs −DI | 0.15 |
| `momentum` | change over the last 8 bars, in %, ÷ 3 % | 0.10 |
| `relative` | today's % change minus Nifty 50's, ÷ 2 % | 0.10 |

score = 50 + 50 × Σ wᵢ·fᵢ / Σ wᵢ, over the features that could be computed. A missing feature
(no VWAP, no earlier sessions for volume, no Nifty series) is left out and listed in
`missing_features`. The weights are set in `STOCK_SIGNALS_SCORE_WEIGHTS` (JSON).

**Validating the weights.** On the server, against stored history:

```
docker compose exec api python -m app.services.score_validation --timeframe 15m --horizon 8 --instruments 50
```

It recomputes the score at every past bar from that bar's own past and groups the forward
returns `horizon` bars later by score band (mean forward %, hit rate, count). A useful score
shows returns rising with the band. It describes the past only. **The default weights have
not been validated yet: run this once there are a few weeks of 15m history, and adjust.**

## 5. Notifications

`app/services/stock_signal_notifications.py`. The research worker creates them after every
run, so they arrive whether or not anyone has the screen open.

| Kind | When | Who |
| --- | --- | --- |
| `bullish_cross` / `bearish_cross` | A new crossover | Watchers of the instrument, holders, users with `all_market` |
| `vwap_confirmed` | A pending crossover is confirmed | Same |
| `holding_reversal` | A held instrument flips bullish ↔ bearish with a confirmed crossover | Holders |
| `portfolio_trend_change` | A held instrument flips bullish ↔ bearish without one | Holders |
| `replacement_candidate` | Weekly: a liquid bullish instrument not held passes the filters | Holders |

- **Preferences:** `PUT /notifications/preferences` sets `enabled`, `kinds`, `timeframes`,
  `all_market` and `confirmed_only` (skip pending crossovers). With no saved preferences, all
  kinds are on, on all timeframes, for watchlists and holdings only.
- **No duplicates:** each notification is unique per (user, dedup key), so a retried run never
  notifies twice.
- **Payload:** instrument (id, symbol, exchange, segment), `signal_id`, timeframe, prices (close,
  EMA 8, EMA 32, VWAP, confirmation price and VWAP), cross, confirmation and data times, trend
  score, and `deep_link` (`/trading/signals/{id}`).
- **Stream:** `GET /stream` is Server-Sent Events. Each event is `event: notification` with
  `id:` set to the notification id, so reconnecting with `Last-Event-ID` (or
  `?last_event_id=`) replays what was missed. `hello` carries the cursor and the unread count;
  `bye` ends the stream (signed out, or the maximum duration). The polling fallback is
  `GET /notifications?after_id=`.
- DevOps notifications and Discord alerts are unchanged.

## 6. Portfolio signal map and rebalance preview

`app/agents/research/portfolio.py` (pure Decimal) and `app/services/portfolio_map.py`.

- **Holdings:** the latest `trading.holding_snapshots` of each Dhan account you own (all of them
  for an admin), written every 5 minutes in market hours by the existing `dhan-reconcile` job.
  Instruments are matched by ISIN, NSE first.
- **Price:** the newer of Dhan's holding LTP and the last closed candle, with its source and time.
- **Per holding:** quantity, average price, invested and current value, unrealized P&L (and %),
  allocation, EMA state, VWAP state, signal state, trend score, and data times.
- **Also:** available cash (latest funds snapshot) and data freshness.
- **Sector exposure:** reported as unavailable. There is no reliable sector source.

**Advisory classes** (first matching rule):

| Class | Rule |
| --- | --- |
| `MONITOR` | No usable signal (missing or `UNAVAILABLE`). |
| `REVIEW_RISK` | Bearish (`BEARISH_CONFIRMED` / `BEARISH_TREND`), or overweight (> `PORTFOLIO_MAX_WEIGHT_PCT`, 20 %) and not bullish. |
| `MONITOR` | `BEARISH_PENDING` or `MIXED`. |
| `POTENTIAL_ADD` | Bullish, score ≥ `PORTFOLIO_ADD_MIN_SCORE` (65), below the maximum weight. |
| `HOLD_CANDIDATE` | Bullish otherwise (including bullish but overweight). |

**Rebalance preview** (`POST /rebalance-preview`):

- Trims overweight holdings to the maximum weight.
- Spreads the freed value, plus cash above the buffer, over up to `PORTFOLIO_CANDIDATES` liquid
  bullish alternatives, in whole shares, each capped at the maximum weight.
- Shows Dhan delivery charges (the paper-trading schedule, `PAPER_DHAN_CHARGES` overrides), cash
  before and after, cash required, and the allocation before and after.
- Weak holdings are listed for review and never sold for being weak; a bullish crossover alone
  never replaces a holding.
- Nothing is stored, and no order is placed or drafted.

## 7. API (`/api/trading/stock-signals`)

| Method | Path | Auth | What |
| --- | --- | --- | --- |
| GET | `` | user | Crossover events. Filters: `timeframe`, `exchange` (NSE/BSE), `symbol` (prefix), `direction`, `confirmation` (confirmed/pending), `in_holdings`, `since`/`until` (with timezone), `min_trend_score`, `fresh_only`. Plus `sort` (±`crossed_at`, ±`trend_score`), `limit`, `offset`. |
| GET | `/{id}` | user | One event and the instrument's current state. |
| GET | `/states` | user | The latest state of every evaluated instrument (`state` repeatable, `exchange`, `in_holdings`, `include_unavailable`, paging). |
| GET | `/market-movers` | user | `kind` = gainers, losers or unusual_volume; `timeframe`, `exchange`, `limit`. With `coverage`. |
| GET | `/trending` | user | `direction` = bullish or bearish; `timeframe`, `exchange`, `limit`. With `coverage`. |
| GET | `/portfolio-map` | user | §6, for your Dhan accounts (an admin sees all). |
| POST | `/rebalance-preview` | user + CSRF | §6. Body: optional `max_weight_pct`, `cash_buffer_pct`, `max_new`, `min_trade_value`, `timeframe`. |
| GET | `/notifications` | user | Yours only. Filters: `unread_only`, `kind`, `instrument_id`, `timeframe`. Paging with `before_id` (newest first) or `after_id` (polling, oldest first). |
| PATCH | `/notifications/{id}/read` | user + CSRF | Another user's id answers 404. |
| POST | `/notifications/read-all` | user + CSRF | Optional `up_to_id`. |
| GET / PUT | `/notifications/preferences` | user (+ CSRF to write) | §5. |
| GET | `/stream` | user | SSE (§5). |

- **Rate limit:** the list, ranking and preview routes share the market terminal's per-user
  budget (`MARKET_USER_REQUESTS_PER_MINUTE`).
- **OpenAPI:** the routes carry `x-ui` metadata (module `stock-signals`, group `markets`), and
  the schemas are in `/api/openapi.json` for ASTRA-FRONTEND.

## 8. Claude tools (read-only; investments and report agents)

- **`get_stock_signals`:** recent crossovers with times and values.
- **`get_market_rankings`:** gainers, losers, trending or unusual volume, with coverage.
- **`get_portfolio_signal_health`:** the portfolio map as text.
- **`get_portfolio_candidates`:** alternatives to compare, explicitly not recommendations.
- **`get_rebalance_preview`:** the deterministic preview for Claude to explain.

All are `kind="read"`. None writes, approves or places anything, and the risk limits and the
paper-trading safeguards are unchanged.

## 9. Tables (migrations 0032-0034, additive)

| Table | Key / dedupe | Retention |
| --- | --- | --- |
| `trading.stock_signal_runs` | unique (timeframe, boundary) | 14 days after finishing |
| `trading.stock_signal_states` | PK (instrument_id, timeframe); keeps `previous_state` and `state_changed_at` | latest only |
| `trading.stock_signal_events` | unique (instrument_id, timeframe, direction, crossed_ts); a confirmation updates the row | `STOCK_SIGNALS_RETENTION_DAYS` (90) |
| `trading.stock_signal_notifications` | bigint id (stream cursor); unique (user_id, dedup_key) | 90 days |
| `trading.stock_signal_prefs` | PK user_id | — |
| `trading.holding_snapshots` | (broker_account_id, as_of) | 90 days |

## 10. Deploying

- Run the migrations (0032-0034) with the usual `migrate` step.
- Keep `COMPOSE_PROFILES=dhan,research`. The signals are computed by the research worker, which
  only runs with the `research` profile, and live ticks need `dhan`. Restart `research-worker`
  and `scheduler` after deploying (new lane, new jobs).
- After the first session, check:
  - the `stock-signals` lane heartbeat in the agents page (runs `done`, `last_counts`);
  - `GET /api/trading/stock-signals/market-movers` → `coverage.data_as_of` is today and
    `unavailable` is small;
  - `trading.stock_signal_runs` has one `done` row per 15-minute close.
- If `history_wait_timed_out` is often true, the Dhan budget is too small for the universe:
  shrink `STOCK_SIGNALS_UNIVERSES`, or raise the timeout.

## 11. Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `STOCK_SIGNALS_ENABLED` | true | The lane and its jobs. |
| `STOCK_SIGNALS_UNIVERSES` | `fno_stocks,watchlists,holdings` | Also `nse_equities`, `bse_equities`. |
| `STOCK_SIGNALS_TIMEFRAMES` | `15m` | Any of `5m,15m,1h,1d`. |
| `STOCK_SIGNALS_MAX_UNIVERSE` | 500 | Cap on instruments per run. |
| `STOCK_SIGNALS_EMA_FAST` / `_SLOW` | 8 / 32 | Fast must be smaller than slow. |
| `STOCK_SIGNALS_CONFIRM_BARS` | 4 | Window for VWAP confirmation. |
| `STOCK_SIGNALS_STALE_BARS` | 2 | Trading-time bars before data is stale. |
| `STOCK_SIGNALS_LOOKBACK_BARS` | 200 | Candles per evaluation. |
| `STOCK_SIGNALS_RUN_DELAY_SECONDS` | 20 | After each candle close. |
| `STOCK_SIGNALS_FETCH_TIMEOUT_SECONDS` | 300 | Wait for history before evaluating. |
| `STOCK_SIGNALS_MAX_ATTEMPTS` | 3 | Retries of a failed run. |
| `STOCK_SIGNALS_RETENTION_DAYS` | 90 | Events, notifications, holdings snapshots. |
| `STOCK_SIGNALS_SCORE_WEIGHTS` | §4 | JSON object. |
| `STOCK_SIGNALS_MIN_DAILY_TURNOVER` | 50000000 | INR per session (liquidity). |
| `STOCK_SIGNALS_UNUSUAL_RVOL` | 2.0 | Unusual-volume threshold. |
| `STOCK_SIGNALS_BENCHMARK` | `IDX_I:13` | Nifty 50; empty turns the relative feature off. |
| `STOCK_SIGNALS_STREAM_POLL_SECONDS` / `_HEARTBEAT_SECONDS` / `_MAX_SECONDS` | 3 / 15 / 3600 | The SSE stream. |
| `STOCK_SIGNALS_SCAN_CRON`, `_PREMARKET_CRON`, `_EOD_CRON`, `_WEEKLY_CRON` | §3 | Empty disables that job. |
| `PORTFOLIO_MAX_WEIGHT_PCT` | 20 | Overweight threshold and rebalance cap. |
| `PORTFOLIO_ADD_MIN_SCORE` | 65 | For `POTENTIAL_ADD` and candidates. |
| `PORTFOLIO_CASH_BUFFER_PCT` | 5 | Cash kept back in the preview. |
| `PORTFOLIO_MIN_TRADE_VALUE` | 1000 | Smaller preview trades are dropped. |
| `PORTFOLIO_CANDIDATES` | 5 | Alternatives per preview or weekly review. |

No new secrets: everything uses the existing Dhan credentials and database.

## 12. Tests

| File | Covers |
| --- | --- |
| `tests/test_stock_signals_engine.py` | Bullish and bearish crossovers; pending then confirmed with both times kept; trend and mixed states; VWAP session reset; daily VWAP from intraday candles; no lookahead (forming and future bars ignored); insufficient and stale data in trading time (weekend, holiday); session boundaries. |
| `tests/test_stock_rankings.py` | % change from the previous session; time-of-day relative volume; score bounds and direction; relative strength; weights renormalised; no lookahead in validation; buckets. |
| `tests/test_portfolio_calc.py` | Values, P&L and allocation; unpriced holdings; every classification rule; rebalance trims, whole shares, charges, cash, allocation after; weak holdings never sold. |
| `tests/test_stock_signals_db.py` | One event per cross, confirmed later on the same row; dedupe of runs; history requested on the shared queue; unavailable data; retries, failure and a stopped worker's run; the trigger; retention; rankings and coverage. |
| `tests/test_stock_signal_notifications_db.py` | Watchers and all-market users; preferences (off, confirmed only); never twice; read state; another user's notification is 404; CSRF; polling fallback; stream replay from Last-Event-ID. |
| `tests/test_portfolio_map_db.py` | Only the latest snapshot; owner and admin visibility; prices, classes and cash; the rebalance preview (CSRF, no intents created); holder notifications once; `in_holdings` flags. |
| `tests/test_stock_signals_api_db.py` | Sign-in on every route; filters, paging and sorting; static paths are not ids; rankings with coverage; Claude's tools report stored numbers and are read-only. |
| `tests/test_dhan_sync.py` | The reconcile job snapshots holdings. |

## 13. Limitations

- **Rankings refresh once per candle close** (15 minutes by default), not tick by tick; every
  response says its data time. Gainers and losers use the last closed candle, not the live LTP.
- **The trend score weights are defaults, not yet validated** on this market's history (§4).
- **No sector exposure** until a reliable sector source exists.
- **Holdings are as fresh as the reconcile job:** every 5 minutes in market hours, and only while
  Dhan is logged in.
- **A worker down for longer than the confirmation window** misses crossovers that started and
  ended in between. The trigger evaluates the newest close, not every close it missed. States
  catch up on the next run.
- **Holidays come from `DHAN_MARKET_HOLIDAYS`.** There is no built-in exchange calendar, so a
  holiday missing from that setting reads as missing data.
- **The ASTRA-FRONTEND screen is not part of this change:** the API, its OpenAPI schemas and the
  deep links are ready for it.
