"""Conversations with the agents: storage, history replay and one turn.

Storage (ai.conversations / ai.messages): every API message of a turn is a row, with
`blocks` holding the exact content as sent (user side, redacted) or received (Claude's,
thinking blocks unchanged). The next turn replays them byte-for-byte, which keeps both the
prompt cache and Claude's thinking blocks valid. `content` holds the readable text: the
user's own words, Claude's answer; tool-only rows have empty content and are hidden from the
conversation view. A conversation longer than AI_CONVERSATION_MAX_MESSAGES must be continued
in a new one (older turns are never edited or dropped).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai import loop
from app.ai.agents import BUDGET_OF, ROUTER_SYSTEM, SYSTEM, AgentName, parse_route
from app.ai.budget import IST
from app.ai.gateway import ClaudeGateway
from app.ai.tools.registry import Registry, ToolContext
from app.models.ai import Conversation, Message


class ConversationError(Exception):
    code = "conversation_error"
    status = 409


class ConversationNotFound(ConversationError):
    code = "not_found"
    status = 404


def _text(blocks: Sequence[dict[str, Any]]) -> str:
    return "".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text").strip()


def visible(m: Message) -> bool:
    """Rows the conversation view shows: the user's words and Claude's answers."""
    return m.role in ("user", "assistant") and bool(m.content.strip())


def replay(rows: Sequence[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in rows:
        if m.role not in ("user", "assistant"):
            continue
        if m.blocks is not None:
            out.append({"role": m.role, "content": m.blocks})
        elif m.content.strip():  # a turn from before the Claude gateway: plain text
            out.append({"role": m.role, "content": m.content})
    return out


async def get_conversation(
    db: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    conv = await db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conv is None:
        raise ConversationNotFound("conversation not found")
    return conv


async def messages_of(db: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    return list(
        (
            await db.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.id)
            )
        ).all()
    )


async def route(gateway: ClaudeGateway, text: str, user_id: uuid.UUID | None) -> AgentName:
    """The router model picks the agent (AI_MODEL_ROUTER); general when it is off."""
    model = gateway.settings.AI_MODEL_ROUTER.strip()
    if not model:
        return "general"
    res = await gateway.call(
        agent="router",
        budget=BUDGET_OF["router"],
        system=ROUTER_SYSTEM,
        messages=[{"role": "user", "content": text[:2000]}],
        model=model,
        max_tokens=1024,
    )
    return parse_route(res.text)


@dataclass(slots=True)
class TurnResult:
    conversation_id: uuid.UUID
    agent: str
    text: str
    cost_usd: Decimal
    steps: int
    stop_reason: str
    approvals: list[uuid.UUID]
    message_id: int | None


def _stamp(prompt: str) -> str:
    """The date goes with the user's message, not the system prompt (which stays cached)."""
    now = datetime.now(UTC).astimezone(IST)
    return f"[{now:%a %d %b %Y, %H:%M} IST]\n{prompt}"


async def start(
    maker: async_sessionmaker[AsyncSession],
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    agent: AgentName,
    title: str,
    model: str,
) -> tuple[Conversation, list[dict[str, Any]]]:
    """The conversation (created when new) and its history to replay."""
    async with maker() as db, db.begin():
        if conversation_id is None:
            conv = Conversation(
                user_id=user_id,
                title=title[:200] or "New chat",
                model=model,
                provider="anthropic",
                agent=agent,
            )
            db.add(conv)
            await db.flush()
            return conv, []
        conv = await get_conversation(db, user_id, conversation_id)
        rows = await messages_of(db, conv.id)
    return conv, replay(rows)


async def turn(
    gateway: ClaudeGateway,
    registry: Registry,
    ctx: ToolContext,
    *,
    conversation: Conversation,
    history: list[dict[str, Any]],
    prompt: str,
    agent: AgentName,
    on_event: loop.OnEvent | None = None,
    context: str | None = None,
) -> TurnResult:
    """One turn. `context` is extra read-only data the app sent with the message (the user's
    workspace, app/ai/workspace.py): a second text block, redacted like the rest, kept in the
    stored blocks so the turn replays exactly; the readable message stays the user's words."""
    settings = ctx.settings
    if len(history) + 2 > settings.AI_CONVERSATION_MAX_MESSAGES:
        raise ConversationError("this conversation is full; continue in a new chat")
    blocks: list[dict[str, Any]] = [{"type": "text", "text": _stamp(prompt)}]
    if context:
        blocks.append({"type": "text", "text": context})
    user_msg = {"role": "user", "content": blocks}
    sent_user = ctx.redactor.messages([user_msg])[0]
    result = await loop.run(
        gateway,
        registry,
        ctx,
        system=SYSTEM[agent],
        budget=BUDGET_OF[agent],
        history=[*history, sent_user],
        on_event=on_event,  # text and tool events stream to the client as they happen
    )
    last_id: int | None = None
    async with ctx.maker() as db, db.begin():
        db.add(
            Message(
                conversation_id=conversation.id,
                role="user",
                content=prompt,
                blocks=sent_user["content"],
                status="complete",
            )
        )
        for i, m in enumerate(result.messages):
            final = i == len(result.messages) - 1
            row = Message(
                conversation_id=conversation.id,
                role=m["role"],
                # Only the turn's answer is readable text; tool steps stay as blocks.
                content=result.text if final and m["role"] == "assistant" else "",
                blocks=m["content"],
                model=settings.AI_MODEL_MAIN if m["role"] == "assistant" else None,
                provider="anthropic" if m["role"] == "assistant" else None,
                status="complete",
                cost_usd=result.cost_usd if final else None,
            )
            db.add(row)
            await db.flush()
            last_id = int(row.id)
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conversation.id)
            .values(updated_at=func.now(), agent=agent)
        )
    return TurnResult(
        conversation.id,
        agent,
        result.text,
        result.cost_usd,
        result.steps,
        result.stop_reason,
        result.approvals,
        last_id,
    )


__all__ = [
    "ConversationError",
    "ConversationNotFound",
    "TurnResult",
    "get_conversation",
    "messages_of",
    "replay",
    "route",
    "start",
    "turn",
    "visible",
]
