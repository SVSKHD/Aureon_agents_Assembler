"""The Claude gateway: the ONLY module that imports the `anthropic` SDK or talks to
api.anthropic.com (docs/26-ai-providers.md; tests/test_ai_gateway.py enforces it).

Every call, in order:

1. The model must be priced (Settings.claude_price), or the call is refused: no price, no
   budget control.
2. Redaction (app/ai/redact.py) on the system prompt, every text block and tool result.
3. The worst-case cost is reserved against the monthly and per-agent caps (app/ai/budget.py);
   a call that could cross either is refused with BudgetExceeded and nothing is sent.
4. The request: streaming; the tool definitions and the system prompt carry `cache_control`
   (they are fixed text, so they stay cached across turns), plus automatic caching of the
   conversation so each tool-loop step re-reads the history from the cache. Effort is
   explicit (AI_EFFORT): Claude Opus 5.5 always thinks, and effort is the control.
   No sampling parameters (rejected by the API version the SDK sends).
5. Retries: the SDK retries connection errors, 408/409/429 and 5xx with exponential backoff
   (AI_MAX_RETRIES), before the first byte. The whole call, retries included, is bounded by
   AI_TIMEOUT_SECONDS.
6. The reservation is settled with the real usage: input, output, cache read and cache write
   tokens, the cost in USD, latency, request id and stop reason. Never any content.

A refusal (`stop_reason == "refusal"`) is recorded and raised as Refused. No server-side
fallback to another model is requested: the answer must come from a priced, allowed model.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import anthropic
from anthropic.types import Message
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.budget import BudgetExceeded, PgUsageStore, Settlement, UsageStore
from app.ai.pricing import NotPriced, TokenUsage, cost_usd, worst_case_usd
from app.ai.redact import Redactor
from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger("ai.gateway")

API_HOST = "api.anthropic.com"
CACHE = {"type": "ephemeral"}

OnText = Callable[[str], Awaitable[None]]


class GatewayError(Exception):
    code = "ai_error"
    status = 502


class NotConfigured(GatewayError):
    code = "ai_not_configured"
    status = 503


class ModelNotPriced(GatewayError):
    code = "model_not_priced"
    status = 503


class Refused(GatewayError):
    code = "refused"
    status = 422


class RateLimited(GatewayError):
    code = "rate_limited"
    status = 429


class Timeout(GatewayError):
    code = "timeout"
    status = 504


class AuthFailed(GatewayError):
    code = "ai_auth_failed"
    status = 503


class BudgetRefused(GatewayError):
    code = "budget_exceeded"
    status = 402


def _translate(exc: Exception) -> GatewayError:
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, TimeoutError | anthropic.APITimeoutError):
        return Timeout("Claude did not answer in time")
    if isinstance(exc, anthropic.RateLimitError):
        return RateLimited("Claude is rate-limiting this key; try again shortly")
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return AuthFailed("the Anthropic API key was refused")
    if isinstance(exc, anthropic.APIStatusError):
        err = GatewayError(f"Claude returned HTTP {exc.status_code}")
        err.code = "bad_request" if exc.status_code < 500 else "upstream_error"
        return err
    if isinstance(exc, anthropic.APIConnectionError):
        err = GatewayError("Claude could not be reached")
        err.code = "unreachable"
        return err
    return GatewayError(f"Claude call failed ({type(exc).__name__})")


def _usage(msg: Message) -> TokenUsage:
    u = msg.usage
    return TokenUsage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=u.cache_read_input_tokens or 0,
        cache_write_tokens=u.cache_creation_input_tokens or 0,
    )


def _with_cache(tools: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool definitions in a fixed order, the last one marked for caching."""
    out = [dict(t) for t in tools]
    if out:
        out[-1] = {**out[-1], "cache_control": CACHE}
    return out


@dataclass(slots=True)
class CallResult:
    message: Message
    cost_usd: Decimal
    usage_id: int

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.message.content if b.type == "text").strip()

    def blocks(self) -> list[dict[str, Any]]:
        """The content as plain dicts, exactly as received (thinking blocks included)."""
        return [b.model_dump(mode="json", exclude_none=True) for b in self.message.content]


class ClaudeGateway:
    def __init__(
        self,
        settings: Settings,
        store: UsageStore,
        *,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        key = settings.anthropic_key
        if client is not None:
            self._client: Any | None = client
        elif key:
            self._client = anthropic.AsyncAnthropic(
                api_key=key,
                max_retries=settings.AI_MAX_RETRIES,
                timeout=settings.AI_TIMEOUT_SECONDS,
            )
        else:
            self._client = None

    @classmethod
    def build(cls, settings: Settings, maker: async_sessionmaker[AsyncSession]) -> ClaudeGateway:
        return cls(settings, PgUsageStore(maker))

    def __repr__(self) -> str:  # never the key
        return f"ClaudeGateway(configured={self.configured})"

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()

    async def probe(self) -> list[str]:
        """The model ids this key can use (GET /v1/models; spends no tokens)."""
        if self._client is None:
            raise NotConfigured("ANTHROPIC_API_KEY is not set")
        try:
            return sorted([m.id async for m in self._client.models.list()])
        except Exception as exc:
            raise _translate(exc) from None

    async def call(
        self,
        *,
        agent: str,
        budget: str,
        system: str,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] = (),
        model: str | None = None,
        max_tokens: int | None = None,
        on_text: OnText | None = None,
        conversation_id: object | None = None,
        redactor: Redactor | None = None,
    ) -> CallResult:
        if self._client is None:
            raise NotConfigured("Claude is not set up: ANTHROPIC_API_KEY is not set")
        s = self.settings
        model = model or s.AI_MODEL_MAIN
        max_tokens = max_tokens or s.AI_MAX_OUTPUT_TOKENS
        red = redactor or Redactor()
        sent = red.messages(messages)
        system_text = red.text(system)
        tool_defs = _with_cache(tools)
        prompt_chars = len(system_text) + len(json.dumps(sent, default=str))
        prompt_chars += len(json.dumps(tool_defs, default=str))
        try:
            estimate = worst_case_usd(s, model, prompt_chars, max_tokens)
        except NotPriced:
            raise ModelNotPriced(f"{model} has no price (AI_MODEL_PRICES): never called") from None
        try:
            rid = await self.store.reserve(
                s,
                agent=agent,
                budget=budget,
                model=model,
                estimate=estimate,
                conversation_id=conversation_id,
            )
        except BudgetExceeded as exc:
            log.warning("ai_budget_refused", agent=agent, budget=budget, cap=exc.cap)
            raise BudgetRefused(str(exc)) from None

        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system_text, "cache_control": CACHE}],
            "messages": sent,
            "cache_control": CACHE,
            "output_config": {"effort": s.AI_EFFORT},
        }
        if tool_defs:
            params["tools"] = tool_defs
        t0 = time.monotonic()
        settled = Settlement(outcome="error")
        refused = False
        try:
            async with asyncio.timeout(s.AI_TIMEOUT_SECONDS):
                async with self._client.messages.stream(**params) as stream:
                    if on_text is not None:
                        async for piece in stream.text_stream:
                            await on_text(piece)
                    msg: Message = await stream.get_final_message()
            usage = _usage(msg)
            cost = cost_usd(s, model, usage)
            refused = msg.stop_reason == "refusal"
            settled = Settlement(
                outcome="refused" if refused else "ok",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cost_usd=cost,
                request_id=getattr(msg, "_request_id", None),
                stop_reason=msg.stop_reason,
            )
        except Exception as exc:
            err = _translate(exc)
            settled.error = err.code
            raise err from None
        finally:
            settled.latency_ms = int((time.monotonic() - t0) * 1000)
            try:
                await asyncio.shield(self.store.settle(rid, settled))
            except Exception as exc:  # the reservation keeps its worst case: safe side
                log.error("ai_usage_not_settled", error=type(exc).__name__)
            log.info(
                "ai_call",
                agent=agent,
                budget=budget,
                model=model,
                outcome=settled.outcome,
                input_tokens=settled.input_tokens,
                output_tokens=settled.output_tokens,
                cache_read_tokens=settled.cache_read_tokens,
                cache_write_tokens=settled.cache_write_tokens,
                cost_usd=str(settled.cost_usd),
                latency_ms=settled.latency_ms,
                request_id=settled.request_id,
                error=settled.error,
            )
        if refused:
            raise Refused("Claude declined this request")
        return CallResult(msg, cost, rid)


__all__ = [
    "API_HOST",
    "AuthFailed",
    "BudgetRefused",
    "CallResult",
    "ClaudeGateway",
    "GatewayError",
    "ModelNotPriced",
    "NotConfigured",
    "RateLimited",
    "Refused",
    "Timeout",
]
