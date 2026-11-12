"""Business tools (Aquakart, Pawsattva) and Spasta todos.

None of these systems is connected to this backend yet: Aquakart and Pawsattva live in their
own services, and Spasta todos live in the frontend's Supabase. Until a read-only connection
is configured, every tool here reports itself unavailable, so it is not offered to Claude at
all (no tokens spent on a call that cannot work), and its handler refuses if called anyway.
Connecting one means: a read-only API URL + token (or database role) in settings, a handler
that summarises (never a raw table), and its host added to the egress allowlist
(docs/egress.md).
"""

from __future__ import annotations

from typing import Any

from app.ai.tools.registry import Draft, Tool, ToolContext, ToolRefused, obj
from app.core.config import Settings

BUSINESS = frozenset({"business", "report"})
GENERAL = frozenset({"general", "business"})


def _not_connected(system: str) -> Any:
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> str | Draft:
        raise ToolRefused(f"{system} is not connected to Aureon yet")

    return handler


def _off(_: Settings) -> bool:
    return False


TOOLS = [
    Tool(
        "get_aquakart_sales_summary",
        "Aquakart sales for a period: orders, revenue, top products (totals only).",
        obj({"days": {"type": "integer", "minimum": 1, "maximum": 90}}),
        _not_connected("Aquakart"),
        "read",
        BUSINESS,
        _off,
    ),
    Tool(
        "get_aquakart_open_queries",
        "Open Aquakart customer queries: count, age and topic (no customer details).",
        obj({}),
        _not_connected("Aquakart"),
        "read",
        BUSINESS,
        _off,
    ),
    Tool(
        "get_pawsattva_summary",
        "Pawsattva business summary for a period (totals only).",
        obj({"days": {"type": "integer", "minimum": 1, "maximum": 90}}),
        _not_connected("Pawsattva"),
        "read",
        BUSINESS,
        _off,
    ),
    Tool(
        "draft_aquakart_blog",
        "Draft an Aquakart blog post. Saved as a DRAFT only, after the user approves.",
        obj(
            {
                "title": {"type": "string", "minLength": 3, "maxLength": 200},
                "body_markdown": {"type": "string", "minLength": 50, "maxLength": 20000},
            },
            ["title", "body_markdown"],
        ),
        _not_connected("Aquakart"),
        "write",
        frozenset({"business"}),
        _off,
    ),
    Tool(
        "get_todos",
        "The user's open Spasta todos (title, due date, status).",
        obj({"limit": {"type": "integer", "minimum": 1, "maximum": 50}}),
        _not_connected("Spasta todos (Supabase)"),
        "read",
        GENERAL,
        _off,
    ),
    Tool(
        "add_todo",
        "Add a Spasta todo, after the user approves.",
        obj(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "due": {"type": "string", "maxLength": 10},
            },
            ["title"],
        ),
        _not_connected("Spasta todos (Supabase)"),
        "write",
        GENERAL,
        _off,
    ),
]
