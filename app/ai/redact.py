"""Redaction before every Claude call (docs/26-ai-providers.md).

Replaced with a typed placeholder, so Claude still knows what kind of thing was there:

- customer and person names: names the tools registered from their own data, and names
  after a label ("customer: Ravi Kumar", "Mr. Rao")                         -> [NAME]
- email addresses                                                            -> [EMAIL]
- phone numbers (Indian mobiles, +country numbers, grouped landlines)         -> [PHONE]
- postal addresses: "address: ..." to the end of the line, and a 6-digit PIN
  after a comma                                                              -> [ADDRESS]
- account / client / customer / demat / folio ids after their label, PAN, Aadhaar,
  IFSC, card numbers (Luhn-checked)                                          -> [ACCOUNT_ID]
- API keys, tokens, bearer credentials, URL passwords and every secret value the app
  registered with the log scrubber (app/core/logging.py)                     -> [REDACTED]

What is NOT removed: symbols, prices, quantities, amounts, dates and plain numbers. That is
the data the agents reason about.

Only text is touched: user text, text blocks and tool results. Thinking blocks and Claude's
own tool calls pass through unchanged (they are signed and must be replayed as-is).
Redaction is idempotent: placeholders never match a pattern again.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from app.core import logging as app_logging

# Bounded repetitions everywhere: an unbounded `+` before a literal is quadratic on a long
# run of letters (a large tool result), and redaction runs on every call.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b")
_PHONE = re.compile(
    r"(?<![\w.])(?:"
    r"\+\d{1,3}[\s-]?\(?\d{2,5}\)?(?:[\s-]?\d{2,5}){1,4}"  # +91 98480 22338, +1 (415) 555 0100
    r"|0?[6-9]\d{4}[\s-]?\d{5}"  # 98480 22338, 09848022338
    r"|0\d{2,4}[\s-]\d{6,8}"  # 040-23456789
    r")(?![\w.])"
)
_LABELLED_ID = re.compile(
    r"(?P<label>\b(?:account|acct|a/c|client|customer|cust|demat|folio|dp|member|ucc|"
    r"user|login|order)[\s_-]*(?:id|no\.?|number|code|#)?\s*[:#=-]?\s*)"
    r"(?P<value>[A-Za-z0-9][A-Za-z0-9-]{4,})",
    re.IGNORECASE,
)
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_AADHAAR = re.compile(r"\b\d{4}[\s-]\d{4}[\s-]\d{4}\b")
_IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
_CARD = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
_ADDRESS_LINE = re.compile(
    r"(?P<label>\b(?:address|addr|ship(?:ping)? to|deliver(?:y)? to|located at)\s*[:=-]\s*)"
    r"(?P<value>[^\n]+)",
    re.IGNORECASE,
)
_PIN_AFTER_COMMA = re.compile(r",\s*(?:[A-Za-z .]+[-\s])?\d{6}\b")
_NAME_LABEL = re.compile(
    r"(?P<label>\b(?i:customer|client|buyer|seller|contact|name|owner|patient|parent|"
    r"user)(?i:\s+name)?\s*[:=-]\s*)"
    r"(?P<value>[A-Z][a-zA-Z'.-]+(?:\s+[A-Z][a-zA-Z'.-]+){0,3})"
)
_HONORIFIC = re.compile(
    r"\b(?P<label>(?:Mr|Mrs|Ms|Miss|Dr|Shri|Smt|Sri)\.?\s+)"
    r"(?P<value>[A-Z][a-zA-Z'.-]+(?:\s+[A-Z][a-zA-Z'.-]+){0,2})"
)

_URL_CREDS = re.compile(
    r"(?P<scheme>\b[a-z][a-z0-9+.-]{0,15}://)(?P<user>[^:/@\s]{1,100}):[^@/\s]{1,200}@", re.I
)
_BEARER = re.compile(r"(?P<p>\b(?:bearer|token)\s{1,5})[A-Za-z0-9._~+/=-]{8,500}", re.I)
_DISCORD_WEBHOOK = re.compile(
    r"(?P<p>discord(?:app)?\.com/api/webhooks/\d{1,30}/)[A-Za-z0-9._-]{1,200}", re.I
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,2000}\.[A-Za-z0-9_-]{5,4000}\.[A-Za-z0-9_-]{5,2000}")
_KEYS = re.compile(
    r"\bsk-[A-Za-z0-9_-]{16,300}|\bAIza[0-9A-Za-z_-]{30,60}|\bgithub_pat_[A-Za-z0-9_]{8,300}"
    r"|\bgh[pousr]_[A-Za-z0-9]{8,300}|\bxox[abpr]-[A-Za-z0-9-]{10,300}"
)

NAME, EMAIL, PHONE, ADDRESS, ACCOUNT, SECRET = (
    "[NAME]",
    "[EMAIL]",
    "[PHONE]",
    "[ADDRESS]",
    "[ACCOUNT_ID]",
    "[REDACTED]",
)


def _secrets(value: str) -> str:
    """The app's own secret values (registered with the log scrubber), then credential
    shapes. Bounded patterns: the log scrubber's are quadratic on long text."""
    for secret in getattr(app_logging, "_known_secrets", ()):
        if secret in value:
            value = value.replace(secret, SECRET)
    value = _URL_CREDS.sub(lambda m: f"{m['scheme']}{m['user']}:{SECRET}@", value)
    value = _BEARER.sub(lambda m: f"{m['p']}{SECRET}", value)
    value = _DISCORD_WEBHOOK.sub(lambda m: f"{m['p']}{SECRET}", value)
    value = _JWT.sub(SECRET, value)
    return _KEYS.sub(SECRET, value)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return ACCOUNT if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


def _labelled(placeholder: str) -> Any:
    def sub(m: re.Match[str]) -> str:
        value = m.group("value")
        if value.startswith("[") or not any(c.isdigit() for c in value):
            return m.group(0)  # a placeholder already, or a word ("client name: ...")
        return m.group("label") + placeholder

    return sub


def _named(m: re.Match[str]) -> str:
    return m.group("label") + NAME


class Redactor:
    """Holds the names the tools found in their own data (customer names and the like),
    so they are removed wherever they appear later in the conversation."""

    def __init__(self, names: Iterable[str] = ()) -> None:
        self._names: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        self.add_names(names)

    def add_names(self, names: Iterable[str | None]) -> None:
        added = {n.strip() for n in names if n and len(n.strip()) >= 3}
        if added - self._names:
            self._names |= added
            alts = sorted((re.escape(n) for n in self._names), key=len, reverse=True)
            self._pattern = re.compile(r"\b(?:" + "|".join(alts) + r")\b", re.IGNORECASE)

    def text(self, value: str) -> str:
        if not value:
            return value
        out = _secrets(value)
        out = _EMAIL.sub(EMAIL, out)
        out = _ADDRESS_LINE.sub(lambda m: m.group("label") + ADDRESS, out)
        out = _PIN_AFTER_COMMA.sub(", " + ADDRESS, out)
        out = _CARD.sub(_card, out)
        out = _AADHAAR.sub(ACCOUNT, out)
        out = _PAN.sub(ACCOUNT, out)
        out = _IFSC.sub(ACCOUNT, out)
        out = _LABELLED_ID.sub(_labelled(ACCOUNT), out)
        out = _PHONE.sub(PHONE, out)
        if self._pattern is not None:
            out = self._pattern.sub(NAME, out)
        out = _NAME_LABEL.sub(_named, out)
        return _HONORIFIC.sub(_named, out)

    def block(self, block: Any) -> Any:
        """One content block (dict). Text and tool results only; anything else unchanged."""
        if not isinstance(block, dict):
            return block
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            return {**block, "text": self.text(block["text"])}
        if kind == "tool_result":
            content = block.get("content")
            if isinstance(content, str):
                return {**block, "content": self.text(content)}
            if isinstance(content, list):
                return {**block, "content": [self.block(b) for b in content]}
        return block

    def messages(self, messages: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """User-side content only (user text, tool results). Claude's own turns are replayed
        byte-for-byte: they were produced from redacted input, and editing them would
        invalidate their thinking blocks."""
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") != "user":
                out.append(dict(m))
                continue
            content = m.get("content")
            if isinstance(content, str):
                out.append({**m, "content": self.text(content)})
            elif isinstance(content, list):
                out.append({**m, "content": [self.block(b) for b in content]})
            else:
                out.append(dict(m))
        return out


def redact(text: str, names: Iterable[str] = ()) -> str:
    return Redactor(names).text(text)


__all__ = ["Redactor", "redact"]
