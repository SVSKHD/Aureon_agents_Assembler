# 26 — Claude: the only AI provider

**Status:** implemented (migration `0030`). Claude (the Anthropic API) is Aureon's only AI
model. Gemini, OpenRouter and local Ollama are removed. Agents, tools and data stay on this
server; the only outbound AI traffic is HTTPS to `api.anthropic.com`, and only
`app/ai/gateway.py` makes it (`tests/test_ai_gateway.py` fails if any other module imports
the `anthropic` SDK or names that host). Egress allowlist: [egress.md](egress.md).

## Layout (`app/ai/`)

| Module | Role |
|---|---|
| `gateway.py` | the only SDK user: redaction, budget reservation, caching, streaming, retries, usage log |
| `redact.py` | strips names, emails, phones, addresses, account/client ids, PAN/Aadhaar/IFSC/cards, keys and tokens |
| `budget.py` | monthly + per-budget caps (Asia/Kolkata month), reserved before every call |
| `pricing.py` | cost of a call from its tokens (input, output, cache read, cache write) |
| `tools/` | the tool registry; investment, business and Spasta tools |
| `loop.py` | the agent loop (max `AI_MAX_STEPS`) |
| `approvals.py` | write requests: file, approve/reject, backend-only execution |
| `risk.py` | hard limits on proposed trades |
| `agents.py` | agents, their fixed system prompts, budgets, the router prompt |
| `chat.py` | conversations: replay, persistence, one turn |
| `reports.py` | the scheduled daily report and weekly review |
| `voice.py` | voice navigation's model fallback over the gateway |

## Settings (/settings → AI, or `.env`)

| Key | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | unset | Console API key. Unset = every AI feature is off. Never logged or returned. |
| `AI_MODEL_MAIN` | `claude-opus-5-5` | every agent's model |
| `AI_MODEL_ROUTER` | `claude-haiku-5-5` | picks the agent when a chat names none; voice fallback. Empty = off |
| `AI_EFFORT` | `medium` | thinking effort (Opus 5.5 always thinks; effort is the control) |
| `AI_MONTHLY_BUDGET_USD` | `100` | cap on all spend this month |
| `AI_AGENT_BUDGETS_USD` | investments 40, business 30, chat 20, reserve 10 | caps per budget |
| `AI_MODEL_PRICES` | `{}` | extra/override prices; a model without a price is never called |
| `AI_MAX_STEPS` | `8` | model calls per turn |
| `AI_TOOL_RESULT_MAX_TOKENS` | `2000` | each tool result is cut to about this |
| `AI_TRADE_MAX_LOSS_INR` / `_MAX_ORDER_INR` / `_MAX_PER_DAY` | 2000 / 100000 / 3 | propose_trade limits |
| `AI_DAILY_REPORT_CRON` / `AI_WEEKLY_REVIEW_CRON` | `30 8 * * *` / `0 10 * * 0` (IST) | reports; empty = off |

Built-in prices (USD per million tokens, input / output / cache read; cache write = 1.25 ×
input): Opus 5.5 4 / 20 / 0.20, Sonnet 5.5 2 / 10 / 0.20, Haiku 5.5 0.10 / 0.50 / 0.01
(0.50 / 2.50 above 100K prompt tokens).

## Every call (gateway)

1. The model must be priced, or nothing is sent.
2. Redaction of the system prompt, user text and tool results. Claude's own earlier turns are
   replayed byte-for-byte (they came from redacted input, and their thinking blocks must not
   change).
3. The worst-case cost (whole prompt at the cache-write rate + `max_tokens` output) is reserved
   in `ai.usage` under a Postgres advisory lock; if it could cross the monthly cap or the
   call's budget cap, the call is refused (`budget_exceeded`, HTTP 402 / SSE error).
4. Streaming request with `cache_control` on the last tool and on the system prompt (both fixed
   text) plus automatic caching of the conversation, `output_config.effort`, no sampling
   parameters. No server-side fallback to another model: the answer comes from a priced,
   allowed model or not at all.
5. SDK retries with backoff on connection errors, 408/409/429, 5xx (`AI_MAX_RETRIES`); the
   whole call is bounded by `AI_TIMEOUT_SECONDS`.
6. The reservation is settled: agent, model, input/output/cache tokens, USD cost, latency,
   request id, stop reason. Never content. A refusal is recorded and returned as `refused`.

## Agents and tools

Agents: `investments`, `business`, `general` (chat budget), `report` (reserve budget, read
tools only). Tools:

| Tool | Kind | Agents | Source |
|---|---|---|---|
| get_portfolio_summary, get_positions | read | investments, report | latest position snapshots (no broker call) |
| get_dhan_screener_results | read | investments, report | latest finished screener run |
| get_mt5_bot_status, get_mt5_recent_signals | read | investments, report | the Aureon bot's own tables (read-only) |
| propose_trade | write | investments | risk limits → approval → PAPER intent |
| get_aquakart_sales_summary, get_aquakart_open_queries, get_pawsattva_summary, draft_aquakart_blog | read/write | business | **not connected**: hidden from Claude |
| get_todos, add_todo | read/write | general | **not connected** (Spasta todos live in Supabase) |

Write tools never act: they file an `ai.approvals` row (`pending`, expires after
`AI_APPROVAL_TTL_HOURS`) and Claude is told "awaiting user approval". An admin approves with
CSRF and their password; the backend then runs the executor. `paper_trade` creates a PAPER
order intent through the paper engine (its own risk check and paper approval still apply);
LIVE trading stays disabled and Dhan stays read-only.

## API (`/api/ai`, signed-in sessions only)

| Route | Who | |
|---|---|---|
| `POST /chat` | any user, CSRF, `AI_CHAT_REQUESTS_PER_MINUTE` | SSE: `meta`, `text`, `tool`, `approval`, `done`, `error` |
| `GET /conversations`, `GET /conversations/{id}` | owner | readable messages only |
| `GET /approvals?status=pending` | admin | |
| `POST /approvals/{id}` `{action, password?}` | admin + CSRF; approve needs the password | executes on approve |
| `GET /usage` | admin | month-to-date total and per budget, remaining |

Admins can pause the `claude` agent from the agents page; chat then answers `ai_paused`.

## What changed for other features

- Research explanations (`/api/trading/*/explain`) use Claude (investments budget).
- Voice navigation's fallback uses `AI_MODEL_ROUTER`; the grammar still answers first.
- Coding agent and self-repair have no model (Ollama-only before): code tasks fail with that
  reason, repairs stay queued. Git ops are unaffected.
- `/api/health/ready` no longer checks Ollama; the integrations card has a `claude` section.
- The `refresh-model-inventory` job is paused on startup. Old tables (`ai.model_calls`,
  `ai.model_inventory`) are kept as history.
- Removed routes: `/api/chat`, `/api/models*`, `/api/conversations*`, `/api/v1/ai/usage`,
  `/api/v1/ai/route-check`. ASTRA-FRONTEND needs a follow-up to use `/api/ai/*`.
