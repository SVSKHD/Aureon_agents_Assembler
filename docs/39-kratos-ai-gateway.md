# 39 — KRATOS research AI route

One machine-to-machine route lets the KRATOS research lab ask Claude through Aureon's gateway. KRATOS has no provider
key and never names a model.

    POST /api/v1/kratos/ai/complete        Authorization: Bearer <KRATOS_AI_TOKEN>

Code: `app/api/v1/kratos_ai.py`. Tests: `tests/test_kratos_ai.py` (route, in-memory store) and
`tests/test_kratos_ai_db.py` (the real `ai.usage` table, metered isolation).

## Request and response

Request body (any other field, including `model`, is a 422):

| Field | Meaning |
|---|---|
| `role` | Short lower-case label of the KRATOS specialist; logged as agent `kratos:<role>` |
| `system`, `user` | The prompt. One user message, no tools |
| `max_tokens` | Capped by `KRATOS_AI_MAX_OUTPUT_TOKENS` |
| `idempotency_key` | Accepted for tracing; calls are not de-duplicated here |

Response: `text`, `usage`, `cost_usd`, `latency_ms`, `model` (what the API served), `model_selected`, `request_id`,
`stop_reason`, `budget`.

    GET /api/v1/kratos/ai/config[?probe=true]        same bearer token

Reports what a call would run with: the selected model, whether it is priced, whether the key can use it (with
`probe=true`; no tokens spent), the budget policy and caps, month-to-date spend, and the limits.

## Model

Every call runs on `KRATOS_AI_MODEL`, a runtime setting only the owner changes. It has no default. Blank refuses every
call. An unpriced model is refused before any call. Nothing falls back to `AI_MODEL_MAIN` or to any other model.

## Controls

- **Auth:** single-purpose bearer token, constant-time comparison. No session, cookie or CSRF. The route is off (503)
  while the token is unset, shorter than 32 characters, a placeholder, or equal to another credential.
- **Budget:** every call is recorded against the `kratos` budget. `KRATOS_AI_BUDGET_POLICY` decides the ceiling:
  - `capped` (default): the `kratos` cap in `AI_AGENT_BUDGETS_USD` (default 0 = refused) and the monthly cap apply.
  - `metered`: usage is recorded and reported the same way, but no dollar ceiling applies to KRATOS, and KRATOS spend
    is left out of the shared monthly total, so it cannot use up the cap the other agents rely on. Every other budget
    is capped exactly as before. The rate, concurrency, size, timeout and retry limits still apply.
    The Claude status card and `GET /api/ai/usage` compare the same shared total with the cap, so metered spend never
    makes them look exhausted; the `kratos` line still shows what KRATOS spent.
- **Gateway:** `app/ai/gateway.py` is unchanged. Pricing, reservation, retries, timeout and the usage log all apply.
- **Redaction:** KRATOS text is sent exactly as written or not at all. The gateway's personal-data rules are not
  applied on this route, because they rewrite numeric evidence: the 13 digits after the point in `3989.5950567873733`
  pass the card-number check and would become `[ACCOUNT_ID]`. KRATOS sends market evidence, not personal data. The
  credential rules still guard the route: a request holding a registered secret or credential-shaped text (API key,
  bearer token, JWT, URL password) is refused with 422 `credential_like_content` before anything is reserved or sent.
  Every other caller of the gateway is redacted as before.
- **Limits:** `KRATOS_AI_MAX_REQUEST_CHARS` (413), `KRATOS_AI_RATE_PER_MINUTE` (429 with Retry-After),
  `KRATOS_AI_MAX_CONCURRENCY` (429). If the rate limiter is unavailable the route fails closed (503).
- **Scope:** text in, text out. No tools, no access to accounts, orders, approvals or settings.
- **Logging:** role, tier and outcome. Never the prompt or the answer.

## Changing the settings at runtime

`KRATOS_AI_TOKEN`, `KRATOS_AI_MODEL` and `KRATOS_AI_BUDGET_POLICY` are managed settings. The route reads the token and
the model per request. The budget policy is read by each Claude gateway from the settings it was built with, so a
change to it rebuilds the API's gateway at once and re-execs the scheduler and the research worker (their
`RELOAD_PREFIXES`). A process still running an older build does not know the policy at all: it counts every `ai.usage`
row, KRATOS included, against the monthly cap. Bring every process that calls Claude onto this build before setting
`metered` (the research worker is in the `research` compose profile and is not recreated by a plain `up -d`).

## Schema (migration 0035)

`ai.usage` needed two changes before a KRATOS call could be recorded: `ck_usage_budget` now also accepts `kratos`, and
`agent` is `varchar(64)` (was 16) because the label is `kratos:<role>`; a role is at most 48 characters. No row is
rewritten and no other table is touched.

**Rollback.** A build without this revision cannot start against the migrated schema: its `migrate` one-shot stops on
the unknown revision, as with every earlier schema release. If the release has to be rolled back after `migrate` ran,
downgrade first with the new migration files, then start the previous build:

    git -C <checkout> archive <new-sha> alembic | tar -x -C /tmp/aureon-0035
    docker compose run --rm -v /tmp/aureon-0035/alembic:/app/alembic:ro migrate alembic downgrade 0034
    docker compose up -d

The downgrade never deletes or shortens a usage record: with `kratos` rows present the four-budget check comes back
as `NOT VALID` (history stays, new `kratos` rows are refused) and `agent` stays wide; with none the table is exactly as
at 0034.

## Settings

| Setting | Default |
|---|---|
| `KRATOS_AI_TOKEN` (secret, runtime-settable) | unset = route off |
| `KRATOS_AI_MODEL` (runtime-settable) | blank = refused |
| `KRATOS_AI_BUDGET_POLICY` (runtime-settable) | `capped` |
| `AI_AGENT_BUDGETS_USD.kratos` | 0 |
| `KRATOS_AI_MAX_REQUEST_CHARS` | 200000 |
| `KRATOS_AI_MAX_OUTPUT_TOKENS` | 8000 |
| `KRATOS_AI_RATE_PER_MINUTE` | 20 |
| `KRATOS_AI_MAX_CONCURRENCY` | 1 |

## To enable

1. Merge and deploy this branch (image rebuild, migration 0035, API and worker restart).
2. Generate a token of 32+ characters and save it as `KRATOS_AI_TOKEN` in Settings.
3. Set `KRATOS_AI_MODEL` to the model the owner has chosen.
4. Either set a `kratos` cap in `AI_AGENT_BUDGETS_USD`, or set `KRATOS_AI_BUDGET_POLICY` to `metered`.
5. Give KRATOS the same token (`KRATOS_AI_SERVICE_TOKEN` in its `deploy/.env`) and enable its `[casework]` gateway.
