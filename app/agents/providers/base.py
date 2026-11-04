"""The provider-neutral contract every external chat adapter follows.

Rules every adapter keeps (the OpenRouter client set them first):

- The API key lives only inside the SDK/HTTP client that sends it: never in `repr`, an
  exception message, a log line or a stored error.
- Error messages are fixed strings built from the HTTP status, never the upstream body (it
  could echo the request), so nothing user-supplied or secret ends up in a stored error.
- The stream ends with one `{"done": True, "usage": usage_dict(...), "model": ...}`; the
  usage's `cost_usd` is the provider's reported cost (OpenRouter) or tokens x the operator's
  configured price (Anthropic, Gemini), else None.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

ExternalProvider = Literal["openrouter", "anthropic", "gemini"]
EXTERNAL_PROVIDERS: tuple[ExternalProvider, ...] = ("openrouter", "anthropic", "gemini")
PROVIDER_LABELS: dict[str, str] = {
    "ollama": "Local (Ollama)",
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "gemini": "Google Gemini",
}
MILLION = Decimal(1_000_000)


class ProviderError(Exception):
    """An external provider could not answer. `code` doubles as the fallback reason."""

    code = "upstream_error"

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class ProviderRateLimited(ProviderError):
    code = "rate_limited"


class ProviderTimeout(ProviderError):
    code = "timeout"


class ProviderUnavailable(ProviderError):
    code = "unavailable"


class ProviderRejected(ProviderError):
    """4xx other than 429: bad or expired key, no credits, unknown model, refused content."""

    code = "rejected"


def error_for_status(label: str, status: int) -> ProviderError:
    """A fixed-message error for an HTTP status (never the response body)."""
    if status == 429:
        return ProviderRateLimited(f"{label} rate limit (HTTP 429)", status)
    if status == 529:  # Anthropic: overloaded
        return ProviderUnavailable(f"{label} is overloaded (HTTP 529)", status)
    if status >= 500:
        return ProviderError(f"{label} returned HTTP {status}", status)
    if status == 402:
        return ProviderRejected(f"{label} account has insufficient credits (HTTP 402)", status)
    if status in (401, 403):
        return ProviderRejected(f"{label} refused the credentials (HTTP {status})", status)
    if status == 404:
        return ProviderRejected(f"{label} does not know this model (HTTP 404)", status)
    return ProviderRejected(f"{label} rejected the request (HTTP {status})", status)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens, as configured by the operator (never built in)."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


def cost_usd(
    price: ModelPrice | None, prompt_tokens: int, completion_tokens: int
) -> Decimal | None:
    if price is None:
        return None
    return (
        Decimal(prompt_tokens) * price.input_per_mtok
        + Decimal(completion_tokens) * price.output_per_mtok
    ) / MILLION


def usage_dict(prompt_tokens: int, completion_tokens: int, cost: Decimal | None) -> dict[str, Any]:
    return {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "cost_usd": cost,
    }


class ExternalChatClient(Protocol):
    """What chat, the model router and the research worker need from a provider."""

    @property
    def provider(self) -> str: ...

    def chat_stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def list_models(self) -> list[str]: ...

    async def aclose(self) -> None: ...


def split_system(
    messages: list[dict[str, str]] | list[Mapping[str, str]],
) -> tuple[str, list[dict[str, str]]]:
    """(the system messages joined, the other turns in order) for APIs that take the
    system prompt separately (Anthropic `system`, Gemini `systemInstruction`)."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [
        {"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"
    ]
    return system, turns


__all__ = [
    "EXTERNAL_PROVIDERS",
    "PROVIDER_LABELS",
    "ExternalChatClient",
    "ExternalProvider",
    "ModelPrice",
    "ProviderError",
    "ProviderRateLimited",
    "ProviderRejected",
    "ProviderTimeout",
    "ProviderUnavailable",
    "cost_usd",
    "error_for_status",
    "split_system",
    "usage_dict",
]
