"""Which provider answers a chat turn: an external provider (opt-in) or local Ollama (default).

docs/25-openrouter.md, docs/26-ai-providers.md. `ExternalGate.decide` is the single place
that says yes to an external call, for every provider (OpenRouter, Anthropic, Gemini). It
is asked before every external request and says no (with a reason code the SPA shows) when
any of these hold, in this order:

    disabled | not_configured | no_budget      settings (Settings.external_unusable_reason)
    model_not_allowed                          not the provider's default or in its allowlist
    no_pricing                                 Anthropic/Gemini model without a configured price
    context | filter                           app/services/chat_filter.py on the full payload
    daily_cap_reached | usage_unavailable      app/services/ai_usage.py pre-check (fail closed)
    rate_limited_local                         AI_EXTERNAL_REQUESTS_PER_MINUTE for the provider
    daily_cap_reached | global_cap_reached     the call's worst case reserved against the caps

A "no" moves on to the next provider in the chain (`Settings.external_chain`: the requested
or default provider, then AI_EXTERNAL_FALLBACK_PROVIDERS), and to local Ollama
(CHAT_DEFAULT_MODEL, else the first installed model) after the last one (`choose`). Errors
during the call (rate_limited, upstream_error, timeout, unavailable, rejected, refused) move
on the same way, from app/services/chat.py. A "no" for `filter` or `context` ends the walk:
that payload goes to no external provider, only local. With an empty fallback list the only
fallback is local (the behaviour before docs/30).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from app.agents.openrouter.client import OpenRouterClient
from app.agents.providers.base import EXTERNAL_PROVIDERS, ExternalChatClient, ModelPrice, cost_usd
from app.core.config import Settings
from app.services.ai_usage import UsageTracker
from app.services.chat_filter import classify_chat

Provider = Literal["ollama", "openrouter", "anthropic", "gemini"]
PROVIDERS: tuple[Provider, ...] = ("ollama", "openrouter", "anthropic", "gemini")
# Reasons that mean "the data had to stay local" (the SPA's "local only" badge).
LOCAL_ONLY_REASONS = frozenset({"filter", "context"})
# A rough upper bound for the prompt's tokens when reserving budget (no tokenizer locally).
CHARS_PER_TOKEN = 3


@dataclass(slots=True)
class Decision:
    external: bool
    reason: str | None = None
    categories: list[str] = field(default_factory=list)
    provider: str = "openrouter"
    # Budget held for this call (micro-USD); released by UsageTracker.record.
    reserved_micro: int = 0

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


def _prices(settings: Settings, provider: str) -> dict[str, ModelPrice]:
    return {m: ModelPrice(i, o) for m, (i, o) in settings.external(provider).prices.items()}


def build_external(
    settings: Settings, provider: str, *, http_client: Any = None
) -> ExternalChatClient | None:
    """The provider's client when it is enabled and keyed; None otherwise.

    `http_client` is for tests (an httpx2.AsyncClient for Anthropic, httpx for Gemini)."""
    if provider == "openrouter":
        return build_openrouter(settings)
    cfg = settings.external(provider)
    if not cfg.enabled or not cfg.api_key:
        return None
    common: dict[str, Any] = {
        "base_url": cfg.base_url,
        "timeout": cfg.timeout,
        "max_retries": settings.AI_EXTERNAL_MAX_RETRIES,
        "default_max_tokens": settings.AI_EXTERNAL_MAX_OUTPUT_TOKENS,
        "prices": _prices(settings, provider),
    }
    if http_client is not None:
        common["http_client"] = http_client
    if provider == "anthropic":
        from app.agents.anthropic.client import AnthropicClient

        return AnthropicClient(cfg.api_key, **common)
    if provider == "gemini":
        from app.agents.gemini.client import GeminiClient

        return GeminiClient(cfg.api_key, **common)
    raise KeyError(provider)


def external_clients(state: Any) -> dict[str, ExternalChatClient]:
    """The API's current external clients by provider (they change on a settings reload)."""
    out: dict[str, ExternalChatClient] = {}
    for p in EXTERNAL_PROVIDERS:
        client = getattr(state, p, None)
        if client is not None:
            out[p] = client
    return out


def estimate_cost(
    settings: Settings,
    provider: str,
    model: str,
    messages: Iterable[Mapping[str, str]],
    max_tokens: int | None,
) -> Decimal | None:
    """The call's worst case at the configured price (None for OpenRouter: it reports cost)."""
    cfg = settings.external(provider)
    if cfg.reports_cost:
        return None
    price = cfg.prices.get(model.removeprefix("models/"))
    if price is None:
        return None
    chars = sum(len(m.get("content", "")) for m in messages)
    prompt = chars // CHARS_PER_TOKEN + 16
    output = max_tokens or settings.AI_EXTERNAL_MAX_OUTPUT_TOKENS
    return cost_usd(ModelPrice(*price), prompt, output)


class ExternalGate:
    def __init__(
        self,
        settings: Settings,
        client: ExternalChatClient | None,
        usage: UsageTracker | None,
        *,
        provider: str = "openrouter",
    ) -> None:
        self.settings = settings
        self.client = client
        self.provider = provider
        self.usage = usage.for_provider(provider) if usage is not None else None

    def unusable_reason(self) -> str | None:
        reason = self.settings.external_unusable_reason(self.provider)
        if reason is None and (self.client is None or self.usage is None):
            return "not_configured"
        return reason

    @property
    def usable(self) -> bool:
        return self.unusable_reason() is None

    @property
    def models(self) -> list[str]:
        return list(self.settings.external(self.provider).models)

    @property
    def default_model(self) -> str:
        return self.settings.external(self.provider).default_model

    def allowed(self, model: str) -> bool:
        return model in self.settings.external(self.provider).models

    def priced(self, model: str) -> bool:
        return self.settings.external(self.provider).priced(model)

    async def decide(
        self,
        model: str,
        messages: Iterable[Mapping[str, str]],
        *,
        context: str = "general",
        max_tokens: int | None = None,
    ) -> Decision:
        msgs = list(messages)

        def no(reason: str | None, cats: Iterable[str] = ()) -> Decision:
            return Decision(False, reason, list(cats), self.provider)

        if reason := self.unusable_reason():
            return no(reason)
        if not self.allowed(model):
            return no("model_not_allowed")
        if not self.priced(model):
            return no("no_pricing")
        verdict = classify_chat(
            msgs,
            context=context,
            research_external_allowed=self.settings.AI_RESEARCH_EXTERNAL_ALLOWED,
        )
        if not verdict.external_ok:
            return no(verdict.reason, verdict.categories)
        cfg = self.settings.external(self.provider)
        assert self.usage is not None
        if reason := await self.usage.precheck(cfg.daily_limit_usd):
            return no(reason)
        if reason := await self.usage.minute_check(self.settings.AI_EXTERNAL_REQUESTS_PER_MINUTE):
            return no(reason)
        estimate = estimate_cost(self.settings, self.provider, model, msgs, max_tokens)
        reason, held = await self.usage.reserve(
            cfg.daily_limit_usd, estimate, self.settings.AI_EXTERNAL_DAILY_LIMIT_USD
        )
        if reason:
            return no(reason)
        return Decision(True, provider=self.provider, reserved_micro=held)

    async def release(self, decision: Decision) -> None:
        """Give back a reservation for a call that will not be made after all."""
        if self.usage is not None and decision.reserved_micro:
            await self.usage.release(decision.reserved_micro)


def gate_for(
    settings: Settings, provider: str, clients: Mapping[str, Any], usage: UsageTracker | None
) -> ExternalGate:
    return ExternalGate(settings, clients.get(provider), usage, provider=provider)


@dataclass(slots=True)
class Pick:
    """The result of walking the provider chain (see `choose`)."""

    # The provider that said yes, and its model; None = no external provider (use local).
    gate: ExternalGate | None
    model: str
    # The yes (with its reservation), else the first refusal (its reason and categories).
    decision: Decision
    # Providers still untried after `gate`, for a runtime failure.
    remaining: tuple[str, ...] = ()
    # Why the first provider in the chain was skipped (None when it said yes).
    skipped_reason: str | None = None


async def choose(
    gates: Mapping[str, ExternalGate],
    chain: Sequence[str],
    model: str,
    messages: Iterable[Mapping[str, str]],
    *,
    context: str = "general",
    max_tokens: int | None = None,
) -> Pick:
    """Ask each provider's gate in order; the first yes wins.

    `model` is for the first provider; the others use their default model. A refusal for a
    local-only reason (the classification filter) ends the walk at once: the verdict does
    not depend on the provider, and that data goes to no external provider. Any other
    refusal (disabled, not configured, unpriced, capped, rate-limited) moves on."""
    msgs = list(messages)
    first: Decision | None = None
    for i, provider in enumerate(chain):
        gate = gates.get(provider)
        if gate is None:
            continue
        m = model if i == 0 else gate.default_model
        decision = await gate.decide(m, msgs, context=context, max_tokens=max_tokens)
        if decision.external:
            reason = first.reason if first is not None else None
            return Pick(gate, m, decision, tuple(chain[i + 1 :]), reason)
        if first is None:
            first = decision
        if decision.local_only:
            break
    refusal = first if first is not None else Decision(False)
    return Pick(None, model, refusal, (), refusal.reason)


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
    "Pick",
    "Provider",
    "build_external",
    "build_openrouter",
    "choose",
    "estimate_cost",
    "external_clients",
    "gate_for",
    "local_fallback_model",
]
