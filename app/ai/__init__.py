"""Aureon's AI layer: Claude (Anthropic API) is the only model provider (docs/26-ai-providers.md).

- gateway.py   the only module that imports the `anthropic` SDK or calls api.anthropic.com
- redact.py    strips names, phones, emails, addresses, account ids and secrets before a call
- budget.py    monthly and per-agent USD caps, enforced before every call
- pricing.py   cost of a call from its token usage
- tools/       the tool registry: read tools run locally, write tools only file an approval
- loop.py      the agent loop (Claude asks for tools, the server runs them, max AI_MAX_STEPS)
- approvals.py approving / rejecting a write request; only the backend executes it
- risk.py      hard limits on proposed trades, in Python
- agents.py    the agents (investments, business, general, report): prompts and tool sets
- chat.py      conversations: history replay, persistence, the streamed turn
- reports.py   the scheduled daily report and weekly review
"""
