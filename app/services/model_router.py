"""One way for an agent to ask a model (docs/model-routing.md).

    answer = await router.complete("news", messages, context="news")

Routing per call, in order:

1. The agent's settings (core.app_settings, /settings → Agents): disabled -> AgentDisabled;
   provider "ollama" -> local; "auto"/"openrouter" -> ask the existing ExternalGate
   (app/services/inference.py): OpenRouter enabled + key + budget, model allowed, the data
   classification filter (app/services/chat_filter.py) on the full payload, and the shared
   OPENROUTER_DAILY_LIMIT_USD cap. No second filter exists.
2. External: bounded by MODEL_ROUTER_EXTERNAL_TIMEOUT_SECONDS (10 s). An error, a timeout or
   the cap -> the same request goes to local Ollama, and the fallback is logged + recorded.
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
from app.agents.openrouter.client import OpenRouterClient, OpenRouterError, parse_usage
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.model_calls import ModelCall
from app.services.ai_usage import UsageTracker
from app.services.inference import ExternalGate

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
    provider: str  # auto | ollama | openrouter
    model: str  # OpenRouter model ("" = OPENROUTER_DEFAULT_MODEL)
    local_model: str


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
    # Why it is not on OpenRouter (None when it is): filter | context | disabled |
    # daily_cap_reached | forced_local | timeout | rate_limited | ...
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
    ) -> None:
        self.settings = settings
        self.ollama = ollama
        self.openrouter = openrouter
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
    ) -> Answer:
        cfg = agent_config(self.settings, agent)
        if not cfg.enabled:
            raise AgentDisabled(f"the {agent} agent is disabled")
        msgs = [{"role": m["role"], "content": m["content"]} for m in messages]
        external_model = cfg.model or self.settings.OPENROUTER_DEFAULT_MODEL.strip()
        reason: str | None
        local_only = False
        fallback = False
        if force_local or cfg.provider == "ollama":
            reason = "forced_local"
        else:
            gate = ExternalGate(self.settings, self.openrouter, self.usage)
            decision = await gate.decide(external_model, msgs, context=context)
            if decision.external:
                try:
                    return await self._external(
                        agent, external_model, msgs, max_tokens, temperature
                    )
                except RouterError as exc:
                    reason, fallback = exc.code, True
            else:
                reason, local_only = decision.reason, decision.local_only
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
    ) -> Answer:
        assert self.openrouter is not None
        started = time.monotonic()
        parts: list[str] = []
        usage: dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": None}

        async def run() -> None:
            assert self.openrouter is not None
            async for piece in self.openrouter.chat_stream(
                model, msgs, temperature=temperature, max_tokens=max_tokens
            ):
                if piece.get("done"):
                    usage.update(parse_usage(piece.get("usage")))
                elif piece.get("content"):
                    parts.append(str(piece["content"]))

        call = _Call(agent, "openrouter", model, "ok")
        try:
            await asyncio.wait_for(run(), self.settings.MODEL_ROUTER_EXTERNAL_TIMEOUT_SECONDS)
        except TimeoutError:
            call.outcome = "timeout"
        except OpenRouterError as exc:
            call.outcome, call.route_reason = "error", exc.code
        call.latency_ms = int((time.monotonic() - started) * 1000)
        call.prompt_tokens = int(usage["prompt_tokens"])
        call.completion_tokens = int(usage["completion_tokens"])
        call.cost_usd = usage["cost_usd"]
        await self._record(call)
        if self.usage is not None:
            await self.usage.record(
                cost_usd=call.cost_usd,
                prompt_tokens=call.prompt_tokens,
                completion_tokens=call.completion_tokens,
                failed=call.outcome != "ok",
            )
        if call.outcome != "ok":
            log.warning(
                "model_router_fallback", agent=agent, reason=call.route_reason or call.outcome
            )
            err = RouterError(call.outcome)
            err.code = call.route_reason or call.outcome
            raise err
        return Answer("".join(parts).strip(), "openrouter", model, False, None, False)

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
