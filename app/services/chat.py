"""Conversation storage (user-scoped) and the streaming chat pipeline.

Streaming design
----------------
A *producer* task talks to Ollama (inside the request timeout) and puts events on a queue.
The SSE generator (*consumer*) reads that queue, sending a keep-alive comment whenever it
has been quiet for CHAT_HEARTBEAT_SECONDS. A long CPU model load therefore never looks idle
to Nginx or Cloudflare, and a disconnected client is noticed within one heartbeat.

When the client goes away, the generator is closed. That cancels the producer, which closes
the Ollama HTTP stream (Ollama stops generating), and the partial answer is saved with
status `cancelled`. Persistence runs under asyncio.shield, so a second cancel can't drop it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.agents.ollama.client import ModelInfo, OllamaClient, OllamaError
from app.agents.ollama.limiter import Ticket
from app.agents.registry import AgentRegistry
from app.core.logging import get_logger
from app.models.ai import Conversation, Message, ModelInventory

log = get_logger(__name__)

TITLE_LEN = 80
RUN_STATUS = {
    "complete": "succeeded",
    "cancelled": "cancelled",
    "error": "failed",
    "timeout": "failed",
    "running": "cancelled",
}


# --- storage --------------------------------------------------------------------------


class ConversationService:
    """Every query filters on user_id here, not only in the router."""

    def __init__(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        self.db = db
        self.user_id = user_id

    async def page(self, limit: int, offset: int) -> tuple[list[Conversation], int]:
        base = select(Conversation).where(Conversation.user_id == self.user_id)
        total = await self.db.scalar(select(func.count()).select_from(base.subquery()))
        rows = await self.db.scalars(
            base.order_by(Conversation.updated_at.desc(), Conversation.id)
            .limit(limit)
            .offset(offset)
        )
        return list(rows), int(total or 0)

    async def get(self, conversation_id: uuid.UUID) -> Conversation | None:
        conv: Conversation | None = await self.db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == self.user_id
            )
        )
        return conv

    async def messages(self, conversation_id: uuid.UUID) -> list[Message]:
        rows = await self.db.scalars(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.conversation_id == conversation_id, Conversation.user_id == self.user_id)
            .order_by(Message.id)
        )
        return list(rows)

    async def delete(self, conversation_id: uuid.UUID) -> bool:
        result = await self.db.execute(
            delete(Conversation).where(
                Conversation.id == conversation_id, Conversation.user_id == self.user_id
            )
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def start_turn(
        self, conversation_id: uuid.UUID | None, model: str, new: list[dict[str, str]]
    ) -> tuple[Conversation, list[dict[str, str]], list[int]] | None:
        """Store the new turns and return (conversation, prompt for Ollama, new message ids).

        Returns None if conversation_id is not one of this user's conversations.
        """
        if conversation_id is None:
            first_user = next(m["content"] for m in new if m["role"] == "user")
            conv = Conversation(
                user_id=self.user_id, title=" ".join(first_user.split())[:TITLE_LEN], model=model
            )
            self.db.add(conv)
            await self.db.flush()
            history: list[dict[str, str]] = []
        else:
            found = await self.get(conversation_id)
            if found is None:
                return None
            conv = found
            # Failed or cancelled assistant turns are kept for the record, not re-sent.
            history = [
                {"role": m.role, "content": m.content}
                for m in await self.messages(conv.id)
                if m.role != "assistant" or m.status == "complete"
            ]
            conv.model = model
        rows = [Message(conversation_id=conv.id, role=m["role"], content=m["content"]) for m in new]
        self.db.add_all(rows)
        conv.updated_at = func.now()
        await self.db.flush()
        return conv, history + new, [r.id for r in rows]


async def sync_inventory(db: AsyncSession, models: list[ModelInfo]) -> None:
    for m in models:
        values = {
            "name": m.name,
            "digest": m.digest,
            "size_bytes": m.size,
            "family": m.family,
            "parameter_size": m.parameter_size,
            "quantization": m.quantization,
            "modified_at": m.modified_at,
        }
        stmt = insert(ModelInventory).values(**values)
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=[ModelInventory.name],
                set_={
                    **{k: stmt.excluded[k] for k in values if k != "name"},
                    "last_seen_at": func.now(),
                },
            )
        )


# --- streaming ----------------------------------------------------------------------------


def sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


PING = ": ping\n\n"


@dataclass
class _Turn:
    parts: list[str] = field(default_factory=list)
    status: str = "running"
    error_code: str | None = None
    error: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ChatJob:
    conversation_id: uuid.UUID
    user_message_ids: list[int]
    model: str
    prompt: list[dict[str, str]]
    options: dict[str, Any]


class ChatStream:
    def __init__(
        self,
        job: ChatJob,
        *,
        ollama: OllamaClient,
        maker: async_sessionmaker[AsyncSession],
        ticket: Ticket,
        timeout: float,
        heartbeat: float,
        registry: AgentRegistry | None = None,
    ) -> None:
        self.registry = registry
        self.job = job
        self.ollama = ollama
        self.maker = maker
        self.ticket = ticket
        self.timeout = timeout
        self.heartbeat = heartbeat
        self.turn = _Turn()
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._started = time.monotonic()
        self._persisted: int | None = None

    async def _produce(self) -> None:
        q, turn = self._queue, self.turn
        try:
            async with asyncio.timeout(self.timeout):
                if not self.ticket.slot_free():
                    await q.put(("queued", None))
                await self.ticket.acquire_slot()
                await q.put(("start", None))
                async for chunk in self.ollama.chat_stream(
                    self.job.model, self.job.prompt, self.job.options
                ):
                    piece = (chunk.get("message") or {}).get("content") or ""
                    if piece:
                        turn.parts.append(piece)
                        await q.put(("token", piece))
                    if chunk.get("done"):
                        turn.prompt_tokens = chunk.get("prompt_eval_count")
                        turn.completion_tokens = chunk.get("eval_count")
                        break
            turn.status = "complete"
        except TimeoutError:
            turn.status, turn.error_code = "timeout", "timeout"
            turn.error = f"generation exceeded {self.timeout:g}s"
        except OllamaError as exc:
            turn.status, turn.error_code, turn.error = "error", exc.code, exc.message
        finally:
            # Free the Ollama slot as soon as generation ends (or is cancelled).
            self.ticket.release_slot()
            q.put_nowait(("end", None))

    async def _persist(self) -> int:
        if self._persisted is not None:
            return self._persisted
        t = self.turn
        async with self.maker() as db, db.begin():
            msg = Message(
                conversation_id=self.job.conversation_id,
                role="assistant",
                content="".join(t.parts),
                model=self.job.model,
                status=t.status,
                error=t.error,
                prompt_tokens=t.prompt_tokens,
                completion_tokens=t.completion_tokens,
                duration_ms=int((time.monotonic() - self._started) * 1000),
            )
            db.add(msg)
            await db.execute(
                update(Conversation)
                .where(Conversation.id == self.job.conversation_id)
                .values(updated_at=func.now())
            )
            if t.status == "complete":
                await db.execute(
                    update(ModelInventory)
                    .where(ModelInventory.name == self.job.model)
                    .values(last_used_at=func.now())
                )
            await db.flush()
            self._persisted = msg.id
        log.info(
            "chat_turn",
            conversation_id=str(self.job.conversation_id),
            model=self.job.model,
            status=t.status,
            chars=sum(map(len, t.parts)),
        )
        if self.registry is not None:
            try:  # best-effort: never fail the chat over the registry
                await self.registry.record_run(
                    "ollama",
                    "chat",
                    RUN_STATUS[t.status],
                    int((time.monotonic() - self._started) * 1000),
                    error=t.error,
                    details={
                        "conversation_id": str(self.job.conversation_id),
                        "message_id": msg.id,
                        "model": self.job.model,
                        "completion_tokens": t.completion_tokens,
                    },
                )
            except Exception as exc:
                log.warning("agent_run_record_failed", error=type(exc).__name__)
        return msg.id

    async def events(self) -> AsyncGenerator[str, None]:
        producer = asyncio.create_task(self._produce())
        try:
            yield sse(
                "meta",
                {
                    "conversation_id": str(self.job.conversation_id),
                    "user_message_ids": self.job.user_message_ids,
                    "model": self.job.model,
                },
            )
            while True:
                try:
                    kind, data = await asyncio.wait_for(self._queue.get(), self.heartbeat)
                except TimeoutError:
                    yield PING
                    continue
                if kind == "end":
                    break
                if kind == "token":
                    yield sse("token", {"content": data})
                else:
                    yield sse(kind, {})
            message_id = await asyncio.shield(self._persist())
            t = self.turn
            if t.status == "complete":
                yield sse(
                    "done",
                    {
                        "message_id": message_id,
                        "prompt_tokens": t.prompt_tokens,
                        "completion_tokens": t.completion_tokens,
                    },
                )
            else:
                yield sse(
                    "error", {"message_id": message_id, "code": t.error_code, "message": t.error}
                )
        finally:
            if not producer.done():
                producer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await producer
            if self._persisted is None:
                if self.turn.status == "running":
                    self.turn.status = "cancelled"
                await asyncio.shield(self._persist())


class ChatStreamingResponse(StreamingResponse):
    """Always closes the event generator and releases the ticket, however the response ends
    (completion, client disconnect seen via receive() or via a failed send(), or shutdown)."""

    def __init__(self, stream: ChatStream, on_close: Callable[[], None]) -> None:
        self._gen = stream.events()
        self._on_close = on_close
        super().__init__(
            self._gen,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self._gen.aclose()
            finally:
                self._on_close()
