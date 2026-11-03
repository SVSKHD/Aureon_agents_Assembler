# Trading Phase C — MT5 bot adapter (read-only)

Aureon watches the existing **Aureon MT5 bot** ([SVSKHD/Aureon_agentic_bot](https://github.com/SVSKHD/Aureon_agentic_bot)).
It does not run, change or control the bot. The bot stays a separate process on its Windows machine, and its EMA
crossover/pullback logic, symbols, timeframes, backtests and Discord notifications are untouched.

```
 Windows box (bot, MetaTrader 5)                       Ubuntu server (Aureon)
 ┌───────────────────────────────┐                     ┌──────────────────────────────────────┐
 │ main.py ── SymbolAgents        │                     │ scheduler: mt5_sync_status (1/min)    │
 │   │  (unchanged)               │   logs/status.json  │   → agents.agents "mt5-bot" heartbeat │
 │   └─ status_export (opt-in) ───┼──── file  or ──────▶│   → agents.agent_logs (journal)       │
 │        GET /status 127.0.0.1   │   GET over Tailscale│   → trading.trading_signals           │
 │        (token, read-only)      │                     │   → trading.positions_snapshots       │
 └───────────────────────────────┘                     │   → mt5.terminals                     │
                                                       │ api: MT5Adapter, /api/trading/mt5/*   │
                                                       └──────────────────────────────────────┘
```

No migration for the pull mode: every table it uses exists since `0006` (Trading Phase A). The push mode (§3)
adds migration `0018` (five tables in `mt5`).

## 1. Bot side (separate PR)

[SVSKHD/Aureon_agentic_bot#37](https://github.com/SVSKHD/Aureon_agentic_bot/pull/37) adds
`aureon_mt5/status_export.py`, which is **off unless `AUREON_STATUS_EXPORT=1`**. Every 15 s it writes one JSON document,
`logs/status.json`, built only from what the bot already holds:

- each agent's cached snapshot (positions, MT5 link, tick age, today's counters), health and latest signal
- the read-only part of `/console/state` (session, regime, timeframe, network)
- allowlisted journal events: signal, position_seen, exit, order_placed, error and others
- `halt.json`, the `ema921e_trades.csv` tail and the `summary` of `out/*.json` backtests

It makes no MT5 calls, and the module never imports `broker` or `MetaTrader5`; a test checks the AST. An optional
GET-only `GET /status` endpoint can be turned on: it uses its own token (`AUREON_STATUS_TOKEN`), binds 127.0.0.1:8788,
refuses a non-loopback bind and answers 405 to any non-GET request. The contract is the bot's `docs/STATUS_EXPORT.md`
(schema `aureon.mt5.status/v1`).

**Not available** (the bot does not compute them without new MT5 calls, so they are not invented):

- account balance, equity and margin
- closed-deal history
- pending orders
- quotes and candles

## 2. Aureon side

| Piece | Where | What |
|---|---|---|
| `StatusSource` | `app/agents/trading/mt5/status.py` | Reads the file (`MT5_STATUS_FILE`) or the URL (`MT5_STATUS_URL` + `MT5_STATUS_TOKEN`). Parses numbers with `parse_float=Decimal` and validates schema v1. |
| `MT5Adapter` | `app/agents/trading/mt5/adapter.py` | `broker = "mt5"`. Implements `health`, `accounts` and `positions` (signed lots). Everything else raises `NotSupported`. |
| `mt5_sync.sync_status` | `app/services/mt5_sync.py` | One pass per minute: heartbeat, logs, signals, positions, terminal. |
| Scheduler job | `mt5-sync-status` → `mt5_sync_status` | Created when `MT5_STATUS_FILE` or `MT5_STATUS_URL` is set (`MT5_SYNC_CRON`, default every minute). |
| API | `GET /api/trading/mt5/status`, `PUT /api/trading/mt5/instruments/{symbol}` | Live view; admin contract-spec registration. |

### Mapping

| Export | Table | Rule |
|---|---|---|
| `generated_at`, `bot`, `agents[*]` | `agents.agents` id `mt5-bot`, kind `trading` | `last_heartbeat_at` = the export's `generated_at`, so a bot that stops exporting goes **stale**. `state` is `idle` when healthy, `error` when stale, dry, disconnected or an agent thread is dead, and `stopped` when halted. `paused` is never touched. |
| `events[*]` | `agents.agent_logs` | Each event is written once (sha256 key in `context.key`) with `created_at` = the event's `t`. `error` is logged as error, `watchdog_open` and `process_exit` as warning, the rest as info. |
| `events[event=signal]` | `trading.trading_signals` | Strategy `mt5:<mode>`, version 1 (`code_ref aureon_agentic_bot:<mode>`). Side LONG→BUY, SHORT→SELL. Timeframe = the agent's `exec_tf`. `candle_time` = `bar` − `offset_h`·3600 (bar times are MT5 server clock). Deduplicated on the Phase D key. **Only for symbols registered in `trading.instruments`**; other symbols are counted as `unknown_instrument`. |
| `agents[*].positions` | `trading.positions_snapshots` (`source=broker`) | Only when `MT5_ACCOUNT_ID` names an active registered `mt5` account and the export is healthy. `unrealized_pnl` is left empty because the bot exports points, not money. |
| bot info | `mt5.terminals` | Login = `MT5_ACCOUNT_ID`, server = `MT5_SERVER` (or `unknown`), `last_seen_at` = `generated_at`. |
| admin input | `trading.instruments` + `mt5.symbols` | `PUT /api/trading/mt5/instruments/XAUUSD` with the contract spec from MT5's *Specification* window. |

### Stale detection

`effective_state` now honours a per-agent window in `details.stale_after_s`. The sync job sets it to
`MT5_STALE_SECONDS` (default 90 s, i.e. six missed 15 s exports). The global `AGENT_STALE_SECONDS` still applies to every
other agent. `GET /api/trading/agents`, `GET /api/agents` and `GET /api/system/status` therefore show `mt5-bot` as
`stale` when:

- the bot stops exporting
- the Windows box goes down
- the file or URL becomes unreachable

### Performance

`GET /api/trading/mt5/status` → `performance` is labelled **`calculated`**. It covers, per symbol, exits, wins, losses,
net points and win rate from the journal `exit` events in the export window (48 h by default), plus the ema921e USD sum
from its trade CSV. Backtest summaries are passed through as the bot wrote them. No MT5 deal history is read.


## 3. Push mode (`aureon.mt5.push/v1`)

The bot (bot PR #38, off unless `AUREON_PUSH_ENABLED=1`) POSTs one document per status poll to Aureon. It is a superset
of `aureon.mt5.status/v1`, so no export file, mount or inbound port on the Windows box is needed.

```
 bot (Windows) ── HTTPS over Tailscale ──▶ tailscale serve ─▶ nginx :8088 (/api/) ─▶ api :8000
   POST /api/trading/mt5/ingest
   Authorization: Bearer <MT5_INGEST_TOKEN>   Content-Encoding: gzip (optional)
   X-Aureon-Push-Id: <uuid4 == body.push_id>
```

| Answer | When |
|---|---|
| `202 {"accepted":true,"push_id","events_new","events_duplicate"}` | stored |
| `200 {"duplicate":true,"push_id"}` | push_id already stored (the bot counts any 2xx as ack) |
| `401` | missing/bad bearer token (constant-time compare) |
| `413` | body > 2 MiB, or > 8 MiB after gzip |
| `415` | Content-Encoding other than gzip/identity |
| `422` | not JSON / not `aureon.mt5.push/v1` / header push id ≠ body / failed validation (`detail.fields` lists paths, never values) |
| `429` | more than `MT5_INGEST_RATE_PER_MINUTE` pushes per minute |
| `503` | `MT5_INGEST_TOKEN` unset, or the database is unavailable (retry) |

No session and no CSRF on this one path (machine-to-machine); the optional Cloudflare Access middleware exempts exactly
`/api/trading/mt5/ingest` (not a prefix). nginx needs no change: `location /api/` already caps bodies at `2m`.

### Storage (one transaction per push, `app/services/mt5_ingest.py`)

| Push part | Table | Rule |
|---|---|---|
| `push_id`, `seq` | `mt5.pushes` | PK push_id, ON CONFLICT DO NOTHING → duplicate |
| v1 part (`bot`, `agents`, `events`, ...) | `agents.agents`, `agents.agent_logs`, `trading.trading_signals`, `mt5.terminals`, `trading.positions_snapshots` | same `mt5_sync.apply_status` as the pull job; heartbeat = **receive time**, stale window `MT5_STALE_HEARTBEAT_SECONDS` |
| `events[]` + `tracebacks[]` | `mt5.events` | PK = the bot's event id, ON CONFLICT DO NOTHING; `kind` = timeline badge (P, cross, reentry, protected, secured, exit, closed, error, or the raw event name) |
| `health`, `guardian`, `strategy`, `signals_latest`, `halt`, `console`, `backtests`, `ema921e_trades` | `mt5.bot_snapshots` (one row) | replaced per push; an older `generated_at` never replaces a newer one |
| `candles[sym][tf][]` | `mt5.candles` | upsert on (symbol, timeframe, UTC open time); bar time − offset_h·3600; EMA keys as sent |
| classification | `mt5.failures` | see below |

### Failures ("why")

| Kind | Trigger | Resolves |
|---|---|---|
| `mt5_disconnected` | `health.mt5.connected` false, or an agent `connected:false` | first push without it |
| `stale_ticks` | market open and `tick_age_s` > `MT5_STALE_TICK_SECONDS` (120) | first push without it |
| `discord_error` | `health.discord.ok` false, or a traceback `where == "discord"` | first push without it |
| `guardian_error` | traceback `where` starts with `guardian`, or an error event mentioning guardian | quiet for `MT5_FAILURE_QUIET_SECONDS` |
| `exception` | any other traceback / error event (`error`, `agent_crash`) | quiet for `MT5_FAILURE_QUIET_SECONDS` |
| `stale_heartbeat` | no push for > `MT5_STALE_HEARTBEAT_SECONDS` (90): on read, and stored by job `mt5-ingest-watch` (every minute) | next push |

One open row per (kind, symbol): first_seen, last_seen, count, reason (message / last_error). Only events first seen in
a push are counted, so a re-sent traceback is not counted twice.

### Dashboard API (session, read-only)

`GET /api/trading/mt5/` `bot-status` (online / stale / error + last heartbeat), `market` (per symbol open/closed, current
session Asia/London/NY from UTC, last price, candles + EMA; `?timeframe=&limit=`), `positions` (with guardian phase, SL,
secured level, peak), `signals` (`?kind=` repeatable, `?symbol=`), `failures`, `strategy`, `report` (`?days=`: per-symbol
points P&L from `exit` events, daily net points, ema921e trades, backtests). `/api/trading/stream` emits `mt5` when a
push arrives or the open failures change. The frontend page is `/trading/mt5` (ASTRA-FRONTEND).

### Settings

`MT5_INGEST_TOKEN` (unset = off), `MT5_INGEST_MAX_BYTES` (2 MiB), `MT5_INGEST_MAX_DECOMPRESSED_BYTES` (8 MiB),
`MT5_INGEST_RATE_PER_MINUTE` (30), `MT5_STALE_HEARTBEAT_SECONDS` (90), `MT5_STALE_TICK_SECONDS` (120),
`MT5_FAILURE_QUIET_SECONDS` (3600), `MT5_CANDLE_RETENTION_DAYS` (7), `MT5_EVENT_RETENTION_DAYS` (30),
`MT5_INGEST_WATCH_CRON` (`* * * * *`), `MT5_INGEST_PURGE_CRON` (`37 3 * * *`). The two jobs exist only when the token is set.

### Deploy (push mode)

1. Merge, then `docker compose run --rm migrate` (0018) and restart `api` + `scheduler` (the deploy poller does this).
2. `.env`: `MT5_INGEST_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")`; the bot gets the same
   value plus `AUREON_PUSH_ENABLED=1` and the URL `https://svskhd8.taila439d5.ts.net/api/trading/mt5/ingest`.
3. Check: `curl -s -o /dev/null -w '%{http_code}\n' -X POST https://svskhd8.taila439d5.ts.net/api/trading/mt5/ingest`
   → `401` (route live, token required); `GET /api/trading/mt5/bot-status` → `online` once the bot pushes.
4. Rollback: unset `MT5_INGEST_TOKEN` (503, jobs no longer created; pause leftovers via `/api/scheduler/jobs/.../pause`),
   `alembic downgrade 0017` drops only the five new tables.

Tests: `tests/test_mt5_ingest_unit.py` (no DB: token, caps incl. gzip bomb, 415/422, duplicate push_id and event ids at
the route, classification, stale detection, exact-path exemption, prod check, jobs) and `tests/test_mt5_ingest_db.py`
(storage, failures lifecycle, watch/purge jobs, every read endpoint, the SSE `mt5` event).

## Deploy and validate (pull mode)

### Bot (Windows box, after #37 is merged)

Add these to the bot's `.env`, then restart the service (`nssm restart aureon-mt5` or your service name):

```
AUREON_STATUS_EXPORT=1
AUREON_STATUS_TOKEN=<python -c "import secrets; print(secrets.token_urlsafe(48))">
```

Check on the box:

```powershell
Get-Content logs\status.json -TotalCount 1 | Select-String aureon.mt5.status/v1
curl.exe -s -H "Authorization: Bearer $env:AUREON_STATUS_TOKEN" http://127.0.0.1:8788/status | Select-Object -First 1
tailscale serve --bg --https=8788 http://127.0.0.1:8788      # expose to the tailnet only
```

### Aureon (Ubuntu)

**Option A, HTTP over Tailscale (recommended: the bot is on another machine).** In `.env`:

```bash
MT5_STATUS_URL=https://<bot-box>.<tailnet>.ts.net:8788/status
MT5_STATUS_TOKEN=<same AUREON_STATUS_TOKEN>
MT5_ACCOUNT_ID=<MT5 login>
MT5_SERVER=<MT5 server name>
MT5_ACCOUNT_CURRENCY=USD
```

**Option B, file.** Use this only when the bot's `logs/` folder is visible on this host (for example a read-only SMB
mount). Then set `MT5_STATUS_FILE=/mnt/mt5-bot/status.json`, and add the mount to `api` and `scheduler` with a
`compose.override.yaml`:

```yaml
services:
  api:
    volumes: [{type: bind, source: /mnt/mt5-bot, target: /mnt/mt5-bot, read_only: true}]
  scheduler:
    volumes: [{type: bind, source: /mnt/mt5-bot, target: /mnt/mt5-bot, read_only: true}]
```

Then restart, register and check:

```bash
docker compose up -d api scheduler
# Reachability from inside the container (HTTP option):
docker compose exec api python -c "import httpx,os;r=httpx.get(os.environ['MT5_STATUS_URL'],headers={'Authorization':'Bearer '+os.environ['MT5_STATUS_TOKEN']},timeout=5);print(r.status_code, r.json()['schema'])"
# Register the account (admin session + CSRF, as in deploy/README.md):
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' \
  -d '{"broker":"mt5","account_id":"<MT5 login>","base_currency":"USD"}' $B/api/trading/accounts
# Contract spec per symbol, typed from MT5 → Symbol → Specification:
curl -sS -b $J -H "x-csrf-token: $CSRF" -H 'content-type: application/json' -X PUT \
  -d '{"lot_size":"0.01","tick_size":"0.01","currency":"USD","digits":2,"contract_size":"100"}' \
  $B/api/trading/mt5/instruments/XAUUSD
curl -sS -b $J $B/api/trading/mt5/status | python3 -m json.tool | head -40
curl -sS -b $J $B/api/trading/agents | python3 -m json.tool        # mt5-bot: idle, fresh heartbeat
curl -sS -b $J "$B/api/scheduler/jobs" | grep -o '"mt5-sync-status"[^}]*'
curl -sS -b $J "$B/api/trading/signals?limit=5" | python3 -m json.tool
```

Stale test: stop the bot's export (`AUREON_STATUS_EXPORT=0` and restart, or `tailscale serve --https=8788 off`). Within
`MT5_STALE_SECONDS` plus one minute, `mt5-bot` shows `stale` and `/api/trading/mt5/status` reports `ok: false`.

### Rollback

- Aureon: unset `MT5_STATUS_FILE` / `MT5_STATUS_URL` and restart. The adapter and job are no longer created. Pause the
  leftover job with `POST /api/scheduler/jobs/mt5-sync-status/pause`. Rows already written stay; nothing else is touched.
- Bot: `AUREON_STATUS_EXPORT=0` and restart. The bot then behaves exactly as before.

## Tests

- `tests/test_mt5_unit.py`, no DB, using the fixture export `tests/fixtures/mt5/status.json`. It covers:
  - schema validation and Decimal parsing
  - the server-clock → UTC conversion
  - signed positions
  - the shared BrokerAdapter contract (`tests/trading_contract.py`)
  - health reasons (stale, dry, disconnected, halted, missing file)
  - the HTTP source (token, 401, unreachable)
  - job wiring and calculated performance
- `tests/test_mt5_sync.py`, against the DB. It covers:
  - idempotent heartbeat, logs and signals
  - unknown instruments being skipped
  - positions and terminal rows for a registered account
  - a stale export (no positions; `stale` with its own window)
  - an invalid export writing nothing
  - the paused flag being kept
  - the scheduler task
  - the API, including CSRF and validation on the instrument endpoint

No MT5 terminal is needed anywhere.

## Open questions / risks

- **Transport:** the bot runs on Windows. Option A needs Tailscale (or another private link) from the Docker containers
  to the bot box. Option B needs the bot's `logs/` folder mounted on this host. Which one do you use?
- **Bar-time offset:** the bot converts bar times with the **current** `offset_h`. Signals stored across a DST change
  in the broker's server clock may be off by an hour for bars before the change. The original values are kept in
  `inputs.bar` and `inputs.offset_h`.
- **Strategy versions:** `mt5:<mode>` stays at version 1. The bot version is recorded per signal (`inputs.bot_version`).
  Bump the version by hand when the bot ships a MAJOR (rule-changing) release.
- **Auto-update:** the bot's auto-updater applies merges to `master` when flat. Merging #37 changes nothing until
  `AUREON_STATUS_EXPORT=1` is set.
- **No money values:** balance, equity and P/L in money are not available until the bot exports them. That would need
  new, read-only MT5 calls in the bot: a separate decision.
