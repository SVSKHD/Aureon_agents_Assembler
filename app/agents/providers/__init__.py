"""External chat providers behind one interface (docs/26-ai-providers.md).

Every adapter (OpenRouter, Anthropic, Gemini) implements `ExternalChatClient`: it streams
`{"content": str}` pieces and ends with one `{"done": True, "usage": {...}, "model": str}`,
and raises `ProviderError` (whose `code` is the chat's fallback reason) on any failure.
Nothing here decides whether a call may happen: that is `app/services/inference.py`.
"""

from app.agents.providers.base import (
    EXTERNAL_PROVIDERS,
    PROVIDER_LABELS,
    ExternalChatClient,
    ExternalProvider,
    ModelPrice,
    ProviderError,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
    cost_usd,
    error_for_status,
    usage_dict,
)

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
    "usage_dict",
]
