"""Token redaction for everything that leaves the GitHub client: exceptions, logs, bodies.

The configured token is replaced wherever it appears, and so is anything shaped like a
GitHub credential (classic `ghp_`, OAuth `gho_`, user/server/refresh `ghu_`/`ghs_`/`ghr_`,
fine-grained `github_pat_`) or an Authorization header value, even if it is not ours.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:bearer|token)\s+[^\s\"',}]+"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-.=]{8,}"),
    # OpenRouter / OpenAI / Anthropic-style API keys (docs/25-openrouter.md)
    re.compile(r"\bsk-(?:or-v1-|proj-|ant-)?[A-Za-z0-9_-]{16,}"),
    # Google API keys (Gemini, docs/26-ai-providers.md)
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
)


def redact(text: str, secrets: Iterable[str | None] = ()) -> str:
    """Return `text` with `secrets` and any credential-shaped string replaced."""
    for s in secrets:
        if s:
            text = text.replace(s, REDACTED)
    for pat in _PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.re.groups else "") + REDACTED, text)
    return text


def redact_obj(value: Any, secrets: Iterable[str | None] = ()) -> Any:
    """Redact every string inside a JSON-like value (dicts, lists, scalars)."""
    secrets = tuple(secrets)
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {k: redact_obj(v, secrets) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_obj(v, secrets) for v in value]
    return value
