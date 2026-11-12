"""Suggestion tools for the user's workspace: todos, tasks, reminders, calendar, notes.

These never write anything, here or anywhere. Each returns a Suggestion: the loop records the
call, streams a `suggestion` event, and tells Claude the card is shown and nothing is saved.
The app shows the card in the conversation; when the user confirms it, the browser saves the
item to their workspace with their own login (Supabase, row-level security). A suggestion
the user does not confirm is never saved.

What is in the workspace reaches Claude only as the read-only summary the app sends with a
message (app/ai/workspace.py).
"""

from __future__ import annotations

from typing import Any

from app.ai.tools.registry import Handler, Suggestion, Tool, ToolContext, obj

EVERYONE = frozenset({"general", "business", "investments"})
DAY = {"type": "string", "minLength": 10, "maxLength": 10, "description": "YYYY-MM-DD"}
TIME = {"type": "string", "minLength": 5, "maxLength": 5, "description": "HH:MM, 24-hour, IST"}
TITLE = {"type": "string", "minLength": 1, "maxLength": 200}
NOTES = {"type": "string", "maxLength": 2000}

SHOWN = (
    "Shown to the user as a {what} card. Nothing is saved unless they confirm it in the app; "
    "do not say it was added."
)


def _suggest(kind: str, what: str) -> Handler:
    async def handler(ctx: ToolContext, args: dict[str, Any]) -> Suggestion:
        fields = {k: v.strip() if isinstance(v, str) else v for k, v in args.items()}
        return Suggestion(kind=kind, fields=fields, summary=SHOWN.format(what=what))

    return handler


TOOLS = [
    Tool(
        "suggest_todo",
        "Suggest a todo for the user's list. Shown as a card; saved only if the user confirms.",
        obj({"title": TITLE, "notes": NOTES}, ["title"]),
        _suggest("todo", "todo"),
        "read",
        EVERYONE,
    ),
    Tool(
        "suggest_task",
        "Suggest a task, optionally with a deadline. Shown as a card; saved only if the user "
        "confirms.",
        obj({"title": TITLE, "deadline": DAY, "notes": NOTES}, ["title"]),
        _suggest("task", "task"),
        "read",
        EVERYONE,
    ),
    Tool(
        "suggest_reminder",
        "Suggest a reminder at a date and time (IST). Shown as a card; saved only if the user "
        "confirms.",
        obj({"title": TITLE, "date": DAY, "time": TIME, "notes": NOTES}, ["title", "date"]),
        _suggest("reminder", "reminder"),
        "read",
        EVERYONE,
    ),
    Tool(
        "suggest_calendar_event",
        "Suggest a calendar event (date, start time, duration). Shown as a card; saved only if "
        "the user confirms.",
        obj(
            {
                "title": TITLE,
                "date": DAY,
                "time": TIME,
                "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
                "notes": NOTES,
            },
            ["title", "date"],
        ),
        _suggest("calendar", "calendar event"),
        "read",
        EVERYONE,
    ),
    Tool(
        "suggest_note",
        "Suggest saving a note (markdown). Shown as a card; saved only if the user confirms.",
        obj(
            {"title": TITLE, "body": {"type": "string", "minLength": 1, "maxLength": 8000}},
            ["title", "body"],
        ),
        _suggest("note", "note"),
        "read",
        EVERYONE,
    ),
]

__all__ = ["TOOLS"]
