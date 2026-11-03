"""Data classification for external chat inference (docs/25-openrouter.md).

Runs on the FULL outbound payload of every request that could go to OpenRouter: the system
prompt, the re-sent history and the new turns (the SPA's live-data context block arrives
inside the system message, so it is covered too). It reuses the Claude review filter's
secret and entropy detection (`app/services/review_filter.py`) and adds rules for chat text.

Categories, each of which forces the request onto local Ollama:

- ``secrets``  credentials of any kind: everything `review_filter.secret_hits` finds, plus
  JWTs, bearer tokens, OpenRouter/OpenAI-style keys, unquoted high-entropy tokens and the
  words people use when they paste one (password, API key, OTP, seed phrase, ...).
- ``finance``  personal finance records: bank/card/IBAN/IFSC/PAN/Aadhaar identifiers, rupee
  or dollar amounts, salary, invoices, expenses, loans, the SPA's "This month (INR)" block.
- ``trading``  markets and strategies: stocks, forex/crypto pairs, options, stop-loss,
  backtests, indicators, watchlists. The only *soft* category: a research conversation may
  send it when ``AI_RESEARCH_EXTERNAL_ALLOWED=true``.
- ``broker``   broker names and accounts (Dhan, MT5, Zerodha, ...), demat, client ids.
- ``positions`` open positions, holdings, P&L, quantities, average prices, exposure.
- ``logs``     tracebacks, stack traces and log lines (timestamps + levels, JSON logs).

Plus the conversation's ``context``: ``finance`` and ``trading`` conversations are always
local; ``research`` ones are local unless ``AI_RESEARCH_EXTERNAL_ALLOWED=true``.

The filter is deliberately broad: a false positive costs a slower local answer, a false
negative leaks data. Reasons name the category only, never the matched text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from app.services.review_filter import entropy, secret_hits

Category = Literal["secrets", "finance", "trading", "broker", "positions", "logs"]
CONTEXTS = ("general", "finance", "trading", "research")
Context = Literal["general", "finance", "trading", "research"]
HARD: frozenset[str] = frozenset({"secrets", "finance", "broker", "positions", "logs"})
SOFT: frozenset[str] = frozenset({"trading"})
LOCAL_CONTEXTS: frozenset[str] = frozenset({"finance", "trading", "research"})


def _rx(*parts: str, ignore_case: bool = True) -> re.Pattern[str]:
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    return re.compile("|".join(f"(?:{p})" for p in parts), flags)


# --- secrets (beyond review_filter.secret_hits) ----------------------------------------------
_SECRET_TEXT = _rx(
    r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.",  # JWT
    r"\bsk-(?:or-v1-|proj-|ant-)?[A-Za-z0-9_-]{16,}",  # OpenRouter / OpenAI / Anthropic keys
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{16,}",
    r"\b(?:ssh-(?:rsa|ed25519|dss)|ecdsa-sha2-nistp\d+)\s+AAAA[A-Za-z0-9+/]{20,}",
    r"\b(?:pass(?:word|wd|phrase|code)s?|pin\s*code|api[\s_-]?keys?|secret[\s_-]?keys?"
    r"|client[\s_-]?secrets?|(?:access|refresh|auth|session|bot)[\s_-]?tokens?"
    r"|private[\s_-]?keys?|ssh[\s_-]?keys?|seed[\s_-]?phrases?|recovery[\s_-]?phrases?"
    r"|mnemonics?|otp|totp|2fa\s+codes?|credentials?|login\s+details)\b",
    r"\b[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY)[A-Z0-9_]*\s*[:=]",
)
# Unquoted random-looking tokens (review_filter only checks quoted literals).
_BARE_TOKEN = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])")
BARE_TOKEN_ENTROPY = 4.3

# --- finance ------------------------------------------------------------------------------------
_FINANCE = _rx(
    r"\bbank\s+(?:account|statement|balance|details|transfer)s?\b",
    r"\b(?:account|a/c|acct)\s*(?:number|no\.?|#)",
    r"\b(?:iban|ifsc|swift\s+code|bic\s+code|routing\s+number|sort\s+code|upi\s+id|micr)\b",
    r"\b(?:credit|debit)\s+cards?\b",
    r"\bcard\s+(?:number|no\.?|details)\b",
    r"\bcvv\b",
    r"\b(?:salary|salaries|payslips?|pay\s+slips?|payroll|ctc)\b",
    r"\bnet\s+worth\b",
    r"\b(?:income\s+tax|tax\s+returns?|itr|form\s+16|gstin?)\b",
    r"\b(?:invoices?|ledgers?|balance\s+sheets?|expense\s+reports?|bank\s+statements?)\b",
    r"\b(?:expenses?|expenditures?|spending|spent\s+(?:₹|rs\.?|inr|\$|usd|\d))",
    r"\b(?:loans?|emis?|mortgages?|credit\s+score|cibil|overdraft)\b",
    r"\b(?:bank|card|upi|credit|debit|payment|financial|money|wire)\s+transactions?\b",
    r"\btransaction\s+(?:history|ids?|refs?|reference|amounts?|details)\b",
    r"\b(?:account|bank|wallet|available)\s+balance\b",
    r"\b(?:income|budget|remaining)\s+(?:₹|rs\.?|inr|\$|usd)?\s*\d",
    r"(?:₹|\brs\.?\s?|\binr\s?|\busd\s?|\$)\s?\d[\d,]*(?:\.\d+)?",
    r"\bthis\s+month\s+\(inr\)",
    r"\b\d{4}[ -]\d{4}[ -]\d{4}\b",  # Aadhaar / card groups
)
# Identifiers are upper case by definition; matching them case-insensitively hits words.
_FINANCE_IDS = _rx(
    r"\b[A-Z]{4}0[A-Z0-9]{6}\b",  # IFSC
    r"\b[A-Z]{5}\d{4}[A-Z]\b",  # PAN
    r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}\b",  # IBAN
    ignore_case=False,
)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

# --- trading (soft) -----------------------------------------------------------------------------
_TRADING = _rx(
    r"\btrad(?:ing|er|ers|ed)\b",
    r"\btrades?\b(?!\s*-?\s*offs?\b)(?!-offs?\b)",
    r"\bstock\s+(?:market|price|exchange|split|picks?)s?\b",
    r"\bstocks\b",
    r"\bshare\s+(?:price|market)s?\b",
    r"\bshares\s+of\b",
    r"\b(?:equit(?:y|ies)|nifty|bank\s?nifty|sensex|nasdaq|nyse|dow\s+jones|s&p\s*500)\b",
    r"\b(?:forex|fx\s+pairs?|crypto(?:currency|currencies)?|bitcoin|btc|ethereum|altcoins?)\b",
    r"\b(?:XAU|XAG|EUR|GBP|USD|JPY|AUD|NZD|CAD|CHF|BTC|ETH)/?"
    r"(?:USD|USDT|JPY|EUR|GBP|INR|CHF|CAD|AUD|NZD)\b",
    r"\bstop[\s-]?loss(?:es)?\b",
    r"\btake[\s-]profits?\b",
    r"\b(?:call|put)\s+options?\b",
    r"\boptions?\s+chains?\b",
    r"\bstrike\s+prices?\b",
    r"\b(?:futures|f&o|derivatives)\b",
    r"\bback[\s-]?test(?:s|ing|ed)?\b",
    r"\b(?:buy|sell|trading|entry|exit)\s+signals?\b",
    r"\b(?:intraday|scalping|scalp\s+trades?|swing\s+trad\w*|day\s+trad\w*)\b",
    r"\b(?:margin\s+calls?|pips|leveraged\s+(?:trades?|positions?|etfs?))\b",
    r"\b\d+\s*x\s+leverage\b|\bleverage\s+(?:of\s+)?1?:\s*\d+",
    r"\b(?:candlesticks?|ohlcv?|vwap|bollinger|macd|rsi|ema\s*\d+|sma\s*\d+)\b",
    r"\b(?:investment|stock|equity|crypto|trading|mutual\s+fund)\s+portfolios?\b",
    r"\bportfolio\s+(?:value|allocation|rebalanc\w*|returns?)\b",
    r"\b(?:tickers?|watchlists?|mutual\s+funds?|sips?\s+in)\b",
    r"\b(?:buy|sell|long|short)\s+\d+(?:\.\d+)?\s*(?:lots?|shares|units|contracts)\b",
    r"\b(?:bull|bear)ish\b",
)

# --- broker -------------------------------------------------------------------------------------
_BROKER = _rx(
    r"\bbroker(?:s|age)?\b",
    r"\bdemat\b",
    r"\b(?:dhan|dhanhq|mt[45]|metatrader|zerodha|kite\s+(?:connect|api)|upstox|groww"
    r"|angel\s?one|icici\s?direct|hdfc\s+securities|interactive\s+brokers|ibkr|robinhood"
    r"|alpaca|binance|coinbase|kraken|oanda|fxcm|exness|ic\s+markets|pepperstone)\b",
    r"\b(?:trading|broker|demat|dhan)\s+(?:account|client)\s*(?:id|number|no\.?)?\b",
    r"\bclient\s+id\b",
)

# --- positions ----------------------------------------------------------------------------------
_POSITIONS = _rx(
    r"\b(?:open|closed|current|my|net|long|short|existing|overnight)\s+positions?\b",
    r"\bpositions?\s+(?:size|sizing|book|report|list|snapshot)s?\b",
    r"\bholdings\b",
    r"\b(?:my|stock|share|demat|equity|fund|crypto)\s+holding\b",
    r"\bp\s*&\s*l\b",
    r"\b(?:pnl|mtm)\b",
    r"\b(?:un)?reali[sz]ed\s+(?:gains?|loss(?:es)?|profits?|p\s*&?\s*l)\b",
    r"\b(?:avg\.?|average)\s+(?:buy\s+|entry\s+)?(?:price|cost)\b",
    r"\bentry\s+price\b",
    r"\b(?:qty|quantity|lots?|volume)\s*[:=]\s*\d",
    r"\b(?:market|net|position|portfolio|currency|fx|delta)\s+exposure\b",
)

# --- logs and tracebacks -------------------------------------------------------------------------
_LOGS = _rx(
    r"Traceback \(most recent call last\)",
    r"^\s*File \"[^\"]+\", line \d+",
    r"^\s+at \S.*:\d+(?::\d+)?\)?\s*$",  # JS / Java stack frames
    r"^\s*at [\w$.<>]+\([\w$.]+\.(?:java|kt|scala):\d+\)",
    r"\bException in thread\b",
    r"^\s*(?:[a-z_][\w]*\.)*[A-Z]\w*(?:Error|Exception|Panic):\s",
    r"^\s*\[?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}",  # timestamped line
    r"^\s*\[?(?:DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL|TRACE)\]?[\s:|-]",
    r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+\S+(?:\[\d+\])?:",  # syslog
    r"\"(?:level|levelname|severity|log_level)\"\s*:\s*\"",  # JSON logs
    r"\b\d{1,3}(?:\.\d{1,3}){3} - \S+ \[\d{2}/\w{3}/\d{4}",  # access log
    r"\bpanic: |\bgoroutine \d+ \[",
    r"(?i:\bsegmentation fault\b|\bcore dumped\b)",
    r"(?i:\b(?:docker|kubectl|journalctl)\s+logs?\b)",
    ignore_case=False,
)

_RULES: tuple[tuple[Category, re.Pattern[str]], ...] = (
    ("secrets", _SECRET_TEXT),
    ("finance", _FINANCE),
    ("finance", _FINANCE_IDS),
    ("trading", _TRADING),
    ("broker", _BROKER),
    ("positions", _POSITIONS),
    ("logs", _LOGS),
)


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _bare_secret(text: str) -> bool:
    for m in _BARE_TOKEN.finditer(text):
        v = m.group(0)
        if (
            any(c.islower() for c in v)
            and any(c.isupper() for c in v)
            and any(c.isdigit() for c in v)
            and entropy(v) >= BARE_TOKEN_ENTROPY
        ):
            return True
    return False


def text_categories(text: str) -> set[str]:
    """Every category `text` falls in (empty set = nothing sensitive found)."""
    found: set[str] = set()
    if not text:
        return found
    if secret_hits(text) or _bare_secret(text):
        found.add("secrets")
    for cat, pat in _RULES:
        if cat not in found and pat.search(text):
            found.add(cat)
    if "finance" not in found:
        for m in _CARD.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and _luhn(digits):
                found.add("finance")
                break
    return found


@dataclass(slots=True)
class ChatClassification:
    """`external_ok` False means: this request must be answered by local Ollama."""

    external_ok: bool
    categories: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str | None:
        """The fallback reason code: `context` beats `filter`; None when external_ok."""
        if self.external_ok:
            return None
        return "context" if any(r.startswith("context:") for r in self.reasons) else "filter"


def classify_chat(
    messages: Iterable[Mapping[str, str]],
    *,
    context: str = "general",
    research_external_allowed: bool = False,
) -> ChatClassification:
    """Classify the whole outbound payload (every message, every role)."""
    reasons: list[str] = []
    research_ok = context == "research" and research_external_allowed
    if context not in CONTEXTS:
        reasons.append("context:unknown")
    elif context in LOCAL_CONTEXTS and not research_ok:
        reasons.append(f"context:{context}")
    cats: set[str] = set()
    for m in messages:
        cats |= text_categories(str(m.get("content") or ""))
    blocking = cats & HARD if research_ok else cats
    reasons.extend(f"content:{c}" for c in sorted(blocking))
    return ChatClassification(not reasons, sorted(blocking), reasons)


__all__ = [
    "CONTEXTS",
    "ChatClassification",
    "Context",
    "classify_chat",
    "text_categories",
]
