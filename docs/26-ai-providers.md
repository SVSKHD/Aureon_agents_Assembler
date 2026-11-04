# 26 — Direct AI providers: Anthropic and Gemini next to OpenRouter and Ollama

**Status:** implemented (migration `0023`). Off by default. Local Ollama stays the default
and the only fallback. This extends the OpenRouter design ([25](25-openrouter.md)); it is
not a second AI stack. The same gate, filter, usage counter, chat stream, model router and
settings registry now serve four providers.

| Provider | Credential | Role | Cost source |
| --- | --- | --- | --- |
| `ollama` | none (local service) | private default and the only fallback | none |
| `openrouter` | `OPENROUTER_API_KEY` | multi-model gateway (unchanged) | OpenRouter's reported `usage.cost` |
| `anthropic` | `ANTHROPIC_API_KEY` | direct Claude models | operator-configured price × tokens |
| `gemini` | `GEMINI_API_KEY` | direct Gemini models (Developer API) | operator-configured price × tokens |

**Two Claude integrations, kept separate.** Direct Anthropic inference
(`app/agents/anthropic/`) uses an API key from the Anthropic Console. The advisory PR review
([22](22-claude-review.md)) runs the local Claude Code CLI with its own login. Neither
credential grants the other: an API key does not log the CLI in, and a Claude subscription
does not authorize API calls. The review's flags, filter and approval rules are unchanged.

## Architecture

```
POST /api/chat ─┐                     app/services/inference.py
ModelRouter ────┼─► ExternalGate(provider).decide ──"no"──► local Ollama (reason recorded)
research worker ┘        │ (one gate, every provider)
                         "yes" ─► ExternalChatClient.chat_stream
                                   ├─ OpenRouterClient  (httpx, unchanged)
                                   ├─ AnthropicClient   (official `anthropic` SDK)
                                   └─ GeminiClient      (official `google-genai` SDK)
                                  error / refusal ─► local Ollama (never another provider)
```

- **One interface** (`app/agents/providers/base.py`): `chat_stream()` yields
  `{"content"}` pieces and then one `{"done", "usage": {prompt_tokens, completion_tokens,
  cost_usd}, "model"}`. `list_models()` and `aclose()` complete it. Every failure is a
  `ProviderError`, and its `code` is the fallback reason. `OpenRouterError` is now a
  `ProviderError`, and all OpenRouter class names are kept.
- **Error rules, the same for every adapter.** Messages are fixed strings built from the
  HTTP status, never the response body (a body can echo the prompt). SDK exceptions are
  re-raised `from None`, so no body is chained. Keys appear only in the SDK's request
  header: never in `repr`, an error or a log line.
- **Anthropic specifics.**
  - SDK 2.x sends `anthropic-version: 2026-10-21` and the key as `Authorization: Bearer`.
  - `temperature`, `top_p` and `top_k` are never sent: that API version rejects them with
    a 400.
  - `max_tokens` is the caller's value, else `AI_EXTERNAL_MAX_OUTPUT_TOKENS`.
  - `stop_reason: "refusal"` raises `refused`.
  - Server-side refusal `fallbacks` are deliberately **not** enabled. They would answer from
    a different model that may not be allowlisted or priced, which defeats the budget.
- **Gemini specifics.**
  - Uses the Developer API, with the key in the `x-goog-api-key` header (never in the URL).
  - The model id is validated before use, because it becomes part of the request path.
  - Thought parts are never streamed. Thinking tokens are counted as output, since they are
    billed as output.
  - A blocked prompt, or a SAFETY / PROHIBITED_CONTENT / BLOCKLIST / SPII / RECITATION
    finish, raises `refused`.

## Settings

All of these are runtime settings (`/settings`, stored Fernet-encrypted in
`core.app_settings`, [settings.md](settings.md)) with `.env` as the fallback. Keys are
`secret` specs: they need the admin's password to write or revert, are masked in every
response, are audited by key name only, and are registered with the log scrubber.

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_ENABLED` / `GEMINI_ENABLED` | `false` | Master switch. Off: no client is built, nothing is sent |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | empty | Server only. Never returned, logged or given to the SPA (no `VITE_` copy) |
| `ANTHROPIC_BASE_URL` / `GEMINI_BASE_URL` | `https://api.anthropic.com` / `https://generativelanguage.googleapis.com` | API base |
| `ANTHROPIC_DEFAULT_MODEL` / `GEMINI_DEFAULT_MODEL` | empty | Required for the provider to be usable; always allowed |
| `ANTHROPIC_ALLOWED_MODELS` / `GEMINI_ALLOWED_MODELS` | empty | Extra allowed ids (comma list) |
| `ANTHROPIC_DAILY_LIMIT_USD` / `GEMINI_DAILY_LIMIT_USD` | `0` | Per-provider cap per UTC day. `0` = no budget, everything stays local |
| `ANTHROPIC_MODEL_PRICES` / `GEMINI_MODEL_PRICES` | `{}` | `{"<model>": {"input": "<usd>", "output": "<usd>"}}`, USD per million tokens. **A model without a price is never used** (`no_pricing`). Nothing is built in: copy current prices from the provider's pricing page |
| `ANTHROPIC_TIMEOUT_SECONDS` / `GEMINI_TIMEOUT_SECONDS` | `60` | Per request |
| `AI_DEFAULT_EXTERNAL_PROVIDER` | `openrouter` | Provider for new conversations and `auto` agents, only when usable |
| `AI_EXTERNAL_DAILY_LIMIT_USD` | unset | Cap on today's **total** external spend across providers. Unset = per-provider caps only |
| `AI_EXTERNAL_MAX_OUTPUT_TOKENS` | `4096` | Output tokens when the caller sets none; also the worst case reserved before a priced call |
| `AI_EXTERNAL_MAX_RETRIES` | `1` | SDK retries before the first byte (connection errors, 408/409/429/5xx) |
| `AI_EXTERNAL_REQUESTS_PER_MINUTE` | `30` | Calls per provider per minute from this server (`0` = no local limit). Also applies to OpenRouter |
| `AGENT_{NEWS,VOICE,RESEARCH}_PROVIDER` | `auto` | Now also `anthropic` / `gemini` |
| `AGENT_{NEWS,VOICE,RESEARCH}_MODEL` | empty | The external model for that provider (empty = its default) |

Every OpenRouter setting keeps its meaning. With the defaults above
(`AI_DEFAULT_EXTERNAL_PROVIDER=openrouter`, no global cap), the existing OpenRouter setup
behaves as before, apart from the new 30/min local rate limit.

A provider is **usable** when it is enabled, has a key and a default model, its cap is
above 0, and the global cap (if set) is above 0. A model is **selectable** when it is
allowlisted and, for Anthropic and Gemini, priced.

## The gate, in order (first "no" wins)

| Reason | Meaning |
| --- | --- |
| `disabled` / `not_configured` / `no_budget` | settings (`Settings.external_unusable_reason`) |
| `model_not_allowed` | not the provider's default or on its allowlist |
| `no_pricing` | an Anthropic/Gemini model without a configured price |
| `context` | conversation linked to finance / trading / research (research: unless `AI_RESEARCH_EXTERNAL_ALLOWED`) |
| `filter` | the classifier hit on the **full** outbound payload (`filter_categories` lists which) |
| `daily_cap_reached` / `usage_unavailable` | the provider's counter in Redis; unreadable = refuse (fail closed) |
| `rate_limited_local` | over `AI_EXTERNAL_REQUESTS_PER_MINUTE` for this provider |
| `daily_cap_reached` / `global_cap_reached` | the call's worst case could not be reserved |

Errors during a call fall back to local with: `rate_limited`, `upstream_error`,
`unavailable`, `timeout`, `rejected` (bad or expired key, unknown model, no credits) and
`refused` (the provider's safety filter declined).

**No external-to-external failover.** If Anthropic fails, the turn goes to local Ollama, not
to Gemini or OpenRouter. Sending a payload to a second company is a separate decision; the
user has to make it by picking that provider.

## Budgets and concurrency

- **Per-provider counters** live in Redis at `ai:<provider>:usage:<UTC day>`. OpenRouter's key
  is unchanged, so today's OpenRouter spend carries over.
- **Reservations for the priced providers** (Anthropic, Gemini).
  - The worst case is reserved with an atomic `HINCRBY` on `ai:<provider>:reserved:<day>`
    before the call, then checked against the cap. Worst case = prompt chars / 3 tokens at the
    input price, plus `max_tokens` at the output price.
  - So N concurrent calls can never jointly pass the cap.
  - The real cost is recorded afterwards, and the reservation is released.
  - A reservation left by a crashed process expires 15 minutes after the last reservation.
    That only makes the cap stricter in the meantime.
  - OpenRouter reports its cost only at the end, so it keeps its documented rule: concurrent
    calls can overshoot by at most their own cost.
- **The global cap** compares the sum of all providers' spend plus open reservations
  against `AI_EXTERNAL_DAILY_LIMIT_USD`.

## Privacy

- Unchanged and shared by every provider:
  - The classifier (`app/services/chat_filter.py`) runs on the whole outbound payload: system
    prompt, re-sent history and new turns.
  - Secrets, finance records, broker data, positions and logs always stay local.
  - Trading text leaves only for `news` (public market news), or for research when
    `AI_RESEARCH_EXTERNAL_ALLOWED=true`.
- **Added:** Google API keys (`AIza…`) are recognized by the classifier, the log scrubber and
  the GitHub redactor. Anthropic keys (`sk-ant-…`) already were.
- **Unchanged paths:**
  - Voice speech-to-text and text-to-speech stay local.
  - Risk calculations never call a model.
  - The research worker's explanations keep their OpenRouter-only path, behind
    `AI_RESEARCH_EXTERNAL_ALLOWED`. They now also count against the global cap.
  - The coding agent and the Claude review are not touched.

## API contract for ASTRA-FRONTEND

Every change is additive: existing fields and their meanings are unchanged.

### `GET /api/models/catalog` (signed in)

```jsonc
{
  "default": {"provider": "gemini", "model": "gemini-…"},       // or ollama + local model
  "local_fallback_model": "qwen2.5:3b",
  "ollama_reachable": true,
  "models": [
    {"provider": "ollama", "id": "qwen2.5:3b", "label": "qwen2.5:3b",
     "available": true, "unavailable_reason": null,
     "capabilities": ["chat", "streaming"], "privacy": "local"},
    {"provider": "anthropic", "id": "claude-…", "label": "claude-…",
     "available": false, "unavailable_reason": "no_pricing", "privacy": "external", …}
  ],
  "openrouter": {"enabled": false, "unavailable_reason": "disabled",
                 "research_external_allowed": false},            // unchanged
  "providers": [                                                  // NEW, local first
    {"provider": "ollama", "label": "Local (Ollama)", "enabled": true, "configured": true,
     "usable": true, "unavailable_reason": null, "default_model": "qwen2.5:3b",
     "allowed_models": ["qwen2.5:3b"], "daily_limit_usd": null,
     "cost_source": "none", "privacy": "local"},
    {"provider": "anthropic", "label": "Anthropic", "enabled": true, "configured": true,
     "usable": true, …, "daily_limit_usd": "1.00", "cost_source": "configured_price",
     "privacy": "external"}
  ],
  "default_external_provider": "openrouter",
  "global_daily_limit_usd": null
}
```

- External models are listed only for **usable** providers.
- Show `available: false` models disabled, with their `unavailable_reason`.
- `configured` means a key is set. The key itself is never returned.

### `POST /api/chat` (signed in, CSRF)

- **Request.** `provider` now accepts `ollama | openrouter | anthropic | gemini`.
  - A `model` sent without a `provider` is matched against each provider's allowlist.
    OpenRouter ids contain `/`.
  - A model that is not allowed returns 404 `{detail, available}`, as before.
- **SSE events are unchanged.** `meta`, `done` and `error` carry:
  - `provider` and `model`: who actually answered.
  - `requested_provider` and `requested_model`: what was asked for.
  - `fallback_reason`: the gate or error reason above.
  - `local_only`: true when the data had to stay local.
  - `filter_categories`.
  - A `fallback` event `{provider: "ollama", model, reason, reset: true}` means: discard the
    partial text, the local model continues.
- **Per conversation.** The chosen provider and model are stored on the conversation
  (`ai.conversations.provider` now allows `anthropic` and `gemini`) and reused on the next
  turn when the request names none.

### `GET /api/ai/usage?provider=openrouter|anthropic|gemini` (signed in)

- Default `openrouter`: the same response as before.
- Added fields: `global_limit_usd` and `global_spent_usd` (all providers, open reservations
  included).

### `GET /api/v1/ai/usage?days=1` (admin)

- Unchanged: `items`, with per-agent rows by provider (calls, failures, fallbacks,
  local_only, tokens, cost, avg latency); OpenRouter's `daily_limit_usd` and
  `spent_today_usd`.
- Added: `providers[]` `{provider, daily_limit_usd, spent_today_usd, reserved_usd,
  calls_today, failures_today}`, `global_limit_usd` and `global_spent_today_usd`.

### `/api/v1/settings` (admin, CSRF; writing a secret needs `{"password"}`)

- New categories: `anthropic`, `gemini`, `ai`, with the keys above.
- Secrets come back as `{is_secret: true, masked: "••••abcd" | "••••", source}`.
- `POST /api/v1/settings/{key}/test` runs that category's connection test:
  - **Anthropic / Gemini.** Lists models with the key (no tokens spent), then checks the
    default model exists and is priced. Result: `{ok, detail}` as a status line.
  - **ai.** Checks the default provider is usable (no network call).

### `GET /api/v1/system/integrations` (admin)

New section, `ai_providers`:
- Per provider: `enabled`, `configured`, `usable`, `reason`, `default_model`,
  `daily_limit_usd`.
- Overall `status`: `ok`, `degraded` (an enabled provider is unusable or its default is
  unpriced) or `disabled`.
- Built from settings only; it never calls a provider.

## Manual setup (on the server, after merge)

1. Create keys: an Anthropic Console API key and/or a Google AI Studio API key. Use
   **API keys**, not a Claude.ai or Claude Code subscription login.
2. In Spasta `/settings` (re-enter your password for each secret):
   - **Anthropic:** `ANTHROPIC_API_KEY`, `ANTHROPIC_DEFAULT_MODEL` (pick from **Test**,
     which lists the models your key can use), `ANTHROPIC_MODEL_PRICES` with that model's
     current price per million tokens, and `ANTHROPIC_DAILY_LIMIT_USD`. Then **Test**, then
     `ANTHROPIC_ENABLED=true`.
   - **Gemini:** the same with the `GEMINI_` keys.
   - **AI:** optionally `AI_EXTERNAL_DAILY_LIMIT_USD` and `AI_DEFAULT_EXTERNAL_PROVIDER`.
3. No restart is needed: the API rebuilds the changed client on the settings event. Existing
   production keys in `.env` and `core.app_settings` are untouched by this change.

## Deploy and rollback

- **Deploy.** Merging to `main` deploys automatically ([deploy-automation.md](deploy-automation.md)):
  build (installs `anthropic` and `google-genai`), then `migrate` (0022 → 0023, a constraint
  swap), then `up -d`, then the readiness gate. A failed deploy rolls back the image and the
  SHA by itself. Do not merge until the PR is approved.
- **Manual roll back** (on the server, as `svskhd8`, from the repo directory):

```bash
# 1. Turn the providers off without a deploy (instant, no data change):
#    /settings -> Anthropic / Gemini -> ENABLED=false   (or delete the rows to revert to .env)
# 2. Code rollback to the previous image (kept by scripts/deploy.sh):
docker tag aureon-agentic:rollback aureon-agentic:dev && docker compose up -d
# 3. Schema rollback (only if required; 0023 is a constraint swap and the old code runs on it):
docker compose run --rm migrate alembic downgrade 0022
```

The 0023 downgrade re-adds the narrow check as `NOT VALID`. Conversations already stored with
`anthropic` or `gemini` are kept as they are; nothing is rewritten or deleted.

## Decisions

- **D-26-1:** use the official SDKs. The project rule and the brief ask for them. Anthropic
  2.x runs on `httpx2` (tests use `httpx2.MockTransport`); google-genai runs on `httpx` (tests
  use `MockTransport`/respx). OpenRouter keeps its tested httpx client.
- **D-26-2:** never build prices in. No price = `no_pricing` = local. A cap the system cannot
  compute is not a cap.
- **D-26-3:** reserve the worst case for priced providers. That stops concurrent calls from
  jointly passing the cap. OpenRouter cannot be estimated (no price table), so its
  documented overshoot rule stays.
- **D-26-4:** no external-to-external failover. Each provider is a separate data recipient
  the user opted into; local is the only automatic fallback.
- **D-26-5:** no Anthropic server-side refusal fallbacks (see the Anthropic specifics above).
- **D-26-6:** the research worker stays OpenRouter-only. Its explanations are trading text,
  external only with `AI_RESEARCH_EXTERNAL_ALLOWED`; widening that to new recipients needs a
  separate decision.
- **Found and fixed along the way:** `ModelRouter` re-parsed the OpenRouter client's
  already-parsed usage and lost `cost_usd`, so agent calls through OpenRouter were never
  charged against `OPENROUTER_DAILY_LIMIT_USD`. A regression test pins the fix.
