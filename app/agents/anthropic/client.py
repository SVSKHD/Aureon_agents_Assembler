"""Direct Anthropic Messages API adapter, on the official `anthropic` SDK (docs/26).

This is direct API inference with ANTHROPIC_API_KEY. It is unrelated to the Claude Code CLI
review (app/integrations/claude_cli.py, docs/22): neither credential grants the other.

What it sends and does not send:

- `system` is the joined system messages; `messages` the other turns (scrubbed first).
- `max_tokens` is required by the API: the caller's, else ANTHROPIC_MAX_TOKENS.
- No `temperature`: SDK 2.x sends `anthropic-version: 2026-10-21`, on which sampling
  parameters are rejected with a 400. Temperature is accepted and ignored here.
- No server-side refusal `fallbacks`: they would answer from another model that may not be
  on the allowlist or priced. A refusal raises `refused`, and chat falls back to local Ollama.
- SDK retries are bounded by AI_EXTERNAL_MAX_RETRIES (connection errors, 408/409/429/5xx,
  before the first byte only).

Cost is the operator's configured price x the reported tokens (ANTHROPIC_MODEL_PRICES);
Anthropic does not report a cost, and none is built in here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

import anthropic
import httpx2
from anthropic.types import MessageParam

from app.agents.providers.base import (
    ModelPrice,
    ProviderError,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    cost_usd,
    error_for_status,
    split_system,
    usage_dict,
)
from app.core.logging import scrub_messages

LABEL = "Anthropic"


class AnthropicRefused(ProviderRejected):
    """stop_reason "refusal": the model's safety classifiers declined the request."""

    code = "refused"


def _translate(exc: Exception) -> ProviderError:
    """A fixed-message ProviderError for any SDK failure (never the response body)."""
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeout(f"{LABEL} did not respond in time")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailable(f"{LABEL} is unreachable")
    if isinstance(exc, anthropic.APIStatusError):
        return error_for_status(LABEL, exc.status_code)
    return ProviderError(f"{LABEL} request failed ({type(exc).__name__})")


class AnthropicClient:
    provider = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout: float,
        max_retries: int,
        default_max_tokens: int,
        prices: Mapping[str, ModelPrice] | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._prices = dict(prices or {})
        self._default_max_tokens = default_max_tokens
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=anthropic.Timeout(timeout, connect=min(timeout, 10.0)),
            max_retries=max_retries,
            http_client=http_client,
        )

    def __repr__(self) -> str:  # never the key
        return f"AnthropicClient(base_url={str(self._client.base_url)!r})"

    async def aclose(self) -> None:
        await self._client.close()

    async def list_models(self) -> list[str]:
        """Model ids this key can use (GET /v1/models; no tokens are spent)."""
        try:
            return sorted([m.id async for m in self._client.models.list(limit=1000)])
        except anthropic.AnthropicError as exc:
            raise _translate(exc) from None

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,  # ignored: rejected on API version 2026-10-21
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield `{"content": str}` pieces, then one `{"done": True, "usage", "model"}`.

        Closing the generator closes the HTTP stream. Raises ProviderError on any failure.
        """
        system, turns = split_system(scrub_messages(messages))
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or self._default_max_tokens,
            "messages": cast(list[MessageParam], turns),
            "stream": True,
        }
        if system:
            params["system"] = system
        prompt_tokens = completion_tokens = 0
        served = model
        stop_reason: str | None = None
        try:
            stream = await self._client.messages.create(**params)
            async with stream:
                async for event in stream:
                    if event.type == "message_start":
                        served = event.message.model or served
                        u = event.message.usage
                        prompt_tokens = (
                            (u.input_tokens or 0)
                            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
                            + (getattr(u, "cache_read_input_tokens", 0) or 0)
                        )
                        completion_tokens = u.output_tokens or 0
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta" and event.delta.text:
                            yield {"content": event.delta.text}
                    elif event.type == "message_delta":
                        completion_tokens = event.usage.output_tokens or completion_tokens
                        stop_reason = event.delta.stop_reason
        except anthropic.AnthropicError as exc:
            raise _translate(exc) from None
        if stop_reason == "refusal":
            raise AnthropicRefused(f"{LABEL} declined the request (refusal)")
        cost = cost_usd(self._prices.get(model), prompt_tokens, completion_tokens)
        yield {
            "done": True,
            "usage": usage_dict(prompt_tokens, completion_tokens, cost),
            "model": served,
            "stop_reason": stop_reason,
        }


__all__ = ["AnthropicClient", "AnthropicRefused"]
