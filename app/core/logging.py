"""Structured JSON logging via structlog, with request-ID context propagation."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any

import structlog

REDACTED = "***"
# Any key containing one of these (case-insensitive) has its value masked, at any depth.
SENSITIVE_KEY = re.compile(
    r"token|passw|secret|authorization|cookie|api[_-]?key|apikey|credential|private[_-]?key"
    r"|session[_-]?id|dsn|database_url",
    re.IGNORECASE,
)
# user:password@ in URLs/DSNs, and bearer tokens, inside free-text values.
_URL_CREDS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)(?P<user>[^:/@\s]+):[^@/\s]+@", re.I)
_BEARER = re.compile(r"(?P<p>\b(?:bearer|token)\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
# GitHub credentials in free text (classic/OAuth/app tokens and fine-grained PATs).
_GITHUB_TOKEN = re.compile(r"github_pat_[A-Za-z0-9_]{8,}|gh[pousr]_[A-Za-z0-9]{8,}")
# OpenRouter (sk-or-v1-...), OpenAI and Anthropic-style API keys in free text.
_API_KEY = re.compile(r"\bsk-(?:or-v1-|proj-|ant-)?[A-Za-z0-9_-]{16,}")
# Discord webhook URLs carry their credential in the path.
_DISCORD_WEBHOOK = re.compile(
    r"(?P<p>discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9._-]+", re.IGNORECASE
)
_MAX_DEPTH = 6
# Secret VALUES known to this process (core.app_settings and their .env fallbacks), replaced
# wherever they appear: a token's shape is not always recognisable, its exact value is.
_known_secrets: tuple[str, ...] = ()
_MIN_SECRET_LEN = 8


def register_secret_values(values: Iterable[str | None]) -> None:
    """Replace the set of exact secret values scrubbed from logs, incidents and AI briefs."""
    global _known_secrets
    found = {v.strip() for v in values if v and len(v.strip()) >= _MIN_SECRET_LEN}
    _known_secrets = tuple(sorted(found, key=len, reverse=True))


def _scrub_text(value: str) -> str:
    for secret in _known_secrets:
        if secret in value:
            value = value.replace(secret, REDACTED)
    value = _DISCORD_WEBHOOK.sub(lambda m: f"{m['p']}{REDACTED}", value)
    value = _URL_CREDS.sub(lambda m: f"{m['scheme']}{m['user']}:{REDACTED}@", value)
    value = _BEARER.sub(lambda m: f"{m['p']}{REDACTED}", value)
    value = _GITHUB_TOKEN.sub(REDACTED, value)
    return _API_KEY.sub(REDACTED, value)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, Mapping):
        return {
            k: REDACTED if isinstance(k, str) and SENSITIVE_KEY.search(k) else _scrub(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(_scrub(v, depth + 1) for v in value)
    return value


def scrub(value: Any) -> Any:
    """Public redaction helper: secret-looking keys masked, credentials in text removed."""
    return _scrub(value)


def scrub_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Chat messages with known secret values and credential shapes removed from their text,
    applied to every prompt before it reaches a model (OpenRouter or Ollama)."""
    return [
        {**m, "content": _scrub_text(m["content"])} if isinstance(m.get("content"), str) else m
        for m in messages
    ]


def redact_secrets(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """structlog processor: mask secret-looking keys and credentials embedded in strings."""
    for key in list(event_dict):
        value = event_dict[key]
        if SENSITIVE_KEY.search(key):
            event_dict[key] = REDACTED
        elif key not in ("timestamp", "level", "logger"):
            event_dict[key] = _scrub(value)
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    # Exceptions are also handed to the incident sink (docs/21-self-repair.md); imported here
    # because the sink itself uses `scrub` from this module.
    from app.core.exception_sink import capture_exceptions, install_excepthook

    install_excepthook()
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        capture_exceptions,
    ]
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_secrets,  # last before rendering, so tracebacks are scrubbed too
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, alembic) through the same JSON renderer.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                # Render stdlib tracebacks as text so redact_secrets can scrub them (a raw
                # exc_info tuple used to be serialised with its repr, secrets included).
                structlog.processors.format_exc_info,
                redact_secrets,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    # Access logs are replaced by our own request middleware.
    logging.getLogger("uvicorn.access").disabled = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
