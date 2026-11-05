# 29 — Trading safety: approvals, AI agents, and market-data vs trading permissions

Dhan access is **read-only by default**, and LIVE trading stays off. This page lists the
backend controls that keep it that way. It also lists what was added in this change and how
each control is tested. Nothing here enables live trading.

## Guarantees

| # | Guarantee | Enforced by | Tested by |
|---|---|---|---|
| 1 | No order can be placed, modified or cancelled through Dhan | `assert_read_only` in `DhanClient._request` (GET, or POST only to five market-data paths), checked before a request is built ([27](27-dhan-audit.md)) | `tests/test_dhan_read_only.py` |
| 2 | LIVE can't be selected | `TRADING_LIVE_ENABLED=false` (a bootstrap key: `.env` only, refused by the settings API). Enabling it needs `PHASE_G_APPROVED=yes` at load time. Even then, `check_mode_change` needs a **single-use** LIVE token bound to the admin and account (Redis `GETDEL`, 5-minute TTL), and still refuses with `409 preconditions_unmet` until Phase G exists | `tests/test_trading_core.py`, `tests/test_trading_api.py`, `test_live_flag_stays_off_by_default` |
| 3 | Approval is decided by the backend, by a person | `ApprovalGate.approve(request_id, admin: User, reauth_password, …)`: an admin session, re-authentication per policy, optional self-approval refusal, a hash-chained event log, and artifact drift checks before execution. A frontend "confirm" grants nothing | `tests/test_approval_engine_*.py` |
| 4 | **No approval can become a live action** (new) | `approvals.FORBIDDEN_KIND_PREFIXES`: no executor may be registered for `trading.live*`, `trading.order*`, `trading.mode*`, `trading.risk*`, `broker.*`, `dhan.*` or `mt5.order*`. `ApprovalGate.request` refuses unknown kinds, so such a request can't even be filed. Paper intents (`trading.paper_intent`, simulated) are unaffected | `test_no_executor_can_be_registered_for_live_actions`, `test_existing_executors_are_unaffected` |
| 5 | **AI agents (Qwen or any other model) can't authorize trades or change risk limits** (pinned) | Risk policy and execution mode are written in exactly one place: the admin trading API (`PUT …/risk-policy`, `POST …/mode`, `POST …/mode/live-token`: admin session + CSRF). No agent, AI service, worker, monitor or recovery module references those functions, constructs a `RiskPolicy`, writes a `BrokerAccount` row or sets `.mode`. No AI service, agent or worker calls `.approve(` | `test_only_the_admin_trading_api_can_reach_live_mode_or_risk_limits`, `test_ai_and_agent_code_cannot_approve_anything` (AST scans of `app/`), `tests/test_route_auth.py` (the admin routes are pinned) |
| 6 | **Market-data recovery is separate from trading** (new) | Automatic recovery for Dhan incidents can only run actions in `repair_recovery.MARKET_DATA_ACTIONS = {"reconnect_dhan_feed"}`. That action publishes one reconnect command, under a cooldown. The monitor and recovery modules import nothing from the trading or paper services. Trading/broker agents are never restarted (`NEVER_RESTART_*`). The manual control (`POST /api/v1/dhan/feed/reconnect`) needs an admin session, is audited, and shares the cooldown | `test_market_data_recovery_is_separate_from_trading`, `test_recovery_and_monitor_import_nothing_from_trading`, `test_reconnect_action_outcomes`, `test_reconnect_is_admin_only_rate_limited_and_audited` (db) |
| 7 | **Credentials are renewed only through Dhan's supported login** (new) | Aureon never calls RenewToken and never automates OTP, CAPTCHA or consent. An expired, rejected or mismatched credential raises an `auth` incident that has no recovery plan, so it escalates to manual attention at once, with the action to take | `test_unusable_credential_is_never_auto_recovered`, `test_expired_credential_escalates_to_a_person_at_once` (db) |
| 8 | No implicit approvals; no automatic strategy or risk changes | Nothing in the scheduler, monitor or recovery path creates, approves or edits an approval, a strategy version or a risk policy. Paper approvals still need an admin decision | #5's scans; `tests/test_paper_*` |
| 9 | Secrets stay out of responses, logs and incidents | Masked settings; the scrubber covers every registry secret; incident `details` pass through `scrub()`; `credential_fp` is an HMAC and never returned | `tests/test_dhan_auth_hardening.py::test_view_is_safe`, the db tests' audit checks |

## What a future LIVE phase (Phase G) must change, on purpose

Each step below is a deliberate, reviewed code change. None of them can be done from
settings, by an agent, or through an approval request:

1. Remove the relevant prefix from `FORBIDDEN_KIND_PREFIXES` and register a LIVE executor
   that re-checks the account, mode, risk policy and fresh credentials at execution time.
2. Update the AST tests above to allow exactly the new execution module.
3. Meet `LIVE_PRECONDITIONS_TODO` in `app/services/trading.py`: static IP preflight, a tested
   kill switch, fresh credentials, an execution module behind its own flag.
4. Set `PHASE_G_APPROVED=yes` and `TRADING_LIVE_ENABLED=true` in `.env` by hand. These are
   bootstrap keys that the settings API refuses.

## Failure handling

| Failure | Result |
|---|---|
| Redis down | LIVE tokens can't be issued or consumed, so LIVE is refused. The feed reconnect returns `503`. Recovery records `failed` and escalates after the attempts run out. Rate limiters fail closed |
| Approval expired or rejected | The existing engine: `approval-expire` marks it expired, and an expired, rejected or used request can't execute (`tests/test_approval_engine_*`) |
| Notification failure | `alerts-dispatch` releases the dedup claim and retries next run; the incident stays open (`test_alert_delivery_failure_is_retried_next_run`) |
| Unauthorized recovery | A non-admin gets `403` on `POST /api/v1/dhan/feed/reconnect`; no route lets a user trigger a trading action through recovery |
