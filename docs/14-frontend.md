# Phase 6 + Trading Phase E — backend side of the Spasta frontend

The frontend ([SVSKHD/ASTRA-FRONTEND](https://github.com/SVSKHD/ASTRA-FRONTEND), branch `dev01`) moves off
Supabase / Netlify / Firebase in stages, and gains a `/trading` panel. This repo provides three things for it.

## 1. Same-origin routing (nginx)

`deploy/nginx/aureon-api.conf` routes `/api/*` on the site that serves the SPA to the API on `127.0.0.1:8000`.

- The session and CSRF cookies are `__Host-`, `Secure` and `SameSite=Strict`, so the SPA and the API **must** share one
  origin (spasta.online, or the LAN `:8088` site with `COOKIE_SECURE=false` for plain-http testing).
- Buffering is off and reads last 900 s, for the SSE streams (`/api/chat`, `/api/trading/stream`).

Include it inside the existing `server { }` block, before the SPA's catch-all `location /`, then:

```bash
sudo nginx -t && sudo systemctl reload nginx          # needs sudo: ask first
curl -sS https://spasta.online/api/health             # {"status":"ok"} from Aureon
curl -sS -o /dev/null -w '%{http_code}\n' https://spasta.online/   # SPA still 200
```

While Netlify still hosts the old build, its `/api/ai` and `/api/github` functions keep working there. On the
self-hosted site, `/api/*` belongs to Aureon. That is why the frontend switches each feature with its own flag.

## 2. Typed client source (OpenAPI)

```bash
python scripts/export_openapi.py --out ../astra-frontend/src/api/openapi.json
```

This builds the app with placeholder settings (nothing is connected) and writes the schema. The frontend generates
`src/api/schema.d.ts` from it with `openapi-typescript`. The `/api/chat` route now declares `status_code=200`,
because without it the schema could not be generated at all.

## 3. Real-time trading stream

`GET /api/trading/stream[?instrument_id=…]` is a Server-Sent Events stream for an authenticated user. It is
read-only and opens no DB connection between polls.

| event | when | payload |
|---|---|---|
| `hello` | on connect | server time, poll interval |
| `signal` | a new `trading_signals` row | id, version, instrument, timeframe, candle time, session, side |
| `risk` | a new `risk_decisions` row for one of your accounts | id, account, signal, approved, reason |
| `backtest` | a run changed status | id, status, error |
| `agent` | a trading agent's effective state changed | id, name, state, last heartbeat |
| `quote` | the Dhan feed hash changed for a requested instrument | ltp, ltt, volume, stale, `source: broker` |
| `bye` | max duration reached or the session ended | reason; the client reconnects (or re-logs in) |

- **Reconnect:** every event has an `id:` cursor. `EventSource` sends it back as `Last-Event-ID` on its own retries; a
  client reconnecting after a hard failure passes it as `?last_event_id=` instead, since EventSource cannot set headers. Signals and risk
  decisions resume exactly after the cursor, with no repeats and no gaps; they are emitted about 2 s after insert, so a
  late-committing row is not skipped. Backtest, agent and quote events describe current state and may repeat; apply
  them by id.
- **Lifetime:** the stream sends `retry: 3000` and a `: ping` heartbeat (`TRADING_STREAM_HEARTBEAT_SECONDS`). It ends
  after `TRADING_STREAM_MAX_SECONDS` (default 30 min) and the browser reconnects with its cursor.
- **Session checks:** the login session is re-checked every poll (`TRADING_STREAM_POLL_SECONDS`), so logout and
  revoke-all end the stream.

## Tests

`tests/test_trading_stream.py` covers:

- the OpenAPI export lists the endpoints the frontend uses
- a cursor round-trip
- the stream delivers only new rows, plus quotes and heartbeats, with SSE headers
- resuming with `Last-Event-ID` repeats nothing and picks up the row written in between
- 401 without a session
- a revoked session ends the stream with `bye: session_ended`
