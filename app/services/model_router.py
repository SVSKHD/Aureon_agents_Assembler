"""One way for an agent to ask a model (docs/model-routing.md).

    answer = await router.complete("news", messages, context="news")

Routing per call, in order:

1. The agent's settings (core.app_settings, /settings → Agents): disabled -> AgentDisabled;
   provider "ollama" -> local; "openrouter"/"anthropic"/"gemini" -> that provider; "auto" ->
   AI_DEFAULT_EXTERNAL_PROVIDER. Either way the existing ExternalGate decides
   (app/services/inference.py): provider enabled + key + budget, model allowed (and priced for
   the direct APIs), the data classification filter (app/services/chat_filter.py) on the full
   payload, the provider's daily cap (shared with chat) and the global cap. No second filter
   exists. A refusal or a failure moves to the next provider in AI_EXTERNAL_FALLBACK_PROVIDERS
   (docs/30); a local-only verdict goes to no external provider.
2. External: bounded by MODEL_ROUTER_EXTERNAL_TIMEOUT_SECONDS (10 s). An error, a timeout or
   the cap on every provider in the chain -> the same request goes to local Ollama, and the
   fallback is logged + recorded.
3. Local: the agent's local model (default qwen2.5:3b) through a bounded queue
   (MODEL_ROUTER_LOCAL_QUEUE). Full -> LocalBusy, and the caller degrades (the news agent
   uses its keyword rules). Market data, the feed and risk checks never call this.

Every attempt is a row in ai.model_calls: agent, provider, model, outcome, route reason,
fallback, local_only, tokens, latency, cost. Never a prompt or an answer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.ollama.client import OllamaClient, OllamaError
from app.agents.ollama.limiter import GenerationLimiter, QueueFull
from app.agents.openrouter.client import OpenRouterClient, parse_usage
from app.agents.providers.base import EXTERNAL_PROVIDERS, ExternalChatClient, ProviderError
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.model_calls import ModelCall
from app.services.ai_usage import UsageTracker
from app.services.inference import ExternalGate, choose, external_clients

log = get_logger("services.model_router")

AgentName = Literal["news", "voice", "research"]
AGENTS: tuple[AgentName, ...] = ("news", "voice", "research")


class RouterError(Exception):
    code = "unavailable"


class AgentDisabled(RouterError):
    code = "agent_disabled"


class LocalBusy(RouterError):
    code = "local_busy"


class ModelUnavailable(RouterError):
    code = "model_unavailable"


@dataclass(frozen=True, slots=True)
class AgentConfig:
    enabled: bool
    provider: str  # auto | ollama | openrouter | anthropic | gemini
    model: str  # the external model ("" = that provider's default model)
    local_model: str

    def external_provider(self, settings: Settings) -> str:
        """The provider an external attempt uses: the agent's own, else the default one."""
        if self.provider in EXTERNAL_PROVIDERS:
            return self.provider
        return settings.AI_DEFAULT_EXTERNAL_PROVIDER


def agent_config(settings: Settings, agent: AgentName) -> AgentConfig:
    up = agent.upper()
    return AgentConfig(
        bool(getattr(settings, f"AGENT_{up}_ENABLED")),
        str(getattr(settings, f"AGENT_{up}_PROVIDER")),
        str(getattr(settings, f"AGENT_{up}_MODEL") or "").strip(),
        str(getattr(settings, f"AGENT_{up}_LOCAL_MODEL") or "").strip(),
    )


@dataclass(slots=True)
class Answer:
    text: str
    provider: str
    model: str
    local_only: bool
    # Why it is not on the external provider (None when it is): filter | context |
    # disabled | daily_cap_reached | forced_local | timeout | rate_limited | ...
    route_reason: str | None
    fallback: bool


@dataclass(slots=True)
class _Call:
    agent: str
    provider: str
    model: str
    outcome: str
    route_reason: str | None = None
    fallback: bool = False
    local_only: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_usd: Decimal | None = None


class ModelRouter:
    def __init__(
        self,
        settings: Settings,
        *,
        ollama: OllamaClient,
        openrouter: OpenRouterClient | None,
        usage: UsageTracker | None,
        maker: async_sessionmaker[AsyncSession] | None,
        limiter: GenerationLimiter,
        external: Mapping[str, ExternalChatClient] | None = None,
    ) -> None:
        self.settings = settings
        self.ollama = ollama
        self.openrouter = openrouter
        # Every external client by provider; `openrouter=` stays the original keyword.
        self.clients: dict[str, ExternalChatClient] = dict(external or {})
        if openrouter is not None:
            self.clients.setdefault("openrouter", openrouter)
        self.usage = usage
        self.maker = maker
        self.limiter = limiter

    @classmethod
    def from_state(cls, state: Any) -> ModelRouter:
        """The API's router: the current settings and clients (they change on a reload)."""
        return cls(
            state.settings,
            ollama=state.ollama,
            openrouter=getattr(state, "openrouter", None),
            usage=getattr(state, "ai_usage", None),
            maker=state.sessionmaker,
            limiter=state.agent_limiter,
            external=external_clients(state),
        )

    async def complete(
        self,
        agent: AgentName,
        messages: Sequence[Mapping[str, str]],
        *,
        context: str = "general",
        max_tokens: int | None = 400,
        temperature: float = 0.2,
        force_local: bool = False,
        allow_local: bool = True,
        local_model: str | None = None,
    ) -> Answer:
        """`allow_local=False`: no local fallback at all (the caller degrades instead), so
        Qwen is never started for a caller that had no local model before. `local_model`
        overrides the agent's local model for this call."""
        cfg = agent_config(self.settings, agent)
        if local_model:
            cfg = AgentConfig(cfg.enabled, cfg.provider, cfg.model, local_model.strip())
        if not cfg.enabled:
            raise AgentDisabled(f"the {agent} agent is disabled")
        msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
        provider = cfg.external_provider(self.settings)
        external_model = cfg.model or self.settings.external(provider).default_model
        reason: str | None
        local_only = False
        fallback = False
        if force_local or cfg.provider == "ollama":
            reason = "forced_local"
        else:
            # The chain (docs/30): the agent's provider, then AI_EXTERNAL_FALLBACK_PROVIDERS.
            gates = {
                p: ExternalGate(self.settings, self.clients.get(p), self.usage, provider=p)
                for p in self.settings.external_chain(provider)
            }
            chain = list(gates)
            reason = None
            while chain:
                pick = await choose(
                    gates, chain, external_model, msgs, context=context, max_tokens=max_tokens
                )
                if pick.gate is None:
                    if reason is None:
                        reason, local_only = pick.decision.reason, pick.decision.local_only
                    break
                reason = reason or pick.skipped_reason
                try:
                    return await self._external(
                        agent,
                        pick.model,
                        msgs,
                        max_tokens,
                        temperature,
                        provider=pick.gate.provider,
                        reserved_micro=pick.decision.reserved_micro,
                    )
                except RouterError as exc:
                    reason, fallback = reason or exc.code, True
                chain = list(pick.remaining)
                if chain:
                    external_model = gates[chain[0]].default_model
        if not allow_local:
            await self._record(
                _Call(
                    agent, "ollama", "-", "skipped", reason or "no_local_fallback",
                    fallback, local_only,
                )
            )  # fmt: skip
            raise ModelUnavailable(f"no external answer ({reason}) and no local fallback")
        return await self._local(
            agent, cfg, msgs, max_tokens, temperature, reason=reason,
            fallback=fallback, local_only=local_only,
        )  # fmt: skip

    async def _external(
        self,
        agent: str,
        model: str,
        msgs: list[dict[str, str]],
        max_tokens: int | None,
        temperature: float,
        *,
        provider: str = "openrouter",
        reserved_micro: int = 0,
    ) -> Answer:
        client = self.clients[provider]
        started = time.monotonic()
        parts: list[str] = []
        usage: dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": None}

        async def run() -> None:
            async for piece in client.chat_stream(
                model, msgs, temperature=temperature, max_tokens=max_tokens
            ):
                if piece.get("done"):
                    # Clients yield parsed usage (`cost_usd`). The raw OpenRouter shape
                    # (`cost`) is still accepted. Before docs/26 this always re-parsed, which
                    # dropped the real client's cost: agent calls were never charged to the cap.
                    u = piece.get("usage") or {}
                    usage.update(u if "cost_usd" in u else parse_usage(u))
                elif piece.get("content"):
                    parts.append(str(piece["content"]))

        call = _Call(agent, provider, model, "ok")
        try:
            await asyncio.wait_for(run(), self.settings.MODEL_ROUTER_EXTERNAL_TIMEOUT_SECONDS)
        except TimeoutError:
            call.outcome = "timeout"
        except ProviderError as exc:
            call.outcome, call.route_reason = "error", exc.code
        call.latency_ms = int((time.monotonic() - started) * 1000)
        call.prompt_tokens = int(usage["prompt_tokens"])
        call.completion_tokens = int(usage["completion_tokens"])
        call.cost_usd = usage["cost_usd"]
        await self._record(call)
        if self.usage is not None:
            await self.usage.for_provider(provider).record(
                cost_usd=call.cost_usd,
                prompt_tokens=call.prompt_tokens,
                completion_tokens=call.completion_tokens,
                failed=call.outcome != "ok",
                reserved_micro=reserved_micro,
            )
        if call.outcome != "ok":
            log.warning(
                "model_router_fallback", agent=agent, reason=call.route_reason or call.outcome
            )
            err = RouterError(call.outcome)
            err.code = call.route_reason or call.outcome
            raise err
        return Answer("".join(parts).strip(), provider, model, False, None, False)

    async def _local(
        self,
        agent: str,
        cfg: AgentConfig,
        msgs: list[dict[str, str]],
        max_tokens: int | None,
        temperature: float,
        *,
        reason: str | None,
        fallback: bool,
        local_only: bool,
    ) -> Answer:
        model = cfg.local_model or self.settings.CHAT_DEFAULT_MODEL.strip()
        call = _Call(agent, "ollama", model or "?", "ok", reason, fallback, local_only)
        if not model:
            call.outcome = "error"
            await self._record(call)
            raise ModelUnavailable("no local model configured")
        try:
            ticket = self.limiter.admit()
        except QueueFull as exc:
            call.outcome = "busy"
            await self._record(call)
            raise LocalBusy("the local model queue is full") from exc
        started = time.monotonic()
        parts: list[str] = []
        stats: dict[str, Any] = {}
        options: dict[str, Any] = {
            "temperature": temperature,
            "num_ctx": self.settings.CHAT_DEFAULT_NUM_CTX,
        }
        if max_tokens:
            options["num_predict"] = max_tokens
        if self.settings.CHAT_NUM_THREAD:
            options["num_thread"] = self.settings.CHAT_NUM_THREAD

        async def run() -> None:
            await ticket.acquire_slot()
            async for chunk in self.ollama.chat_stream(model, msgs, options):
                if chunk.get("done"):
                    stats.update(chunk)
                else:
                    parts.append(str((chunk.get("message") or {}).get("content") or ""))

        try:
            await asyncio.wait_for(run(), self.settings.MODEL_ROUTER_LOCAL_TIMEOUT_SECONDS)
        except TimeoutError:
            call.outcome = "timeout"
        except OllamaError as exc:
            call.outcome = "error"
            log.warning("model_router_local_failed", agent=agent, code=exc.code)
        finally:
            ticket.release()
        call.latency_ms = int((time.monotonic() - started) * 1000)
        call.prompt_tokens = int(stats.get("prompt_eval_count") or 0)
        call.completion_tokens = int(stats.get("eval_count") or 0)
        await self._record(call)
        if call.outcome != "ok":
            raise ModelUnavailable(f"local model {call.outcome}")
        return Answer("".join(parts).strip(), "ollama", model, local_only, reason, fallback)

    async def _record(self, call: _Call) -> None:
        """Best effort: a DB problem never fails the agent's work."""
        if self.maker is None:
            return
        try:
            async with self.maker() as db, db.begin():
                db.add(
                    ModelCall(
                        agent=call.agent,
                        provider=call.provider,
                        model=call.model[:200],
                        outcome=call.outcome,
                        route_reason=(call.route_reason or None) and call.route_reason[:40],
                        fallback=call.fallback,
                        local_only=call.local_only,
                        prompt_tokens=call.prompt_tokens,
                        completion_tokens=call.completion_tokens,
                        latency_ms=call.latency_ms,
                        cost_usd=call.cost_usd,
                    )
                )
        except Exception as exc:
            log.warning("model_call_not_recorded", error=type(exc).__name__)
