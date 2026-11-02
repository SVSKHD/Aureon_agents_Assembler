# Trading Phase F — Paper trading

PAPER mode works end to end: an order intent passes a risk decision and an admin's approval, then gets a
**simulated** fill at the current price plus configured spread, slippage and charges. The fill is written to
`trading.executions`, `trading.positions_snapshots` and the paper P&L ledger, and every step is logged in
`trading.order_events`.

**No broker order is ever sent.** The `BrokerAdapter` interface has no order methods, and nothing in this phase calls a
broker's order API. LIVE stays unreachable until Phase G, which needs your written approval.

```
POST /order-intents ─▶ risk decision ─▶ POST …/approve (admin) ─▶ Execution Agent ─▶ simulated fill
  PAPER account only    (status stays     risk re-checked now       approved PAPER      executions (is_simulated)
  idempotent on         PENDING_RISK      → APPROVED, or            intents only;       paper_ledger (realized, fees)
  correlation_id        or REJECTED)      REJECTED                  never LIVE          positions_snapshots (simulated)
                                                                     │                  order_events (FILLED)
                                                                     └─ LIMIT not marketable → SUBMITTED (resting)
scheduler: paper-process (every minute) fills resting LIMITs once marketable, expires DAY intents from a past day
scheduler: paper-reconcile (every 15 min) replays the ledger and flags drift
```

Code lives in:

- `app/agents/trading/paper/accounting.py`: pure Decimal maths (fill price, charges, lots, pip value, positions, P&L)
- `app/services/paper.py`: intents, risk, the Execution Agent, reconciliation, summary
- `app/api/v1/paper.py`: the API

Migration **0009** adds two tables, `trading.paper_ledger` and `trading.reconciliations`. Everything else uses the
Phase A tables.

## 1. Lifecycle

| Step | Status | `order_events.event_type` |
| --- | --- | --- |
| `POST /api/trading/order-intents` | `PENDING_RISK` | `CREATED` |
| risk run at creation passes / fails | `PENDING_RISK` / `REJECTED` | `RISK_PASSED` / `RISK_REJECTED` |
| `POST …/{id}/approve` (admin): risk **re-run** with current prices and limits | `APPROVED` or `REJECTED` | `APPROVED` / `RISK_REJECTED` |
| Execution Agent: simulated order accepted | `SUBMITTED` | `SUBMITTED` |
| fill | `FILLED` | `FILLED` (price, reference price and its source, charges, realized P&L, position, pip value) |
| LIMIT not marketable yet, or no fresh price | `SUBMITTED` (resting) | `SUBMITTED` (note) |
| `POST …/{id}/cancel` (creator or admin) while open | `CANCELLED` | `CANCELLED` |
| DAY intent still open on a later market day (IST for Dhan, UTC for MT5) | `EXPIRED` | `EXPIRED` |

Since Phase 20 the approve call goes through the approval engine (kind `trading.paper_intent`):
without a body it answers 202 with an approval id for the inbox; with `{"password"}` and a
single-approval policy it approves in the same call. See
[23-approval-engine.md](23-approval-engine.md#frontend-compatibility-paper-tab--inbox).

Every event carries the intent's `correlation_id`. Each risk run is a `trading.risk_decisions` row with
`order_intent_id`, the policy snapshot, its inputs (including the cost estimate) and per-check results.

**Idempotency:** `correlation_id` is unique. If you leave it out, a random one is assigned.

- The **same request again** returns the existing intent with **200**. No new row or events are written.
- A **different request** with that id returns **409** `correlation_conflict`.
- Two concurrent requests with the same id: the loser of the race also gets the existing intent.

Intents accept `MARKET` and `LIMIT` orders, validity `DAY`. Rejected before any row is written:

- `STOP` and `STOP_LIMIT` (not simulated)
- a quantity that is not a lot multiple
- an instrument from the other broker
- a missing or unexpected limit price

## 2. Risk checks on an intent

All of these must pass, at creation **and again at approval**:

| Check | Fails when |
| --- | --- |
| `policy_present` | the account has no risk policy |
| `mode_paper` | the account or the intent is not PAPER |
| `quantity_valid` | not a multiple of the lot step (Dhan lot size in units; MT5 volume step in lots), or no MT5 contract size |
| `currency` | the instrument settles in another currency than the account (conversion is not modelled) |
| `price_fresh` | no reference price younger than `PAPER_PRICE_MAX_AGE_SECONDS` |
| `kill_switch_off` | the policy's kill switch is on |
| `session_allowed` | the current session (Asia/London/NY) is not in the policy |
| `position_size` | the position **after** the fill would exceed `max_position_size` |
| `exposure` | gross notional of paper positions plus this order would exceed `max_exposure` |
| `costs` | estimated charges plus half-spread and slippage exceed `RISK_MAX_COST_BPS` of notional |
| `daily_loss` | today's realized P&L plus fees (from the ledger) is below `-max_daily_loss` |
| `drawdown` | the paper equity curve (capital + ledger) is more than `max_drawdown_pct` below its peak |

### Why risk cannot be bypassed

- The **Execution Agent** (`paper.execute`) re-checks everything itself. It accepts only an intent that is:
  - `APPROVED`, or resting `SUBMITTED`
  - PAPER, on a PAPER account
  - backed by an approving risk decision as its **latest** decision

  A status forged straight into the DB is refused with `risk_not_approved`.
- Approval is admin-only, CSRF-protected and audited:
  - `order_intent_approved`, `order_intent_rejected` or `order_intent_approve_denied`
  - creations log `order_intent_created`
  - cancels log `order_intent_cancelled`

### AI output cannot create or approve intents

- Intents are created only by an authenticated user through the API:
  - source `manual`
  - source `strategy` when they reference a signal, which still needs the same risk run and admin approval
- Only `app/services/paper.py` constructs `OrderIntent` rows. Only the paper API and the scheduler import that module.
- Nothing in the research or AI agents references order intents. A test enforces both points.

### LIVE is unreachable

- **Creating** an intent:
  - on an OBSERVE account returns 409 `account_not_paper`
  - on a LIVE account returns 409 `live_unreachable`
- **Approving or executing** returns 409 `live_unreachable` when either the intent or its account is LIVE, even if the
  account was flipped after the intent was created.
- There is no order-sending code path to reach.

## 3. Prices and fills

### Reference price

The first fresh source wins:

1. **Dhan:** the live-feed hash `dhan:tick:<SEG>:<ID>` (`ltp`), when not marked `stale` and younger than the limit.
2. **MT5:** the bot's last close (`agents[symbol].health.close`) from its status export, timed by `snapshot_at`.
3. **Either broker:** the newest closed, non-gap candle in `trading.market_candles` that closed within the limit.

With no fresh price, the intent is rejected at risk time (`price_fresh`). If prices go stale between approval and
fill, the approved intent rests as `SUBMITTED` until `paper-process` finds a price.

### Fill price

The fill moves against you by half the spread plus slippage:

`fill = ref × (1 ± (spread_bps/2 + slippage_bps)/10 000)`

The result is then rounded to the tick, again against the trader: BUY rounds up, SELL rounds down. A LIMIT fills only
when that price is at or better than its limit.

| Example | Result |
| --- | --- |
| Dhan defaults (2 bps spread, 1 bp slippage), BUY at ref 1000 | 1000.20 |
| SELL at ref 1010 | 1009.798, rounded to the 0.05 tick: **1009.75** |

## 4. Broker-aware accounting (all Decimal)

### Dhan

**Quantity** is units: shares, or contracts × lot size. It must be a multiple of the instrument's `lot_size`, for
example 75 for a NIFTY option. Notional is price × quantity.

**Kinds** come from `segment` and `instrument_type`:

| Segment | Instrument type | Kind |
| --- | --- | --- |
| `NSE_EQ` / `BSE_EQ` | `EQUITY` | equity: intraday, or delivery when `product` is `CNC` or `DELIVERY` |
| `*_FNO` | `FUT*` | futures |
| `*_FNO` | `OPT*` | options (turnover is the premium) |

Indices, commodities and currency are refused.

**Charges** per executed order, on its turnover:

| Charge | Rate |
| --- | --- |
| Brokerage | the lower of ₹20 and 0.03 %; free for equity delivery |
| STT | equity delivery 0.1 % both sides; intraday 0.025 % sell; futures 0.02 % sell; options 0.1 % sell |
| Exchange | equity 0.00297 %, futures 0.00173 %, options 0.03503 % |
| SEBI | 0.0001 % |
| Stamp (buyer only) | delivery 0.015 %, intraday 0.003 %, futures 0.002 %, options 0.003 % |
| GST | 18 % on brokerage + exchange + SEBI |

Each charge is rounded to the paisa. These are **approximations**: check them against Dhan's and the exchanges'
current schedules.

To change them, set `PAPER_DHAN_CHARGES`. Top-level keys **replace** the built-in ones, so give a whole nested object
(for example all of `stt_pct`) when you override one.

**Fixture**, an option SELL of ₹7 500 premium:

| Brokerage | STT | Exchange | SEBI | GST | Total |
| --- | --- | --- | --- | --- | --- |
| 2.25 | 7.50 | 2.63 | 0.01 | 0.88 | **₹13.27** |

### MT5

- **Quantity** is lots, a multiple of the symbol's volume step (for example 0.01).
- **Contract size** comes from `mt5.symbols` (`PUT /api/trading/mt5/instruments/{symbol}` with `contract_size`).
  Without it the intent is refused.
- **P&L** = Δprice × lots × contract size, in the profit currency. It must equal the account currency.
- **Pip value:** a pip is 10 points on 3/5-digit quotes, 1 point otherwise, so
  pip value = pip × contract size × lots. Examples: EURUSD (5 digits, 100 000) is 10 per lot. XAUUSD (2 digits, 100)
  at 0.1 lot is 0.100.
- **Costs:** spread and slippage are in the fill price. Commission is `PAPER_MT5_COMMISSION_PER_LOT` per lot per side.

### Positions and the ledger

Positions use the **average-cost** method:

- Adding to a position re-averages its cost.
- Reducing or closing it realizes (fill − average) × closed quantity × multiplier.
- Crossing zero flips the position, and the rest opens at the fill price.

Each fill writes:

- `trading.executions`: `is_simulated=true`, `broker_trade_id=paper-<intent id>`, fees, session, currency
- `trading.paper_ledger`: a `fees` row (negative), plus a `realized` row when P&L was realized
- `trading.positions_snapshots` (`source=simulated`): a full snapshot of all paper positions, computed by replaying
  every simulated execution. Instruments without a new price keep their last one.

## 5. Reconciliation

`paper-reconcile` (every 15 min) and `POST /api/trading/accounts/{id}/paper/reconcile` (admin) **replay every
simulated execution** into a fresh book. They compare it with:

- the latest simulated position snapshot: quantity and average price per instrument
- the money ledger: summed `realized` and `fees`

The result is stored in `trading.reconciliations` (`ok`, `drift[]`, `summary`). Any drift:

- is logged as an **error** on the scheduler agent (`GET /api/agents/scheduler/logs`)
- shows in the paper summary (`reconciliation.ok=false`)

Nothing is auto-corrected: drift means a row was changed outside the engine, which needs a human look.

## 6. API

| Method | Path | Who | Notes |
| --- | --- | --- | --- |
| POST | `/api/trading/order-intents` | owner or admin (CSRF) | 201 new (already risk-checked), 200 idempotent repeat, 409 conflict / not PAPER / LIVE, 422 bad shape |
| GET | `/api/trading/order-intents?account_id=&status=` | visible accounts | paged |
| GET | `/api/trading/order-intents/{id}` | visible accounts | intent + events + risk decisions + executions |
| POST | `/api/trading/order-intents/{id}/approve` | **admin** (CSRF, audited) | re-runs risk, then fills (or rests a LIMIT) |
| POST | `/api/trading/order-intents/{id}/cancel` | creator or admin (CSRF, audited) | while `PENDING_RISK` / `APPROVED` / `SUBMITTED` |
| GET | `/api/trading/executions?account_id=` | visible accounts | existing endpoint; paper fills have `source=simulated` |
| GET | `/api/trading/accounts/{id}/paper` | visible accounts | capital, realized and unrealized P&L, fees, equity, max drawdown, positions, last reconciliation |
| POST | `/api/trading/accounts/{id}/paper/reconcile` | admin (audited) | reconcile now |
| GET | `/api/trading/accounts/{id}/paper/reconciliations` | visible accounts | history |

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PAPER_STARTING_CAPITAL` | `1000000` | Notional starting equity per PAPER account (its base currency) |
| `PAPER_PRICE_MAX_AGE_SECONDS` | `180` | Oldest reference price a fill or risk check accepts |
| `PAPER_COSTS` | `{"dhan":{"spread_bps":"2","slippage_bps":"1"},"mt5":{"spread_bps":"1","slippage_bps":"1"}}` | JSON, one line |
| `PAPER_DHAN_CHARGES` | `{}` | JSON overrides of the Indian charge schedule (top-level keys replace) |
| `PAPER_MT5_COMMISSION_PER_LOT` | `0` | Commission per lot per side |
| `RISK_MAX_COST_BPS` | `25` | Shared with Phase D: maximum cost of one order, in bps of notional |

## Deploy and validate

```bash
cd /opt/private-ai/aureon && git pull
docker compose build
docker compose run --rm migrate                       # 0009: trading.paper_ledger + trading.reconciliations (new tables only)
docker compose up -d api scheduler                    # scheduler adds jobs paper-process and paper-reconcile
curl -sS -b $J $B/api/scheduler/jobs | python3 -m json.tool | grep -A2 paper
```

Switch one account to PAPER and make sure it has a risk policy. PAPER needs no live token.

```bash
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' \
  $B/api/trading/accounts/<acct>/mode -d '{"mode":"PAPER"}'
curl -sS -b $J $B/api/trading/accounts/<acct>/risk-policy
```

Create an intent, approve it as an admin, and look at the result:

```bash
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' $B/api/trading/order-intents \
  -d '{"account_id":"<acct>","instrument_id":42,"side":"BUY","quantity":"1","correlation_id":"smoke-0001"}' | python3 -m json.tool
curl -sS -b $J -H "x-csrf-token: $CSRF" -X POST $B/api/trading/order-intents/<intent>/approve | python3 -m json.tool
curl -sS -b $J "$B/api/trading/executions?account_id=<acct>&limit=5"
curl -sS -b $J $B/api/trading/accounts/<acct>/paper | python3 -m json.tool
curl -sS -b $J -H "x-csrf-token: $CSRF" -X POST $B/api/trading/accounts/<acct>/paper/reconcile
```

Expected result:

- The repeat with the same `correlation_id` returns 200 and the same id.
- A LIVE or OBSERVE account returns 409.
- The executions show `source: simulated`.

**Rollback:**

1. Pause the jobs:
   ```bash
   curl -sS -b $J -H "x-csrf-token: $CSRF" -X POST $B/api/scheduler/jobs/paper-process/pause
   ```
   Do the same for `paper-reconcile`.
2. Set the account back to OBSERVE, so no new intents are accepted.
3. `alembic downgrade 0008` drops only the two new tables.

Simulated executions, snapshots and events stay in the Phase A tables, always flagged `is_simulated` /
`source=simulated`.

## Tests

**`tests/test_paper_accounting.py`** (no DB):

- lot multiples; Dhan kind mapping
- adverse fill prices and tick rounding
- the Dhan option-sell fixture (₹13.27); equity delivery vs intraday charges
- MT5 commission and pip values
- average-cost flips; MT5 P&L with contract size; replay

**`tests/test_paper_db.py`:**

- **Round trip:**
  - BUY then SELL through the API
  - exact fill prices and charges (₹4.21 on the BUY), realized ₹95.50
  - ledger, flat snapshot, summary and executions
  - reconcile OK; audit events
- **Idempotent correlation id:** 200 on repeat with no duplicate events; 409 on a different payload.
- **Order shape:** lot size and shape errors.
- **Intent cannot bypass risk:**
  - no policy
  - kill switch, at creation and at approval
  - size limit
  - stale or old price
  - approving a rejected intent
  - the Execution Agent refusing a pending intent and a forged APPROVED status
- **Daily loss limit.**
- **LIVE unreachable:**
  - OBSERVE / LIVE accounts
  - the account flipped to LIVE after creation
  - a forged LIVE intent
  - the adapter interface has no order methods
- **AI cannot create or approve:**
  - only the paper service builds intents
  - only the paper API and scheduler import it
  - research/AI modules never mention intents
  - approval needs an admin with CSRF; other users cannot see intents
- **Resting LIMIT:** fills once marketable; DAY expiry; cancel rules.
- **MT5 contract-size requirement.**
- **Reconciliation drift:** a tampered snapshot and ledger are flagged, logged as an error and shown in the summary.

## Open questions / risks

- **Charges are approximations** of today's Indian schedule. The following are not modelled:
  - DP charges on delivery sells
  - option exercise STT
  - IPFT and clearing fees

  Check the rates before relying on paper P&L.
- **No currency conversion:** an instrument that settles in another currency than the account is refused.
- **Fill model:**
  - It is one fill at one price, with no partial fills, depth or queue position.
  - LIMIT fills are checked once a minute against the latest price, so a short touch between checks is missed.
  - STOP orders are not simulated.
- **Exposure** values other paper positions at their average cost, not their live price.
- **Frontend:** the "Send to paper" controls in ASTRA-FRONTEND can now be enabled against these endpoints. That is a
  separate frontend PR.
