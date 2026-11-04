"""Direct Google Gemini API adapter, on the official `google-genai` SDK (docs/26).

The Gemini Developer API with GEMINI_API_KEY (sent as the `x-goog-api-key` header by the
SDK, never in the URL). Vertex AI is not used.

- System messages become `system_instruction`; assistant turns use Gemini's `model` role.
- The model id is validated before use (it becomes part of the request path).
- Thought parts (thinking models) are never streamed to the user; thinking tokens are
  billed as output, so they are counted in `completion_tokens`.
- A prompt blocked by Gemini's safety filters (`prompt_feedback.block_reason`, or a
  SAFETY/PROHIBITED_CONTENT/BLOCKLIST finish) raises `refused`; chat falls back to local.
- SDK retries are bounded by AI_EXTERNAL_MAX_RETRIES.

Cost is the operator's configured price x the reported tokens (GEMINI_MODEL_PRICES).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

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

LABEL = "Gemini"
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_BLOCKED_FINISH = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"})


class GeminiRefused(ProviderRejected):
    code = "refused"


def normalize_model(model: str) -> str:
    """`gemini-x` from `gemini-x` or `models/gemini-x`; ProviderRejected if malformed."""
    name = model.strip().removeprefix("models/")
    if not MODEL_ID.fullmatch(name):
        raise ProviderRejected(f"{LABEL} model id is not valid")
    return name


def _translate(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeout(f"{LABEL} did not respond in time")
    if isinstance(exc, httpx.TransportError):
        return ProviderUnavailable(f"{LABEL} is unreachable")
    if isinstance(exc, genai_errors.APIError):
        code = exc.code if isinstance(exc.code, int) else 502
        return error_for_status(LABEL, code)
    return ProviderError(f"{LABEL} request failed ({type(exc).__name__})")


def _contents(turns: list[dict[str, str]]) -> list[types.Content]:
    return [
        types.Content(
            role="model" if m["role"] == "assistant" else "user",
            parts=[types.Part(text=m["content"])],
        )
        for m in turns
    ]


class GeminiClient:
    provider = "gemini"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        timeout: float,
        max_retries: int,
        default_max_tokens: int,
        prices: Mapping[str, ModelPrice] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._prices = dict(prices or {})
        self._default_max_tokens = default_max_tokens
        self._base_url = base_url.rstrip("/")
        self._own_http = http_client
        options = types.HttpOptions(
            base_url=self._base_url,
            timeout=int(timeout * 1000),
            retry_options=types.HttpRetryOptions(attempts=max_retries + 1),
            httpx_async_client=http_client,
        )
        self._client = genai.Client(api_key=api_key, http_options=options)

    def __repr__(self) -> str:  # never the key
        return f"GeminiClient(base_url={self._base_url!r})"

    async def aclose(self) -> None:
        try:
            await self._client.aio.aclose()
        finally:
            if self._own_http is not None:
                await self._own_http.aclose()

    async def list_models(self) -> list[str]:
        """Model ids that support generateContent (GET models; no tokens are spent)."""
        try:
            pager = await self._client.aio.models.list(config={"page_size": 1000})
            names = [
                (m.name or "").removeprefix("models/")
                async for m in pager
                if not m.supported_actions or "generateContent" in m.supported_actions
            ]
        except Exception as exc:
            raise _translate(exc) from None
        return sorted(n for n in names if n)

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        name = normalize_model(model)
        system, turns = split_system(scrub_messages(messages))
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            temperature=temperature,
            max_output_tokens=max_tokens or self._default_max_tokens,
        )
        prompt_tokens = completion_tokens = 0
        served = name
        finish: str | None = None
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=name, contents=_contents(turns), config=config
            )
            async for chunk in stream:
                feedback = chunk.prompt_feedback
                if feedback is not None and feedback.block_reason:
                    raise GeminiRefused(f"{LABEL} blocked the prompt")
                served = chunk.model_version or served
                u = chunk.usage_metadata
                if u is not None:
                    prompt_tokens = u.prompt_token_count or prompt_tokens
                    completion_tokens = (u.candidates_token_count or 0) + (
                        u.thoughts_token_count or 0
                    ) or completion_tokens
                for cand in chunk.candidates or []:
                    if cand.finish_reason is not None:
                        finish = str(getattr(cand.finish_reason, "value", cand.finish_reason))
                    for part in (cand.content.parts if cand.content else None) or []:
                        if part.text and not part.thought:
                            yield {"content": part.text}
        except Exception as exc:
            raise _translate(exc) from None
        if finish in _BLOCKED_FINISH:
            raise GeminiRefused(f"{LABEL} stopped the answer ({finish})")
        cost = cost_usd(self._prices.get(name), prompt_tokens, completion_tokens)
        yield {
            "done": True,
            "usage": usage_dict(prompt_tokens, completion_tokens, cost),
            "model": served,
            "stop_reason": finish,
        }


__all__ = ["GeminiClient", "GeminiRefused", "normalize_model"]
