"""The chat classification filter (docs/25-openrouter.md): what may go to OpenRouter."""

from __future__ import annotations

import pytest

from app.services.chat_filter import classify_chat, text_categories

# --- positives: each must stay local, in the named category ----------------------------------

POSITIVE: list[tuple[str, str]] = [
    # secrets / credentials
    ("secrets", "my password is hunter2, can you remember it?"),
    ("secrets", 'config: password = "s3cr3t-value"'),
    ("secrets", "here is my OpenRouter key sk-or-v1-0123456789abcdef0123456789abcdef"),
    ("secrets", "use sk-proj-AbCdEfGhIjKlMnOpQrStUvWx for the call"),
    ("secrets", "token ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("secrets", "AWS key AKIAIOSFODNN7EXAMPLE in the script"),
    ("secrets", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n"),
    ("secrets", "Authorization: Bearer abcdefghijklmnop1234567890"),
    ("secrets", "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig"),
    ("secrets", "DATABASE_URL=postgresql://user:pw@db:5432/x"),
    ("secrets", "SECRET_KEY=abc"),
    ("secrets", "the OTP is 482913"),
    ("secrets", "write down your seed phrase somewhere safe"),
    ("secrets", "what are my login details for the bank portal"),
    ("secrets", "random token Zx9qL2mN8vB4cR7tY1uI0oP3aS6dF5gH2jK"),
    ("secrets", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGx2YmZ0aW5nIHRoaXMgaXM user@box"),
    # finance records
    ("finance", "This month (INR):\n- income ₹85,000\n- spent ₹42,300"),
    ("finance", "I spent ₹1,200 on groceries"),
    ("finance", "my salary is credited on the 1st"),
    ("finance", "account number 0123456789 at SBI"),
    ("finance", "IFSC SBIN0001234"),
    ("finance", "PAN ABCDE1234F"),
    ("finance", "card 4111 1111 1111 1111 exp 12/29"),
    ("finance", "pay with 4111111111111111"),
    ("finance", "IBAN DE89 3704 0044 0532 0130 00"),
    ("finance", "summarise my October expenses"),
    ("finance", "draft an invoice for the client"),
    ("finance", "how much EMI on my home loan"),
    ("finance", "check my bank statement for duplicates"),
    ("finance", "it costs $49.99 a month"),
    ("finance", "show my UPI transaction history"),
    ("finance", "what is my net worth now"),
    # trading (soft)
    ("trading", "should I buy NIFTY calls tomorrow?"),
    ("trading", "set a stop-loss under the last swing low"),
    ("trading", "XAUUSD is ranging, thoughts?"),
    ("trading", "explain this backtest result"),
    ("trading", "my trading strategy uses EMA 9 and EMA 21"),
    ("trading", "RSI crossed 70 on the 15m chart"),
    ("trading", "buy 2 lots of banknifty"),
    ("trading", "is bitcoin bullish this week"),
    ("trading", "rebalance my investment portfolio"),
    ("trading", "the stock market crashed"),
    ("trading", "add RELIANCE to my watchlist"),
    # broker
    ("broker", "my Dhan account shows a margin shortfall"),
    ("broker", "the MT5 terminal disconnected"),
    ("broker", "compare Zerodha and Upstox brokerage"),
    ("broker", "my demat client id is 1100223344"),
    ("broker", "Interactive Brokers API limits"),
    # positions
    ("positions", "list my open positions"),
    ("positions", "what is my P&L today"),
    ("positions", "pnl dropped after the open"),
    ("positions", "unrealized gains on my holdings"),
    ("positions", "average price 2450, qty: 30"),
    ("positions", "position size for a 1% risk"),
    ("positions", "reduce net exposure before results"),
    # logs / tracebacks
    ("logs", 'Traceback (most recent call last):\n  File "app/main.py", line 3, in <module>'),
    ("logs", '  File "/srv/x.py", line 12, in run'),
    ("logs", "ValueError: invalid literal for int()"),
    ("logs", "2026-11-03T10:15:00Z INFO request served"),
    ("logs", "[2026-11-03 10:15:00] worker started"),
    ("logs", "ERROR: connection refused"),
    ("logs", '{"level": "error", "event": "boom"}'),
    ("logs", "    at Object.<anonymous> (/app/index.js:10:5)"),
    ("logs", "Exception in thread main java.lang.NullPointerException"),
    ("logs", "Nov  3 10:15:00 host sshd[123]: Failed password"),
    ("logs", '127.0.0.1 - - [03/Nov/2026:10:15:00 +0000] "GET / HTTP/1.1" 200'),
    ("logs", "here is the docker logs output"),
    ("logs", "panic: runtime error\ngoroutine 1 [running]:"),
]

# --- negatives: ordinary chat that may go out -------------------------------------------------

NEGATIVE: list[str] = [
    "hello",
    "Write a haiku about autumn leaves.",
    "Plan a 3-day trip to Goa with beaches and food.",
    "Explain how a hash map works.",
    "pip install requests fails behind a proxy, what should I check?",
    "How do database transactions and isolation levels work?",
    "What's the trade-off between latency and throughput?",
    "trade-offs of microservices",
    "Info about the museum opening hours",
    "Give me a short summary of this paragraph.",
    "In order to finish, I need three options for dinner.",
    "We should leverage the existing API.",
    "Holding the door for others is polite.",
    "Long story short, the meeting moved.",
    "Break 'Aquakart CRM' into steps.",
    "What is on my plate this week?",
    "Remind me to call mom at 6pm",
    "Make my photography portfolio site faster",
    "Adjust the exposure on this photo",
    "Translate 'good morning' to French.",
    "Convert 10 km to miles.",
    "Write a SQL query that counts users per day.",
    "what's a good name for a cat",
    "The year 2026 was busy.",
    "Use git rebase to clean the history.",
    "uuid 123e4567-e89b-12d3-a456-426614174000 is a sample",
    "commit 9fceb02d0ae598e95dc970b74767f19372d61af8",
    "Today is Tuesday, 3 November 2026.",
    "TODOS — open, sub-todos indented under their parent:\n- [todo:12] Fix the filter desc: none",
    "The stock of the shop ran out",
    "How do I reset my router?",
]

# The SPA's standing instructions (AI_INSTRUCTIONS, abridged but with every risky word):
# they ride along on every turn, so they alone must never force local.
SPA_INSTRUCTIONS = (
    "You are Aureon, the assistant inside the user's personal tracker. Be brief, concrete "
    'and warm. "type" is one of todo, task, idea, goal, trip, reminder or stock. When the '
    "user shares a link, use it for the details — names, dates, times, places, prices, "
    'steps, requirements. "notes" must carry every detail: names, numbers, amounts, dates, '
    "links. Renew the car insurance before the current policy lapses on 2 Nov. Get the "
    "renewal quote from the current insurer, then pay online and save the new policy PDF. "
    "Fix the installation status filter; check the query param mapping in the list API."
)


@pytest.mark.parametrize(("category", "text"), POSITIVE)
def test_sensitive_text_stays_local(category: str, text: str) -> None:
    assert category in text_categories(text), text
    verdict = classify_chat([{"role": "user", "content": text}])
    assert not verdict.external_ok
    assert verdict.reason == "filter"


@pytest.mark.parametrize("text", NEGATIVE)
def test_ordinary_text_may_go_out(text: str) -> None:
    assert text_categories(text) == set(), text
    assert classify_chat([{"role": "user", "content": text}]).external_ok


def test_spa_instructions_alone_are_not_sensitive() -> None:
    assert text_categories(SPA_INSTRUCTIONS) == set()


def test_every_role_of_the_payload_is_checked() -> None:
    clean = {"role": "user", "content": "what next?"}
    for role in ("system", "assistant", "user"):
        bad = {"role": role, "content": "my open positions are long 3 lots"}
        verdict = classify_chat([clean, bad, clean])
        assert not verdict.external_ok, role
        assert "positions" in verdict.categories


def test_system_context_block_with_money_forces_local() -> None:
    system = SPA_INSTRUCTIONS + "\n\n[AUREON CONTEXT]\nThis month (INR):\n- spent ₹12,000"
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": "hi"}]
    verdict = classify_chat(msgs)
    assert verdict.categories == ["finance"]


def test_reasons_never_quote_the_text() -> None:
    secret = "sk-or-v1-" + "a1B2" * 10
    verdict = classify_chat([{"role": "user", "content": f"key {secret}"}])
    assert all(secret not in r for r in verdict.reasons)
    assert verdict.reasons == ["content:secrets"]


@pytest.mark.parametrize("context", ["finance", "trading", "research"])
def test_linked_contexts_force_local(context: str) -> None:
    verdict = classify_chat([{"role": "user", "content": "hello"}], context=context)
    assert not verdict.external_ok
    assert verdict.reason == "context"
    assert verdict.reasons == [f"context:{context}"]


def test_unknown_context_is_local() -> None:
    assert not classify_chat([{"role": "user", "content": "hi"}], context="bogus").external_ok


def test_research_flag_relaxes_only_research_and_only_trading() -> None:
    msgs = [{"role": "user", "content": "explain this backtest: EMA 9/21 crossover on NIFTY"}]
    assert not classify_chat(msgs, context="research").external_ok
    ok = classify_chat(msgs, context="research", research_external_allowed=True)
    assert ok.external_ok
    # trading conversations stay local even with the flag
    assert not classify_chat(msgs, context="trading", research_external_allowed=True).external_ok
    # general conversations still treat trading text as local
    assert not classify_chat(msgs, research_external_allowed=True).external_ok
    # hard categories still block research
    for text in ("my open positions", "Dhan client id 12345", "spent ₹500", "password=x"):
        verdict = classify_chat(
            [{"role": "user", "content": text}], context="research", research_external_allowed=True
        )
        assert not verdict.external_ok, text
