# Model routing for the agents

Every agent that asks a language model goes through one function:
`ModelRouter.complete(agent, messages, context=…)` in `app/services/model_router.py`. That
covers news, voice answers and trading research explanations. Chat keeps its own streaming
path (docs/25-openrouter.md) and uses the same gate.

## Defaults

| Agent | Context | Default route |
|---|---|---|
| News (summary, sentiment, tags) | `news` | OpenRouter when `OPENROUTER_ENABLED` and the gate allows (public news), else local `qwen2.5:3b`; keyword rules if both fail |
| Voice intent and spoken answer text | `general` | OpenRouter for speed; anything about finance, positions, P&L, the broker or secrets is kept local by the filter and shown as "local only" |
| Voice speech-to-text / text-to-speech | n/a | local only (faster-whisper + Piper; no cloud audio) |
| Trading research explanations | `research` | local unless `AI_RESEARCH_EXTERNAL_ALLOWED=true` |
| Image generation | n/a | local SD 1.5 only; `OPENROUTER_IMAGE_MODEL` is off unless set, and never used for personal or finance prompts |

## How one call is routed

1. **Agent settings** (`/settings` → Agents, stored in `core.app_settings`):
   - `AGENT_<NAME>_ENABLED`: a disabled agent raises `AgentDisabled`.
   - `AGENT_<NAME>_PROVIDER`: `auto`, `ollama` or `openrouter`. `ollama` keeps the call local (reason `forced_local`). `openrouter` behaves like `auto`, still gated.
   - `AGENT_<NAME>_MODEL`: the OpenRouter model. Blank means `OPENROUTER_DEFAULT_MODEL`.
   - `AGENT_<NAME>_LOCAL_MODEL`: the local Ollama model.
2. **The gate** (`ExternalGate` in `app/services/inference.py`, the only one there is) checks, in order:
   - OpenRouter is enabled, has a key and has a budget.
   - The model is the default or on the allowlist.
   - The data classification filter (`app/services/chat_filter.py`) passes on the full payload.
   - Today's spend is under `OPENROUTER_DAILY_LIMIT_USD`. This cap is shared by chat and every agent.
3. **External call**: if it takes longer than `MODEL_ROUTER_EXTERNAL_TIMEOUT_SECONDS` (10 s), errors, or is rate limited, the same request goes to local Ollama. A `model_router_fallback` log line and a recorded row say why.
4. **Local call**: the agent's local model, through a separate bounded queue: one generation running, `MODEL_ROUTER_LOCAL_QUEUE` (2) waiting. A full queue raises `LocalBusy`, and the caller degrades (the news agent uses its keyword rules). Market data, the Dhan feed and risk checks never call a model, so a busy Ollama can't block them.

The `news` context lets the soft `trading` category (market words) leave the server, because
public market news is the whole point. Everything in the hard categories still forces local:
secrets, personal finance, broker accounts, positions and logs.

## Usage

- **What is recorded.** Every attempt becomes a row in `ai.model_calls` (migration `0022`): agent, provider, model, outcome (`ok`/`error`/`timeout`/`busy`), route reason, `fallback`, `local_only`, tokens, latency and cost. **No prompt or answer is stored.**
- **`GET /api/v1/ai/usage?days=1`** (admin) returns totals per agent and provider: calls, failures, fallbacks, local-only, tokens, cost and average latency. It also returns today's OpenRouter spend against the shared cap.
- **Checking the local models.** `/settings` → Agents → Test confirms the agents' local models are installed in Ollama (`/api/tags`). Nothing is pulled.
