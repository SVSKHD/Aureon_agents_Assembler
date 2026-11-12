"""The tool registry (docs/26-ai-providers.md).

A tool is a name, a description, a JSON schema for its input, a handler and a kind:

- "read":  the handler runs locally and returns a SHORT text summary (never a raw table).
  The summary is capped at AI_TOOL_RESULT_MAX_TOKENS (about 4 characters a token).
  A read tool may instead return a Suggestion (app/ai/tools/workspace.py): a card the app
  shows, which the user saves themselves by confirming it; the server never acts on it.
- "write": the handler NEVER acts. It validates the request (and, for trades, the hard risk
  limits in app/ai/risk.py) and returns a Draft. The loop stores the draft as an
  ai.approvals row (status pending) and tells Claude "awaiting user approval". The action
  runs only when an admin approves it, and then the backend runs it (app/ai/approvals.py),
  never Claude.

Inputs are validated against the schema here before any handler runs (Claude's arguments are
data, not trusted). A tool whose `available` is False (its system is not connected) is not
offered to Claude at all.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.redact import Redactor
from app.core.config import Settings

Kind = Literal["read", "write"]
CHARS_PER_TOKEN = 4


class ToolInputError(ValueError):
    """The arguments do not match the tool's schema."""


class ToolRefused(Exception):
    """A write request that breaks a rule (risk limits, not connected, ...): never filed."""


@dataclass(slots=True)
class ToolContext:
    settings: Settings
    maker: async_sessionmaker[AsyncSession]
    user_id: uuid.UUID | None
    is_admin: bool
    agent: str
    conversation_id: uuid.UUID | None = None
    redis: Redis | None = None
    adapters: Mapping[str, Any] = field(default_factory=dict)
    redactor: Redactor = field(default_factory=Redactor)


@dataclass(frozen=True, slots=True)
class Suggestion:
    """What a suggest tool returns: an item for the user to confirm in the app (a todo, a
    reminder, ...). Nothing acts on it on the server; the app saves it only when the user
    confirms (app/ai/tools/workspace.py)."""

    kind: str
    fields: dict[str, Any]
    summary: str


@dataclass(frozen=True, slots=True)
class Draft:
    """What a write tool asks for: the action, its validated payload, a one-line summary."""

    action: str
    payload: dict[str, Any]
    summary: str


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[str | Draft | Suggestion]]


def _always(_: Settings) -> bool:
    return True


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Handler
    kind: Kind
    agents: frozenset[str]
    available: Callable[[Settings], bool] = _always

    def api(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate(schema: Mapping[str, Any], value: Any, path: str = "input") -> None:
    """The subset of JSON Schema the tools use: type, properties, required,
    additionalProperties false, enum, minimum/maximum, minLength/maxLength, items, maxItems."""
    kind = schema.get("type")
    if kind is not None:
        allowed = _TYPES[kind]
        if not isinstance(value, allowed) or (kind != "boolean" and isinstance(value, bool)):
            raise ToolInputError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolInputError(f"{path} must be one of {schema['enum']}")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolInputError(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolInputError(f"{path} must be <= {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ToolInputError(f"{path} must be > {schema['exclusiveMinimum']}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ToolInputError(f"{path} is too short")
        if len(value) > schema.get("maxLength", 10_000):
            raise ToolInputError(f"{path} is too long")
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", 1000):
            raise ToolInputError(f"{path} has too many items")
        for i, item in enumerate(value):
            validate(schema.get("items", {}), item, f"{path}[{i}]")
    if isinstance(value, dict):
        props: Mapping[str, Any] = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise ToolInputError(f"{path}.{name} is required")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                raise ToolInputError(f"{path} has unknown fields: {', '.join(extra)}")
        for name, sub in props.items():
            if name in value:
                validate(sub, value[name], f"{path}.{name}")


def cap(text: str, settings: Settings) -> str:
    """A tool result cut to AI_TOOL_RESULT_MAX_TOKENS (about 4 characters a token)."""
    limit = settings.AI_TOOL_RESULT_MAX_TOKENS * CHARS_PER_TOKEN
    if len(text) <= limit:
        return text
    return text[: limit - 60].rstrip() + "\n…[cut: ask for a narrower question for more]"


class Registry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.add(t)

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def for_agent(self, agent: str, settings: Settings) -> list[Tool]:
        """The tools offered to one agent, in a fixed (name) order so the cache prefix is
        stable."""
        return sorted(
            (t for t in self._tools.values() if agent in t.agents and t.available(settings)),
            key=lambda t: t.name,
        )


def obj(properties: dict[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


__all__ = [
    "Draft",
    "Registry",
    "Suggestion",
    "Tool",
    "ToolContext",
    "ToolInputError",
    "ToolRefused",
    "cap",
    "obj",
    "validate",
]
