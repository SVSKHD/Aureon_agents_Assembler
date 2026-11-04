"""Conversation storage (user-scoped) and the streaming chat pipeline.

Streaming design
----------------
A *producer* task talks to the model (inside the request timeout) and puts events on a queue.
With an external route (OpenRouter, Anthropic or Gemini; docs/25, docs/26) it tries that
provider first and, on any error, falls back to local Ollama in the same stream (a
`fallback` event says so). It never moves to another external provider.
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
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.agents.ollama.client import ModelInfo, OllamaClient, OllamaError
from app.agents.ollama.limiter import GenerationLimiter, QueueFull, Ticket
from app.agents.openrouter.client import OpenRouterClient
from app.agents.providers.base import (
    EXTERNAL_PROVIDERS,
    PROVIDER_LABELS,
    ExternalChatClient,
    ProviderError,
)
from app.agents.registry import AgentRegistry
from app.core.logging import get_logger
from app.models.ai import Conversation, Message, ModelInventory
from app.services import audit
from app.services.ai_usage import UsageTracker
from app.services.inference import LOCAL_ONLY_REASONS

log = get_logger(__name__)

TITLE_LEN = 80


def chat_options(num_ctx: int, num_thread: int) -> dict[str, Any]:
    """Ollama options every API chat (and the startup warm-up) uses. The warm-up must load
    the model with the same num_ctx/num_thread, or the first chat would reload it."""
    options: dict[str, Any] = {"num_ctx": num_ctx}
    if num_thread:
        options["num_thread"] = num_thread
    return options


def trim_history(history: list[dict[str, str]], max_messages: int) -> list[dict[str, str]]:
    """System messages plus the newest `max_messages` other turns, in their original order.
    0 keeps everything. The full conversation stays stored; only the prompt is shorter."""
    if max_messages <= 0:
        return history
    keep = {i for i, m in enumerate(history) if m["role"] != "system"}
    keep = set(sorted(keep)[-max_messages:])
    return [m for i, m in enumerate(history) if m["role"] == "system" or i in keep]


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

    async def history(self, conv: Conversation) -> list[dict[str, str]]:
        """Stored turns to re-send. Failed or cancelled assistant turns are kept for the
        record, not re-sent."""
        return [
            {"role": m.role, "content": m.content}
            for m in await self.messages(conv.id)
            if m.role != "assistant" or m.status == "complete"
        ]

    async def store_turn(
        self,
        conv: Conversation | None,
        new: list[dict[str, str]],
        *,
        model: str,
        provider: str = "ollama",
        context: str | None = None,
    ) -> tuple[Conversation, list[int]]:
        """Create the conversation if needed, record the picked provider/model (and a
        stricter context), store the new turns. Returns (conversation, new message ids)."""
        if conv is None:
            first_user = next(m["content"] for m in new if m["role"] == "user")
            conv = Conversation(
                user_id=self.user_id,
                title=" ".join(first_user.split())[:TITLE_LEN],
                model=model,
                provider=provider,
                context=context or "general",
            )
            self.db.add(conv)
            await self.db.flush()
        else:
            conv.model = model
            conv.provider = provider
            # A link only ever gets stricter: general -> finance/trading/research.
            if context and context != "general" and conv.context == "general":
                conv.context = context
        rows = [Message(conversation_id=conv.id, role=m["role"], content=m["content"]) for m in new]
        self.db.add_all(rows)
        conv.updated_at = func.now()
        await self.db.flush()
        return conv, [r.id for r in rows]

    async def start_turn(
        self,
        conversation_id: uuid.UUID | None,
        model: str,
        new: list[dict[str, str]],
        max_history: int = 0,
    ) -> tuple[Conversation, list[dict[str, str]], list[int]] | None:
        """Store the new turns and return (conversation, prompt, new message ids).
        The prompt re-sends at most `max_history` stored turns (plus system ones; 0 = all).

        Returns None if conversation_id is not one of this user's conversations.
        """
        conv: Conversation | None = None
        history: list[dict[str, str]] = []
        if conversation_id is not None:
            conv = await self.get(conversation_id)
            if conv is None:
                return None
            history = await self.history(conv)
        conv, ids = await self.store_turn(
            conv, new, model=model, provider=conv.provider if conv else "ollama"
        )
        return conv, trim_history(history, max_history) + new, ids


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
    # Who is answering now (changes on a fallback) and why the turn is not external.
    provider: str = "ollama"
    model: str = ""
    fallback_reason: str | None = None
    cost_usd: Decimal | None = None


@dataclass(slots=True)
class _External:
    """One external attempt, for the audit row and the daily counter. Never content."""

    model: str
    started: float
    provider: str = "openrouter"
    outcome: str = "running"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Decimal | None = None
    latency_ms: int | None = None
    recorded: bool = False


@dataclass(frozen=True, slots=True)
class ChatJob:
    conversation_id: uuid.UUID
    user_message_ids: list[int]
    model: str
    prompt: list[dict[str, str]]
    options: dict[str, Any]
    # Routing (docs/25-openrouter.md, docs/26). `provider`/`model` are the first attempt.
    provider: str = "ollama"
    requested_provider: str = "ollama"
    requested_model: str = ""
    # Local Ollama model used when the external provider is skipped or fails (None = none).
    fallback_model: str | None = None
    # Why the turn starts on Ollama although an external provider was requested.
    route_reason: str | None = None
    filter_categories: tuple[str, ...] = ()
    temperature: float | None = None
    max_tokens: int | None = None
    user_id: uuid.UUID | None = None
    client: audit.ClientInfo = field(default_factory=audit.ClientInfo)
    # Budget the gate reserved for the external call (released when it is recorded).
    reserved_micro: int = 0


class ChatStream:
    def __init__(
        self,
        job: ChatJob,
        *,
        ollama: OllamaClient,
        maker: async_sessionmaker[AsyncSession],
        ticket: Ticket | None,
        timeout: float,
        heartbeat: float,
        registry: AgentRegistry | None = None,
        limiter: GenerationLimiter | None = None,
        openrouter: OpenRouterClient | None = None,
        usage: UsageTracker | None = None,
        external: ExternalChatClient | None = None,
    ) -> None:
        self.registry = registry
        self.job = job
        self.ollama = ollama
        self.maker = maker
        # An external route admits to the Ollama limiter only if it has to fall back.
        self.ticket = ticket
        self.limiter = limiter
        # `openrouter=` is the original keyword; `external=` takes any provider's client.
        self.external_client: ExternalChatClient | None = (
            external if external is not None else openrouter
        )
        self.openrouter = self.external_client
        self.usage = (
            usage.for_provider(job.provider)
            if usage is not None and job.provider in EXTERNAL_PROVIDERS
            else usage
        )
        self.timeout = timeout
        self.heartbeat = heartbeat
        self.turn = _Turn(provider=job.provider, model=job.model, fallback_reason=job.route_reason)
        self.external: _External | None = None
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._started = time.monotonic()
        self._persisted: int | None = None

    def release(self) -> None:
        if self.ticket is not None:
            self.ticket.release()

    # --- producers ----------------------------------------------------------------------

    async def _produce(self) -> None:
        q, turn = self._queue, self.turn
        try:
            async with asyncio.timeout(self.timeout):
                if turn.provider in EXTERNAL_PROVIDERS:
                    if await self._produce_external():
                        turn.status = "complete"
                        return
                    if not self.job.fallback_model:
                        label = PROVIDER_LABELS.get(self.job.provider, self.job.provider)
                        turn.status, turn.error_code = "error", "fallback_unavailable"
                        turn.error = f"{label} failed and no local model is available"
                        return
                    turn.provider, turn.model = "ollama", self.job.fallback_model
                    await q.put(
                        (
                            "fallback",
                            {
                                "provider": "ollama",
                                "model": turn.model,
                                "reason": turn.fallback_reason,
                                "local_only": False,
                                "reset": True,
                            },
                        )
                    )
                await self._produce_ollama()
            turn.status = "complete"
        except TimeoutError:
            turn.status, turn.error_code = "timeout", "timeout"
            turn.error = f"generation exceeded {self.timeout:g}s"
        except OllamaError as exc:
            turn.status, turn.error_code, turn.error = "error", exc.code, exc.message
        except QueueFull:
            turn.status, turn.error_code = "error", "queue_full"
            turn.error = "the local generation queue is full"
        finally:
            # Free the Ollama slot as soon as generation ends (or is cancelled).
            if self.ticket is not None:
                self.ticket.release_slot()
            q.put_nowait(("end", None))

    async def _produce_ollama(self) -> None:
        q, turn = self._queue, self.turn
        if self.ticket is None:
            if self.limiter is None:
                raise QueueFull(0)
            self.ticket = self.limiter.admit()
        if not self.ticket.slot_free():
            await q.put(("queued", None))
        await self.ticket.acquire_slot()
        await q.put(("start", None))
        async for chunk in self.ollama.chat_stream(turn.model, self.job.prompt, self.job.options):
            piece = (chunk.get("message") or {}).get("content") or ""
            if piece:
                turn.parts.append(piece)
                await q.put(("token", piece))
            if chunk.get("done"):
                turn.prompt_tokens = chunk.get("prompt_eval_count")
                turn.completion_tokens = chunk.get("eval_count")
                break

    async def _produce_external(self) -> bool:
        """True when the external provider answered in full. False after any provider error,
        with turn.fallback_reason set and the partial text dropped (the SPA resets it)."""
        q, turn = self._queue, self.turn
        ext = self.external = _External(turn.model, time.monotonic(), turn.provider)
        client = self.external_client
        if client is None:
            turn.fallback_reason = ext.outcome = "not_configured"
            await self._record_external()
            return False
        await q.put(("start", None))
        try:
            async for chunk in client.chat_stream(
                turn.model,
                self.job.prompt,
                temperature=self.job.temperature,
                max_tokens=self.job.max_tokens,
            ):
                if chunk.get("done"):
                    u = chunk.get("usage") or {}
                    ext.prompt_tokens = int(u.get("prompt_tokens") or 0)
                    ext.completion_tokens = int(u.get("completion_tokens") or 0)
                    ext.cost_usd = u.get("cost_usd")
                    break
                piece = str(chunk.get("content") or "")
                if piece:
                    turn.parts.append(piece)
                    await q.put(("token", piece))
        except ProviderError as exc:
            ext.outcome = f"error:{exc.code}"
            turn.fallback_reason = exc.code
            turn.parts.clear()
            await self._record_external()
            log.warning(
                "external_fallback", provider=ext.provider, reason=exc.code, status=exc.status
            )
            return False
        ext.outcome = "ok"
        turn.prompt_tokens, turn.completion_tokens = ext.prompt_tokens, ext.completion_tokens
        turn.cost_usd = ext.cost_usd
        await self._record_external()
        return True

    # The original name (tests and callers from before docs/26).
    _produce_openrouter = _produce_external

    async def _record_external(self) -> None:
        ext = self.external
        if ext is None or ext.recorded:
            return
        ext.recorded = True
        ext.latency_ms = int((time.monotonic() - ext.started) * 1000)
        if self.usage is None:
            return
        if ext.outcome == "not_configured":
            await self.usage.release(self.job.reserved_micro)
            return
        await self.usage.record(
            cost_usd=ext.cost_usd,
            prompt_tokens=ext.prompt_tokens,
            completion_tokens=ext.completion_tokens,
            failed=ext.outcome != "ok",
            reserved_micro=self.job.reserved_micro,
        )

    # --- persistence --------------------------------------------------------------------

    async def _persist(self) -> int:
        if self._persisted is not None:
            return self._persisted
        t = self.turn
        ext = self.external
        if ext is not None and not ext.recorded:  # cancelled or timed out mid-call
            ext.outcome = "cancelled" if t.status in ("running", "cancelled") else t.status
            await self._record_external()
        async with self.maker() as db, db.begin():
            msg = Message(
                conversation_id=self.job.conversation_id,
                role="assistant",
                content="".join(t.parts),
                model=t.model,
                status=t.status,
                error=t.error,
                prompt_tokens=t.prompt_tokens,
                completion_tokens=t.completion_tokens,
                duration_ms=int((time.monotonic() - self._started) * 1000),
                provider=t.provider,
                fallback_reason=t.fallback_reason,
                cost_usd=t.cost_usd,
            )
            db.add(msg)
            await db.execute(
                update(Conversation)
                .where(Conversation.id == self.job.conversation_id)
                .values(updated_at=func.now())
            )
            if t.status == "complete" and t.provider == "ollama":
                await db.execute(
                    update(ModelInventory)
                    .where(ModelInventory.name == t.model)
                    .values(last_used_at=func.now())
                )
            await db.flush()
            if ext is not None and ext.outcome != "not_configured":
                # Metadata only: never the prompt or the answer.
                audit.record(
                    db,
                    "ai.external_call",
                    self.job.client,
                    actor_user_id=self.job.user_id,
                    details={
                        "provider": ext.provider,
                        "model": ext.model,
                        "conversation_id": str(self.job.conversation_id),
                        "message_id": msg.id,
                        "outcome": ext.outcome,
                        "prompt_tokens": ext.prompt_tokens,
                        "completion_tokens": ext.completion_tokens,
                        "cost_usd": str(ext.cost_usd) if ext.cost_usd is not None else None,
                        "latency_ms": ext.latency_ms,
                        "fell_back_to": t.model if t.provider == "ollama" else None,
                    },
                )
            self._persisted = msg.id
        log.info(
            "chat_turn",
            conversation_id=str(self.job.conversation_id),
            model=t.model,
            provider=t.provider,
            fallback_reason=t.fallback_reason,
            status=t.status,
            chars=sum(map(len, t.parts)),
        )
        if self.registry is not None and t.provider == "ollama":
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
                        "model": t.model,
                        "completion_tokens": t.completion_tokens,
                    },
                )
            except Exception as exc:
                log.warning("agent_run_record_failed", error=type(exc).__name__)
        return msg.id

    # --- the SSE stream -----------------------------------------------------------------

    def _route(self) -> dict[str, Any]:
        """Who is answering and why, on `meta`, `done` and `error`."""
        t = self.turn
        return {
            "provider": t.provider,
            "model": t.model,
            "requested_provider": self.job.requested_provider,
            "requested_model": self.job.requested_model or self.job.model,
            "fallback_reason": t.fallback_reason,
            "local_only": t.fallback_reason in LOCAL_ONLY_REASONS,
            "filter_categories": list(self.job.filter_categories),
        }

    async def events(self) -> AsyncGenerator[str, None]:
        producer = asyncio.create_task(self._produce())
        try:
            yield sse(
                "meta",
                {
                    "conversation_id": str(self.job.conversation_id),
                    "user_message_ids": self.job.user_message_ids,
                    **self._route(),
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
                elif kind == "fallback":
                    yield sse("fallback", data)
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
                        "cost_usd": str(t.cost_usd) if t.cost_usd is not None else None,
                        **self._route(),
                    },
                )
            else:
                yield sse(
                    "error",
                    {
                        "message_id": message_id,
                        "code": t.error_code,
                        "message": t.error,
                        **self._route(),
                    },
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

    def __init__(
        self,
        stream: ChatStream,
        on_close: Callable[[], None] | None = None,
        on_finish: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._gen = stream.events()
        self._on_close = on_close or stream.release
        # Async clean-up after the stream ends, e.g. clearing the interactive marker.
        self._on_finish = on_finish
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
                if self._on_finish is not None:
                    with contextlib.suppress(Exception):
                        await self._on_finish()
