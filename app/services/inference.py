"""Which provider answers a chat turn: OpenRouter (opt-in) or local Ollama (default).

docs/25-openrouter.md. `ExternalGate.decide` is the single place that says yes to an
external call. It is asked before every OpenRouter request and says no (with a reason code
the SPA shows) when any of these hold, in this order:

    disabled | not_configured | no_budget      settings (Settings.openrouter_unusable_reason)
    model_not_allowed                          the model is not the default or in the allowlist
    context | filter                           app/services/chat_filter.py on the full payload
    daily_cap_reached | usage_unavailable      app/services/ai_usage.py pre-check

A "no" sends the turn to local Ollama (CHAT_DEFAULT_MODEL, else the first installed model).
Errors during the call (rate_limited, upstream_error, timeout, unavailable, rejected) fall
back the same way, from app/services/chat.py.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from app.agents.openrouter.client import OpenRouterClient
from app.core.config import Settings
from app.services.ai_usage import UsageTracker
from app.services.chat_filter import classify_chat

Provider = Literal["ollama", "openrouter"]
PROVIDERS: tuple[Provider, ...] = ("ollama", "openrouter")
# Reasons that mean "the data had to stay local" (the SPA's "local only" badge).
LOCAL_ONLY_REASONS = frozenset({"filter", "context"})


@dataclass(slots=True)
class Decision:
    external: bool
    reason: str | None = None
    categories: list[str] = field(default_factory=list)

    @property
    def local_only(self) -> bool:
        return self.reason in LOCAL_ONLY_REASONS


def build_openrouter(settings: Settings) -> OpenRouterClient | None:
    """A client only when enabled and a key is set; None otherwise (nothing to call)."""
    if not settings.OPENROUTER_ENABLED or not settings.openrouter_key:
        return None
    assert settings.OPENROUTER_API_KEY is not None
    return OpenRouterClient(
        settings.OPENROUTER_BASE_URL,
        settings.OPENROUTER_API_KEY,
        timeout=settings.OPENROUTER_TIMEOUT_SECONDS,
    )


class ExternalGate:
    def __init__(
        self,
        settings: Settings,
        client: OpenRouterClient | None,
        usage: UsageTracker | None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.usage = usage

    def unusable_reason(self) -> str | None:
        reason = self.settings.openrouter_unusable_reason()
        if reason is None and (self.client is None or self.usage is None):
            return "not_configured"
        return reason

    @property
    def usable(self) -> bool:
        return self.unusable_reason() is None

    def allowed(self, model: str) -> bool:
        return model in self.settings.openrouter_models

    async def decide(
        self,
        model: str,
        messages: Iterable[Mapping[str, str]],
        *,
        context: str = "general",
    ) -> Decision:
        if reason := self.unusable_reason():
            return Decision(False, reason)
        if not self.allowed(model):
            return Decision(False, "model_not_allowed")
        verdict = classify_chat(
            messages,
            context=context,
            research_external_allowed=self.settings.AI_RESEARCH_EXTERNAL_ALLOWED,
        )
        if not verdict.external_ok:
            return Decision(False, verdict.reason, verdict.categories)
        assert self.usage is not None
        if reason := await self.usage.precheck(self.settings.OPENROUTER_DAILY_LIMIT_USD):
            return Decision(False, reason)
        return Decision(True)


def local_fallback_model(settings: Settings, installed: list[str] | None) -> str | None:
    """CHAT_DEFAULT_MODEL when installed (or when the list is unknown), else the first."""
    default = settings.CHAT_DEFAULT_MODEL.strip()
    if installed is None:
        return default or None
    if default and default in installed:
        return default
    return installed[0] if installed else None


__all__ = [
    "LOCAL_ONLY_REASONS",
    "PROVIDERS",
    "Decision",
    "ExternalGate",
    "Provider",
    "build_openrouter",
    "local_fallback_model",
]
