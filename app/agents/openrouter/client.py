"""HTTPX client for OpenRouter's OpenAI-compatible chat API (docs/25-openrouter.md).

Only `app/services/chat.py` and the research worker call it, and only after the
classification filter, the daily budget and the settings have said yes. Rules kept here:

- The API key is a SecretStr. It is put on each request's Authorization header and nowhere
  else: never on the client's default headers, in an exception message, or in a log line.
- Error messages are fixed strings built from the HTTP status, never the upstream body (it
  could echo the request), so nothing user-supplied or secret ends up in a stored error.
- `usage: {include: true}` is always requested, so the final chunk carries the token counts
  and the cost in USD that the daily cap is charged with.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import SecretStr

from app.core.logging import scrub_messages


class OpenRouterError(Exception):
    """OpenRouter could not answer. `code` doubles as the chat's fallback reason."""

    code = "upstream_error"

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class OpenRouterRateLimited(OpenRouterError):
    code = "rate_limited"


class OpenRouterTimeout(OpenRouterError):
    code = "timeout"


class OpenRouterUnavailable(OpenRouterError):
    code = "unavailable"


class OpenRouterRejected(OpenRouterError):
    """4xx other than 429: bad key, no credits, unknown model, refused content."""

    code = "rejected"


def _for_status(status: int) -> OpenRouterError:
    if status == 429:
        return OpenRouterRateLimited("OpenRouter rate limit (HTTP 429)", status)
    if status >= 500:
        return OpenRouterError(f"OpenRouter returned HTTP {status}", status)
    if status == 402:
        return OpenRouterRejected("OpenRouter account has insufficient credits (HTTP 402)", status)
    if status in (401, 403):
        return OpenRouterRejected(f"OpenRouter refused the credentials (HTTP {status})", status)
    return OpenRouterRejected(f"OpenRouter rejected the request (HTTP {status})", status)


def parse_usage(raw: Any) -> dict[str, Any]:
    """{prompt_tokens, completion_tokens, cost_usd: Decimal | None} from an OpenRouter
    `usage` object; missing fields are 0 / None."""
    raw = raw if isinstance(raw, dict) else {}
    cost: Decimal | None
    try:
        cost = Decimal(str(raw["cost"])) if raw.get("cost") is not None else None
    except (InvalidOperation, ValueError):
        cost = None
    if cost is not None and (not cost.is_finite() or cost < 0):
        cost = None
    return {
        "prompt_tokens": int(raw.get("prompt_tokens") or 0),
        "completion_tokens": int(raw.get("completion_tokens") or 0),
        "cost_usd": cost,
    }


class OpenRouterClient:
    def __init__(
        self,
        base_url: str,
        api_key: SecretStr,
        *,
        timeout: float,
        app_title: str = "Aureon",
        referer: str = "https://spasta.online",
    ) -> None:
        self._key = api_key
        self._extra = {"X-Title": app_title, "HTTP-Referer": referer}
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(connect=timeout, read=timeout, write=30.0, pool=timeout),
        )

    def __repr__(self) -> str:  # never the key
        return f"OpenRouterClient(base_url={str(self._http.base_url)!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key.get_secret_value()}",
            "Accept": "text/event-stream",
            **self._extra,
        }

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield `{"content": str}` pieces, then one `{"done": True, "usage": {...}}`.

        Closing the generator closes the HTTP stream (OpenRouter stops billing the rest).
        Raises OpenRouterError (or a subclass) on any failure.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": scrub_messages(messages),
            "stream": True,
            "usage": {"include": True},
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        usage: dict[str, Any] = parse_usage(None)
        served_model = model
        try:
            async with self._http.stream(
                "POST", "/chat/completions", json=payload, headers=self._headers()
            ) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise _for_status(resp.status_code)
                async for line in resp.aiter_lines():
                    line = line.strip()
                    # ": OPENROUTER PROCESSING" keep-alives and blank separators
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk: dict[str, Any] = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        err = chunk["error"] if isinstance(chunk["error"], dict) else {}
                        code = err.get("code")
                        raise _for_status(code if isinstance(code, int) else 502)
                    served_model = str(chunk.get("model") or served_model)
                    if chunk.get("usage"):
                        usage = parse_usage(chunk["usage"])
                    for choice in chunk.get("choices") or []:
                        piece = (choice.get("delta") or {}).get("content") or ""
                        if piece:
                            yield {"content": piece}
                        if choice.get("finish_reason") == "error":
                            raise OpenRouterError("OpenRouter ended the stream with an error")
        except httpx.TimeoutException as exc:
            raise OpenRouterTimeout("OpenRouter did not respond in time") from exc
        except httpx.TransportError as exc:
            raise OpenRouterUnavailable("OpenRouter is unreachable") from exc
        yield {"done": True, "usage": usage, "model": served_model}


__all__ = [
    "OpenRouterClient",
    "OpenRouterError",
    "OpenRouterRateLimited",
    "OpenRouterRejected",
    "OpenRouterTimeout",
    "OpenRouterUnavailable",
    "parse_usage",
]
