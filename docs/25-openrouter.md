> **Superseded (2026-11-12):** the AI stack is Claude-only. Ollama, OpenRouter, Gemini and the
> provider routing described here are removed. See [26-ai-providers.md](26-ai-providers.md).

# 25 — OpenRouter (opt-in external inference)

**Status:** done (migration `0020`). Brief rule 4 was amended on 2026-11-03 (see "Prompt 0"
in [decisions.md](decisions.md)). External inference through [OpenRouter](https://openrouter.ai)
is allowed for chat, but only when you opt in, and only behind a data-classification filter.
**Local Ollama stays the default and the fallback** for every request.

Never sent to OpenRouter: secrets and credentials, finance records, broker data, positions,
and logs or tracebacks. Research and AI trading explanations stay local unless
`AI_RESEARCH_EXTERNAL_ALLOWED=true`.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPENROUTER_ENABLED` | `false` | Master switch. Off: no client is built and nothing is ever sent |
| `OPENROUTER_API_KEY` | empty | Server only (`SecretStr`). Never logged or returned, and no `VITE_` copy exists |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | |
| `OPENROUTER_DEFAULT_MODEL` | empty | Used by new conversations when OpenRouter is usable; always allowed. Required |
| `OPENROUTER_ALLOWED_MODELS` | empty | Comma list of extra model ids offered in the picker |
| `OPENROUTER_DAILY_LIMIT_USD` | `0` | Budget per UTC day. `0` = no budget: every turn stays local |
| `OPENROUTER_TIMEOUT_SECONDS` | `60` | Connect timeout, and the longest silence allowed between streamed chunks |
| `AI_RESEARCH_EXTERNAL_ALLOWED` | `false` | Lets `research` conversations and the research worker send trading text |

OpenRouter is **usable** only when all of these hold: it is enabled, a key is set, a default
model is set, and the cap is above 0. Only then is it the default for new conversations.
Otherwise the default is local (`CHAT_DEFAULT_MODEL`, else the first installed model).

Suggested production values:

```
OPENROUTER_ENABLED=true
OPENROUTER_API_KEY=<from openrouter.ai/keys, in .env only>
OPENROUTER_DEFAULT_MODEL=google/gemini-2.5-flash-lite
OPENROUTER_ALLOWED_MODELS=qwen/qwen3.7-flash,openai/gpt-4.1-nano
OPENROUTER_DAILY_LIMIT_USD=0.50
OPENROUTER_TIMEOUT_SECONDS=45
AI_RESEARCH_EXTERNAL_ALLOWED=false
```

## Which provider answers a turn

```
POST /api/chat {provider?, model?, context?, conversation_id?, messages}
  target = body.provider/model ▸ else the conversation's ▸ else the default
  │
  ├─ ollama ─────────────────────────────────────────────► local, as before (docs/03)
  │
  └─ openrouter ─ ExternalGate.decide (app/services/inference.py), first "no" wins:
        disabled | not_configured | no_budget      settings
        model_not_allowed                          not the default or in the allowlist
        context                                    conversation linked to finance/trading/research
        filter                                     the classifier hit (categories listed)
        daily_cap_reached | usage_unavailable      Redis pre-check (fails closed)
     "no"  ─► local fallback model, reason on `meta`
     "yes" ─► OpenRouter stream (usage: {include: true})
                error: rate_limited | upstream_error | rejected | timeout | unavailable
                  ─► `fallback` event, same request continues on local Ollama
```

- A `model` sent **without** a `provider` means OpenRouter only if it contains `/` and is
  an allowed OpenRouter id. Otherwise it is a local model, exactly as before, so older
  clients keep working.
- If you explicitly ask for an OpenRouter model that is not allowed, the answer is `404`
  with `available`. Nothing is stored.
- The picked provider and model are stored on the conversation. The provider and model that
  actually answered are stored on each assistant message, with `fallback_reason` and
  `cost_usd`.
- OpenRouter turns join the Ollama queue (`OLLAMA_MAX_CONCURRENT`) only when they fall back.

## The classification filter (`app/services/chat_filter.py`)

The filter runs on the **full outbound payload**: the system prompt, the re-sent history and
the new turns. The SPA's live-data block travels in the system message, so it is checked
too. It reuses the Phase 19 secret and entropy detection (`review_filter.secret_hits`,
`entropy`) and adds rules for chat text:

| Category | Examples | Research flag |
| --- | --- | --- |
| `secrets` | everything the review filter finds, JWTs, `sk-…` keys, bearer tokens, unquoted high-entropy tokens, "password", "API key", "OTP", "seed phrase" | still local |
| `finance` | ₹ / $ amounts, "This month (INR)", salary, invoices, expenses, loans, bank/card/IBAN/IFSC/PAN/Aadhaar, Luhn-valid card numbers | still local |
| `trading` | stocks, NIFTY, XAUUSD, options, stop-loss, backtests, EMA/RSI/MACD, watchlists, "buy 2 lots" | **allowed** for `research` |
| `broker` | Dhan, MT5, Zerodha, Upstox, …, brokerage, demat, client id | still local |
| `positions` | open positions, holdings, P&L / PnL, unrealised gains, avg price, qty, exposure | still local |
| `logs` | Python tracebacks, JS/Java stack frames, `ValueError: …`, timestamped or levelled log lines, JSON logs, syslog, access logs | still local |

The conversation **context** (`general | finance | trading | research`, set on creation or
added later) also forces local inference. `finance` and `trading` are always local.
`research` is local unless `AI_RESEARCH_EXTERNAL_ALLOWED=true`. A context can be added to a
`general` conversation, but never removed.

The filter blocks broadly. A false positive only costs a slower local answer. If "Use your
data" is on in the SPA, the money block is in the system message, so those turns are local.
Reasons name the category, never the matched text.

## Stream events

The events are the same as in [03-ollama.md](03-ollama.md#stream-events), plus routing fields.
`meta`, `done` and `error` carry these:

| Field | Meaning |
| --- | --- |
| `provider`, `model` | who answers now (`meta`) / who answered (`done`, `error`) |
| `requested_provider`, `requested_model` | what the conversation asked for |
| `fallback_reason` | null, or why it is not OpenRouter (one of the codes above) |
| `local_only` | true when the data had to stay local (`filter` or `context`), which shows the SPA's "local only" badge |
| `filter_categories` | the filter's categories, e.g. `["finance"]` |
| `cost_usd` (`done`) | OpenRouter's reported cost as a decimal string, or null |

New event: `fallback` `{provider: "ollama", model, reason, local_only: false, reset: true}`.
OpenRouter failed during the call. Drop any text shown so far (`reset`); the local answer
follows in the same stream (`queued`/`start`/`token`/`done`).

## Endpoints

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/models/catalog` | the picker list: `models[{provider, id, label}]` (installed Ollama models, then the allowed OpenRouter ids when usable), `default {provider, model}`, `local_fallback_model`, `ollama_reachable`, `openrouter {enabled, unavailable_reason, research_external_allowed}` |
| GET | `/api/ai/usage` | today (UTC): `spent_usd`, `limit_usd`, `remaining_usd`, `prompt_tokens`, `completion_tokens`, `calls`, `failures`, `cap_reached`, `available` |
| POST | `/api/chat` | also accepts `provider`, `context`; `model` is optional |
| GET | `/api/conversations[/{id}]` | conversations gain `provider`, `context`; messages gain `provider`, `fallback_reason`, `cost_usd` |

Every endpoint needs a session. `POST` needs CSRF, as before.

## Spend cap (`app/services/ai_usage.py`)

There is one Redis hash per UTC day, `ai:openrouter:usage:YYYY-MM-DD`, kept for 8 days. It
holds `cost_micro` (USD × 1e6, from `usage.cost`), `prompt_tokens`, `completion_tokens`,
`calls` and `failures`.

- Before each call, the spend is checked against `OPENROUTER_DAILY_LIMIT_USD`. A reached
  cap, or a counter that cannot be read, keeps the turn local.
- Calls that start at the same time can overshoot the cap by at most their own cost.
- Reset: the day key rolls over at 00:00 UTC.
- To inspect: `redis-cli HGETALL ai:openrouter:usage:$(date -u +%F)`.

## Audit

Every OpenRouter attempt writes one `ai.external_call` row to `audit.audit_events`. It holds
the provider, model, conversation and message id (or the research subject), outcome (`ok`,
`error:<code>`, `cancelled`, `timeout`), prompt and completion tokens, cost, latency and
`fell_back_to`. It **never** holds the prompt or the answer.

## Keeping the key secret

- The key is a `SecretStr`. It goes on the request's `Authorization` header and nowhere else.
- `OpenRouterClient.__repr__` leaves it out.
- Error texts come from the HTTP status, never the upstream body.
- The log scrubber (`app/core/logging.py`) and the Discord redactor
  (`app/integrations/github/redact.py`) also mask any `sk-…` / `sk-or-v1-…` key and any
  bearer token.
- Tests check that the key never appears in log output, in any API response or SSE stream,
  in stored errors or in the audit rows: `tests/test_openrouter_unit.py` and
  `tests/test_chat_openrouter.py`.

## Research

`app/agents/research/ai.py` tries OpenRouter first only when `AI_RESEARCH_EXTERNAL_ALLOWED=true`
and the gate allows the payload with `context="research"`. Broker data, positions, finance
records, secrets and logs still block it. A refusal or an error falls back to
`RESEARCH_AI_MODEL` on Ollama. External explanations are stored with
`model = "openrouter:<id>"`.

## Deploy

1. Apply migration `0020`. It is additive: two columns on `ai.conversations` with defaults,
   three nullable columns on `ai.messages`. It needs 0018–0020 from the other PRs first.
2. Set the variables above in the server `.env` (only there), then restart the API (and the
   research worker if the research flag changes).
3. Check: `GET /api/models/catalog` shows `default.provider = "openrouter"`, and
   `GET /api/ai/usage` shows the cap.
