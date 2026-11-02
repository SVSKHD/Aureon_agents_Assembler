# Trading Phase D — Research agents

Six cooperating agents analyse stored market data. **Every number is deterministic** (pandas/numpy indicators and
Decimal money). The local Ollama model **only explains** finished results. Nothing in this phase places, modifies or
cancels an order, or creates or approves an order intent. Approved risk decisions are records only; acting on them is
Phase F (paper) and Phase G (live, held back).

```
                  research-worker (own process, profile `research`)
 ┌──────────────────────────────────────────────────────────────────────────────────┐
 │ tick (60 s): watch list = active strategy versions × instruments × timeframes    │
 │   │                                                                              │
 │   ▼  queue (bounded)       queue              queue             queue            │
 │ market-data ──────▶ technical-analysis ──▶ signal ──────────▶ risk               │
 │  adapter candles     indicators →           strategy →         policy checks →   │
 │  Dhan ticks → 1m     indicator_values       trading_signals    risk_decisions    │
 │  gaps + freshness                           (dedupe)           (no orders)       │
 │                                                                                  │
 │ backtest     ◀── trading.backtest_runs (status=queued, SKIP LOCKED)              │
 │ ai-research  ◀── Redis list research:ai:queue ──▶ Ollama ──▶ trading.ai_analyses │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

All six register in `agents.agents` with kind `trading`: `market-data`, `technical-analysis`, `signal`, `risk`,
`backtest` and `ai-research`.

- Each heartbeats its queue length, processed, dropped and error counters, and last error.
- Each can be paused with `POST /api/agents/{id}/pause`. A paused agent holds its current item until it is resumed.
- Each has its own `asyncio.Queue(maxsize=RESEARCH_QUEUE_MAX)`. When a queue is full the new item is **dropped and
  counted**; the next tick offers the same work again, so nothing piles up.
- The worker is a separate process from the API, the scheduler, the imaging worker and chat inference.

Migration **0007** adds one table, `trading.indicator_values`. Everything else uses the Phase A tables.

## 1. Market Data Agent (`market-data`)

- **Candles:** `adapter.candles()` is asked for bars newer than the last stored one (or from the oldest open gap). The
  results are upserted into `trading.market_candles`:
  - times in UTC
  - a `session` tag (Asia/London/NY)
  - the instrument foreign key
  - `source=broker`

  A real candle always replaces a tick-built candle or a gap filler.
- **Ticks:** when the `dhan-feed` worker runs, packets on `dhan:ticks` are aggregated into 1-minute candles
  (`source=ticks`). The aggregator uses LTP/LTT, and volume is the change in cumulative day volume. A minute is
  written only once a later packet proves it is complete, late packets are ignored, and it never overwrites a broker
  candle.
- **MT5:** the bot's export carries no candles, so `MT5Adapter.candles` raises `NotSupported`. The agent records
  that under `details.unsupported` and stores nothing; it never invents candles.
- **Gaps:** gaps are marked for intraday timeframes only. A bar missing *between the first and last real bar of the
  same UTC day* is inserted as a filler:
  - O=H=L=C = the previous close, volume 0
  - `is_gap=true`, `source=gap`

  Nights, weekends and holidays are never gaps. Indicators, signals and backtests read only `is_gap=false` rows.
- **Stale data:** a series is stale when its newest real candle closed more than `RESEARCH_STALE_BARS` bars ago.
  Stale series are listed in the agent's `details.stale` and logged once as a warning. The risk agent rejects on stale
  data.

## 2. Technical Analysis Agent (`technical-analysis`)

Computed on closed, non-gap candles, in pandas/numpy. Prices become floats only for this maths. Values are stored as
Decimal strings rounded to 8 places in `trading.indicator_values (instrument, timeframe, name, ts)`.

| name | values | definition |
|---|---|---|
| `ema:<n>` | `value` | Seeded with the SMA of the first n closes, then `prev + (close − prev)·2/(n+1)`. Periods are `RESEARCH_EMA_PERIODS` (9, 21) plus every strategy's `fast`/`slow`. |
| `macd:12:26:9` | `macd`, `signal`, `hist` | EMA12 − EMA26; signal = EMA9 of MACD (same seeding); hist = MACD − signal |
| `atr:14` | `value` | Wilder smoothing of the true range, seeded with the mean of the first 14 TRs |
| `vwap:<tz>` | `value` | (H+L+C)/3 weighted by volume, reset each day in `Asia/Kolkata` (Dhan) or `UTC` |
| `structure:3` | `swing_high`, `swing_low`, `trend` | Pivots 3 bars each side, strict on the left and ties allowed on the right. A pivot is reported only once it is confirmed (3 bars later). `trend`: up (HH+HL), down (LH+LL), else range. |

Query: `GET /api/trading/indicators?instrument_id=&timeframe=&name=&since=&limit=`.

## 3. Signal Agent (`signal`)

The agent evaluates the **latest version** of every active strategy whose `code_ref` has an implementation
(`app/agents/research/strategies.py`).

- `ema_cross`: BUY when EMA(fast) crosses above EMA(slow) on a closed bar; SELL on the cross below.
- `ema_pullback`: in an up-trend (fast > slow, close > slow), BUY when the bar's low touches EMA(fast) and it closes
  back above it; SELL is the mirror image.

The watch list lives in the version's params: `{"fast": 9, "slow": 21, "instrument_ids": [..], "timeframes": ["15m"]}`.

On each newest closed candle the agent writes BUY / SELL / NO_TRADE to `trading.trading_signals`. The signal's `inputs`
record the indicator values used, the strategy name, version, code_ref and params, and the bar count. Signals are
deduplicated on `(strategy_version, instrument, timeframe, candle_time)`. Versions are immutable:
`POST /api/trading/strategies` (admin, audited) creates v1 or the next version.

The `mt5:<mode>` strategies from Phase C are observed signals from the bot and have no implementation here, so they
are never re-evaluated.

## 4. Risk Agent (`risk`)

Every new BUY/SELL signal is checked against **each active account of that broker**. One row per account goes into
`trading.risk_decisions`, holding the policy as it was, the inputs, and every check with `ok` and detail. A decision is
approved only when no check fails.

| check | rejects when |
|---|---|
| `policy_present` | the account has no risk policy |
| `kill_switch_off` | `kill_switch` is on |
| `session_allowed` | the current session is not in `allowed_sessions` |
| `signal_fresh` | the signal's candle closed more than `RISK_SIGNAL_MAX_AGE_BARS` bars ago |
| `data_fresh` | the series is stale (see §1) |
| `credentials_ok` | `adapter.health()` is not ok (expired Dhan token, MT5 bot stale), or there is no adapter |
| `account_state_fresh` | the newest Dhan fund snapshot or MT5 terminal heartbeat is older than `RISK_ACCOUNT_STALE_SECONDS`, or missing |
| `position_size` | the proposed quantity (one `lot_size`) exceeds `max_position_size` |
| `exposure` | gross notional of the latest position snapshot plus the proposed one exceeds `max_exposure` |
| `daily_loss` | the latest snapshot's realized + unrealized P/L is below −`max_daily_loss` |
| `costs` | the round trip (spread + 2×slippage + 2×fee, bps) exceeds `RISK_MAX_COST_BPS` |
| `drawdown` | never rejects yet: reported as `ok: null` ("not evaluated: no equity history", until Phase F) |

Query: `GET /api/trading/risk-decisions?account_id=&signal_id=`. Users only see their own accounts.

## 5. Research / Backtesting Agent (`backtest`)

`POST /api/trading/strategies/backtest` queues the runs. Example body:

```json
{"strategy_version_id": "…", "instrument_id": 42, "timeframe": "15m",
 "start": "2026-08-01T00:00:00Z", "end": "2026-11-01T00:00:00Z",
 "grid": {"fast": [5, 9], "slow": [21, 34]},
 "costs": {"spread_bps": "2", "slippage_bps": "1", "fee_bps": "3"}, "quantity": "1"}
```

- **Runs:** the grid expands to one `trading.backtest_runs` row per combination (at most `BACKTEST_MAX_GRID`). The
  endpoint answers `202 {"run_ids": […]}`. The worker claims queued runs with `FOR UPDATE SKIP LOCKED` and runs them
  off the event loop.
- **No lookahead:**
  - the decision on bar i uses rows 0..i only
  - it is filled at bar i+1's **open**; the last bar's decision is never filled
  - a position still open at the end is closed at the last close and flagged `open_at_end`
- **Costs:** fills are adverse by spread/2 + slippage (bps), and fees are bps of notional per side. P/L, fees and
  metrics are **Decimal**.
- **Metrics:** trades, wins, losses, win rate, net P/L, gross profit/loss, expectancy, profit factor, max drawdown (on
  the closed-trade equity curve), fees, spread+slippage cost, `fill: "next bar open"`, bars and period.
- **Results:** `GET /api/trading/backtests` and `GET /api/trading/backtests/{id}` return the run, metrics and trades,
  labelled `source: "simulated"`.

## 6. AI Research Agent (`ai-research`)

`POST /api/trading/signals/{id}/explain` or `POST /api/trading/backtests/{id}/explain` returns `202`.

- **Queue:** requests go on the bounded Redis list `research:ai:queue` (`RESEARCH_AI_QUEUE_MAX`; 429 when full). The
  API returns 503 when `RESEARCH_AI_MODEL` is not set.
- **What the model sees:** the agent sends **only stored rows** (the signal with its inputs and risk decisions, or the
  run's params, costs and metrics) to the **local Ollama** model. There is no external API. The system prompt forbids
  inventing figures and forbids order recommendations.
- **Output:** a summary line plus the full text go to `trading.ai_analyses`; read them with
  `GET /api/trading/ai-analyses?signal_id=|backtest_run_id=`.
- **Separation:** the agent has no code path to order intents, signals or risk decisions; a test asserts no order
  intent exists after a full run.
- **Ollama load:** explanations run one at a time, and Ollama queues them behind chat. Lower the priority by leaving
  `RESEARCH_AI_MODEL` empty during heavy chat use.

## Deploy and validate

```bash
cd /opt/private-ai/aureon && git pull
docker compose build
docker compose run --rm migrate                       # 0007: trading.indicator_values (new table only)
nano .env                                             # RESEARCH_AI_MODEL=<a model from GET /api/models>, RISK_* as wanted
docker compose up -d api scheduler
docker compose --profile research up -d research-worker
docker compose logs --tail=30 research-worker         # research_worker_started adapters=[...]
curl -sS -b $J $B/api/trading/agents | python3 -m json.tool   # 6 research agents, fresh heartbeats
```

Create a strategy version that watches an instrument already in `trading.instruments` (Dhan: from the instrument
sync; MT5: register it with `PUT /api/trading/mt5/instruments/{symbol}`):

```bash
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' $B/api/trading/strategies \
  -d '{"name":"ema-9-21","code_ref":"ema_cross","params":{"fast":9,"slow":21,"instrument_ids":[42],"timeframes":["15m"]}}'
# within a minute or two:
curl -sS -b $J "$B/api/trading/indicators?instrument_id=42&timeframe=15m&name=ema:9&limit=3"
curl -sS -b $J "$B/api/trading/signals?instrument_id=42&limit=5"
curl -sS -b $J "$B/api/trading/risk-decisions?limit=5"
# a backtest with a small grid:
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' $B/api/trading/strategies/backtest \
  -d '{"strategy_version_id":"<id>","instrument_id":42,"timeframe":"15m","start":"2026-10-01T00:00:00Z","end":"2026-11-01T00:00:00Z","grid":{"fast":[5,9]}}'
curl -sS -b $J $B/api/trading/backtests/<run_id> | python3 -m json.tool | head -40
curl -sS -b $J -H "x-csrf-token: $CSRF" -X POST $B/api/trading/backtests/<run_id>/explain
curl -sS -b $J "$B/api/trading/ai-analyses?backtest_run_id=<run_id>"
docker stats --no-stream aureon-research              # stays under its 1.5 CPU / 1 GB limit
```

**Rollback:**

1. `docker compose --profile research stop research-worker`. The API endpoints stay, read-only.
2. `alembic downgrade 0006` drops only `trading.indicator_values`.

Rows already written to Phase A tables stay; to remove a strategy's signals, delete the strategy.

## Tests

- **`tests/test_research_unit.py`** (no DB):
  - indicator values on hand-computed fixtures: EMA seeding, MACD against its definition, Wilder ATR, VWAP daily
    reset, pivots reported only once confirmed
  - Decimal-string storage
  - the **no-lookahead test**: indicators and both strategies on truncated series equal the full series' prefix
  - backtest fills at the next open, and futures mutated after bar 200 do not change earlier trades
  - backtest P/L, costs and metrics (profit factor, drawdown, expectancy)
  - the EMA-cross detector, params validation and the grid cap
  - tick → 1m aggregation
- **`tests/test_research_db.py`**:
  - candle upsert with gap fillers that are later replaced by real bars, and freshness
  - `NotSupported` adapters reported rather than invented
  - indicators persisted and upserted
  - **signal dedupe**
  - a risk approval when everything is fresh
  - **risk rejection cases**: no policy, kill switch, stale account state, bad credentials, size/exposure limits,
    session, stale data plus old signal; NO_TRADE gets no decision
  - one full worker pass (market data → indicators → signal → risk)
  - the bounded queue dropping and counting
  - backtests via the API and worker (grid, cap, metrics, Decimal trades)
  - an AI explanation with a mocked Ollama (prompt rules, stored summary, 429 queue, 503 without a model)
  - no order intent created

## Open questions / risks

- **Floats in indicator maths:** indicators and strategy decisions use float64. It is reproducible and standard, but a
  decision exactly on a threshold could differ from a Decimal computation in the last bits. P/L, fees, risk and stored
  values are Decimal.
- **Account state is coarse.** Exposure and daily loss use the latest position snapshot (Dhan reconcile every 5 min;
  MT5 bot positions without money P/L). Currency conversion between instrument and account currency is not modelled.
  Both get stricter in Phase F.
- **Gaps:** gap detection is per UTC day, so a session that crosses UTC midnight is split there. That is fine for NSE
  (03:45–10:00 UTC); for 24 h FX/MT5 markets the daily maintenance break is not treated as a gap.
- **Ticks:** tick-built 1m candles depend on `DHAN_FEED_MODE` carrying volume (quote/full). In `ticker` mode volume
  is 0.
- **Ollama contention:** explanations share the one Ollama instance with chat; keep `RESEARCH_AI_QUEUE_MAX` small.
