# 30 — External-first AI: Gemini, then OpenRouter, then local

The local models (qwen2.5:3b) are not good enough to be the main model for chat and research
explanations. With this change the order is:

1. `AI_DEFAULT_EXTERNAL_PROVIDER` (e.g. `gemini`)
2. each provider in `AI_EXTERNAL_FALLBACK_PROVIDERS`, in order (e.g. `openrouter`)
3. local Ollama, only when every external provider refused or failed

Every provider is still asked through its own gate (docs/26): enabled + key + default model,
allowlist, price, the classification filter, its daily cap, the global cap and the per-minute
limit. Nothing about cost control or privacy is relaxed.

## What moves to the next provider, and what does not

| Situation | Result |
|---|---|
| The provider is disabled, has no key or no default model, or a 0 daily cap | next provider |
| The model is unpriced (`no_pricing`) or not allowed | next provider (with its default model) |
| Its daily cap, the global cap or the per-minute limit is reached | next provider |
| The call fails (HTTP error, rate limit, timeout, refusal) | next provider, in the same chat stream |
| The classification filter says the data must stay local (`filter`, `context`) | **local only**: no external provider is asked. The verdict does not depend on the provider |

`finance`, `trading` and `research` contexts stay local by design. A research explanation
goes external only with `AI_RESEARCH_EXTERNAL_ALLOWED=true`, and the filter still blocks
broker data, positions, finance records, secrets and logs.

The chat shows each switch as a `fallback` event (`provider`, `model`, `reason`,
`from_provider`, `reset: true`), and the stored answer keeps the first reason in
`fallback_reason`. The audit log has one `ai.external_call` row per provider tried. Each
provider's spend counts against its own cap.

## Other fixes

- **A new conversation no longer becomes local for good** when the default provider is
  enabled but not yet usable (no price, no budget). It keeps the provider, the turn moves down
  the chain with the reason shown, and once the provider is fixed later turns use it.
  Conversations that were already stored as local stay local: start a new chat.
- **The research worker** used only OpenRouter. It now builds every configured provider and
  walks the same chain, and it re-execs when any `GEMINI_`, `ANTHROPIC_`, `OPENROUTER_` or
  `AI_` key changes. Its external calls now pass their budget reservation back when they are
  recorded (OpenRouter reserves nothing, but a priced Gemini or Anthropic call does).
- **`ModelRouter`** (docs/model-routing.md) walks the same chain. Note: no agent calls it
  yet; the news and voice agents have no model call today.
- `/settings → AI → Test` lists the whole chain with each provider's state.

## Settings to set on the server (`/settings`, not `.env`)

| Key | Value |
|---|---|
| `GEMINI_ENABLED` | `true` |
| `GEMINI_API_KEY` | the key from Google AI Studio |
| `GEMINI_DEFAULT_MODEL` | e.g. `gemini-2.5-flash` |
| `GEMINI_MODEL_PRICES` | `{"gemini-2.5-flash": {"input": "<usd/Mtok>", "output": "<usd/Mtok>"}}` from Google's current price list; the key must match the model id exactly |
| `GEMINI_DAILY_LIMIT_USD` | above 0, e.g. `2` |
| `OPENROUTER_ENABLED` | `true` |
| `OPENROUTER_API_KEY` | the OpenRouter key |
| `OPENROUTER_DEFAULT_MODEL` | an OpenRouter id, e.g. `google/gemini-2.5-flash` |
| `OPENROUTER_DAILY_LIMIT_USD` | above 0 |
| `AI_DEFAULT_EXTERNAL_PROVIDER` | `gemini` |
| `AI_EXTERNAL_FALLBACK_PROVIDERS` | `openrouter` |
| `AI_RESEARCH_EXTERNAL_ALLOWED` | `true` for external research explanations |

Then run `/settings → Gemini → Test`, `OpenRouter → Test` and `AI → Test`. `GET
/api/v1/models/catalog` shows each provider's `usable` and `unavailable_reason`.

## Dhan keys (checklist)

The code reads all of these; nothing is missing on the code side. The values live on the
server (`.env` or `/settings`), so check them there; `GET /api/v1/dhan/auth/connection` and
`GET /api/v1/dhan/feed/health` show the result (docs/28).

| Purpose | Keys |
|---|---|
| Any Dhan access | `DHAN_CLIENT_ID` |
| Daily login through Dhan's consent page (recommended) | `DHAN_API_KEY`, `DHAN_API_SECRET`, `DHAN_REDIRECT_URL` (must match the redirect URL registered on web.dhan.co) |
| Or a manual token instead | `DHAN_ACCESS_TOKEN` (expires; used only while unexpired) |
| Live market-data feed | `DHAN_FEED_INSTRUMENTS`, and `dhan` in `COMPOSE_PROFILES` (not set on the server today) |
| Historical backfill | `DHAN_BACKFILL_INSTRUMENTS` (and `DHAN_BACKFILL_TIMEFRAMES`, `DHAN_BACKFILL_DAYS`) |
| Holidays (market-closed status) | `DHAN_MARKET_HOLIDAYS` |

Dhan stays read-only; none of these keys enables orders (docs/27, docs/29).

## Rollback

`AI_EXTERNAL_FALLBACK_PROVIDERS=` (empty) restores the previous behaviour exactly: one
external provider, then local. No migration.
