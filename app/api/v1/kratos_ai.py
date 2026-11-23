"""KRATOS research AI route (docs/39-kratos-ai-gateway.md): the one machine-to-machine door from
the KRATOS research lab to the Claude gateway.

    POST /api/v1/kratos/ai/complete     bearer KRATOS_AI_TOKEN; no session, no cookie, no CSRF

    GET  /api/v1/kratos/ai/config       same bearer; what a call would run with (no tokens spent)

KRATOS sends a role label, a system prompt, one user message and an output limit. It never names
a model: every call runs on KRATOS_AI_MODEL, a runtime setting only the owner changes. Blank means
no model is selected and every call is refused; there is no default, no fallback to another model
and no substitution. The response names the model the API actually served.

Every call goes through app/ai/gateway.py unchanged: redaction, pricing (an unpriced model is
never called), bounded retries, the timeout and the usage log (agent "kratos:<role>", budget
"kratos"). Spending follows KRATOS_AI_BUDGET_POLICY: "capped" (default) applies the monthly cap
and the "kratos" cap (default 0 = refused until set); "metered" records the same usage with no
dollar ceiling and without consuming the shared monthly cap.

The route has no tools and no access to accounts, orders, approvals or settings. It returns text
and usage, nothing else. Off (503) while KRATOS_AI_TOKEN is unset, weak, or reused.
"""

from __future__ import annotations

import asyncio
import hmac
import re
import time
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.agents.trading.ratelimit import BrokerRateLimiter, RateLimitExceeded, Window
from app.ai.budget import shared_total
from app.ai.gateway import ClaudeGateway, GatewayError
from app.ai.redact import Redactor, strip_secrets
from app.api.ui import ui_module
from app.auth.deps import SettingsDep
from app.core.config import Settings
from app.core.logging import get_logger
from app.core.prod_checks import MIN_SECRET_LEN, PLACEHOLDER_MARKERS

log = get_logger(__name__)
# Machine route: SAFETY_RULES in app/api/ui.py keeps it hidden from the UI manifest.
UI_MODULE = ui_module("kratos-ai", label="KRATOS AI", group="ai", icon="cpu", order=46)
router = APIRouter(prefix="/v1/kratos/ai", tags=["kratos"])

BUDGET = "kratos"
# Credentials the KRATOS token must never equal (named in the error, never shown).
DISTINCT_FROM = (
    "ANTHROPIC_API_KEY",
    "SECRET_KEY",
    "SETTINGS_MASTER_KEY",
    "MT5_INGEST_TOKEN",
    "MT5_RECORDS_TOKEN",
    "MT5_STATUS_TOKEN",
    "AUREON_API_READ_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_WEBHOOK_SECRET",
)
# "kratos:<role>" is the ai.usage agent label (varchar(64)): 7 + 48 fits with room to spare.
ROLE = re.compile(r"^[a-z][a-z0-9_:.-]{0,47}$")


class VerbatimRedactor(Redactor):
    """KRATOS sends market evidence (prices, sizes, times, hashes), never personal data, and the
    evidence has to reach the model exactly as it was recorded. The personal-data rules rewrite
    such text: the 13 digits after the point in 3989.5950567873733 pass the card-number check and
    become a placeholder. So nothing is rewritten here; `complete` refuses, before any call, a
    request in which the credential rules find something."""

    def text(self, value: str) -> str:
        return value


def _err(code: int, key: str, message: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(code, detail={"code": key, "message": message}, headers=headers)


def token_problem(settings: Settings) -> str | None:
    """Why the configured token cannot be used (never its value), or None."""
    tok = settings.KRATOS_AI_TOKEN.get_secret_value() if settings.KRATOS_AI_TOKEN else ""
    if not tok:
        return None
    if len(tok) < MIN_SECRET_LEN:
        return f"KRATOS_AI_TOKEN is shorter than {MIN_SECRET_LEN} characters"
    if any(m in tok.lower() for m in PLACEHOLDER_MARKERS):
        return "KRATOS_AI_TOKEN is a placeholder"
    for name in DISTINCT_FROM:
        other = getattr(settings, name, None)
        raw = other.get_secret_value() if isinstance(other, SecretStr) else other
        if isinstance(raw, str) and raw and any(v.strip() == tok for v in raw.split(",")):
            return f"KRATOS_AI_TOKEN must not reuse the value of {name}"
    return None


def check_token(settings: Settings, authorization: str | None) -> None:
    """503 while the route is off or misconfigured; 401 unless `Authorization: Bearer <token>`.
    Constant-time comparison, evaluated even for a wrong scheme."""
    token = settings.KRATOS_AI_TOKEN.get_secret_value() if settings.KRATOS_AI_TOKEN else ""
    if not token:
        raise _err(503, "kratos_ai_disabled", "the KRATOS AI route is off (KRATOS_AI_TOKEN unset)")
    problem = token_problem(settings)
    if problem:
        log.error("kratos_ai_token_misconfigured", problem=problem)
        raise _err(503, "kratos_ai_misconfigured", "the KRATOS AI route is off: " + problem)
    scheme, _, got = (authorization or "").partition(" ")
    match = hmac.compare_digest(got.strip().encode(), token.encode())
    if not (match and scheme.lower() == "bearer"):
        raise _err(
            401, "unauthorized", "bad or missing bearer token", {"WWW-Authenticate": "Bearer"}
        )


class CompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a `model` field (or anything else) is a 422

    role: str = Field(min_length=1, max_length=48)
    system: str = Field(min_length=1)
    user: str = Field(min_length=1)
    max_tokens: int = Field(default=4000, ge=64, le=64_000)
    idempotency_key: str | None = Field(default=None, max_length=160)


class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class CompleteOut(BaseModel):
    text: str
    usage: UsageOut
    cost_usd: str
    latency_ms: int
    model: str  # the model the API served
    model_selected: str  # KRATOS_AI_MODEL at the time of the call
    request_id: str | None
    stop_reason: str | None
    budget: str


def _limiter(request: Request, settings: Settings) -> BrokerRateLimiter:
    lim: BrokerRateLimiter | None = getattr(request.app.state, "kratos_ai_limiter", None)
    if lim is None:
        lim = BrokerRateLimiter(
            request.app.state.redis,
            "ratelimit:kratos-ai",
            {"call": [Window(60, settings.KRATOS_AI_RATE_PER_MINUTE)]},
        )
        request.app.state.kratos_ai_limiter = lim
    return lim


def _slots(request: Request, settings: Settings) -> asyncio.Semaphore:
    sem: asyncio.Semaphore | None = getattr(request.app.state, "kratos_ai_slots", None)
    if sem is None:
        sem = asyncio.Semaphore(settings.KRATOS_AI_MAX_CONCURRENCY)
        request.app.state.kratos_ai_slots = sem
    return sem


@router.post(
    "/complete",
    response_model=CompleteOut,
    responses={
        401: {"description": "bad or missing bearer token"},
        402: {"description": "monthly or kratos budget cap reached (or the kratos cap is 0)"},
        413: {"description": "request over KRATOS_AI_MAX_REQUEST_CHARS"},
        422: {"description": "invalid body, or the model declined the request"},
        429: {"description": "too many calls, or every concurrency slot is in use"},
        503: {"description": "route off, token misconfigured, or Claude not set up"},
        504: {"description": "Claude did not answer in time"},
    },
)
async def complete(
    body: CompleteIn,
    request: Request,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CompleteOut:
    """One text completion for a KRATOS research specialist, charged to the "kratos" budget."""
    check_token(settings, authorization)
    if not ROLE.match(body.role):
        raise _err(422, "bad_role", "role must be a short lower-case label")
    if len(body.system) + len(body.user) > settings.KRATOS_AI_MAX_REQUEST_CHARS:
        raise _err(413, "too_large", "system + user exceed KRATOS_AI_MAX_REQUEST_CHARS")
    if strip_secrets(body.system) != body.system or strip_secrets(body.user) != body.user:
        # Sent exactly as written or not at all: a credential is never forwarded, and evidence
        # is never silently edited on the way to the model.
        log.warning("kratos_ai_credential_like_content", role=body.role)
        raise _err(
            422, "credential_like_content", "the request holds credential-shaped text; not sent"
        )
    try:
        await _limiter(request, settings).acquire("call", max_wait=0)
    except RateLimitExceeded as exc:
        raise _err(
            429,
            "rate_limited",
            "too many KRATOS AI calls",
            {"Retry-After": str(max(1, int(exc.wait_s)))},
        ) from exc
    except Exception as exc:  # Redis down: a paid route fails closed
        log.warning("kratos_ai_ratelimit_unavailable", error=type(exc).__name__)
        raise _err(503, "rate_limiter_unavailable", "the rate limiter is unavailable") from exc
    gateway: ClaudeGateway | None = getattr(request.app.state, "ai", None)
    if gateway is None or not gateway.configured:
        raise _err(503, "ai_not_configured", "Claude is not set up in Aureon")
    model = settings.KRATOS_AI_MODEL.strip()
    if not model:
        raise _err(
            503, "kratos_model_not_selected", "no model is selected (KRATOS_AI_MODEL is blank)"
        )
    slots = _slots(request, settings)
    if slots.locked():
        raise _err(429, "busy", "every KRATOS AI slot is in use", {"Retry-After": "5"})
    t0 = time.monotonic()
    async with slots:
        try:
            result = await gateway.call(
                agent=f"kratos:{body.role}",
                budget=BUDGET,
                system=body.system,
                messages=[{"role": "user", "content": body.user}],
                model=model,
                max_tokens=min(body.max_tokens, settings.KRATOS_AI_MAX_OUTPUT_TOKENS),
                redactor=VerbatimRedactor(),
            )
        except GatewayError as exc:
            log.info("kratos_ai_refused", role=body.role, model=model, code=exc.code)
            raise _err(exc.status, exc.code, str(exc)) from None
    u = result.message.usage
    return CompleteOut(
        text=result.text,
        usage=UsageOut(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=u.cache_read_input_tokens or 0,
            cache_write_tokens=u.cache_creation_input_tokens or 0,
        ),
        cost_usd=str(result.cost_usd),
        latency_ms=int((time.monotonic() - t0) * 1000),
        model=str(result.message.model),
        model_selected=model,
        request_id=getattr(result.message, "_request_id", None),
        stop_reason=result.message.stop_reason,
        budget=BUDGET,
    )


class ConfigOut(BaseModel):
    model_selected: str
    model_priced: bool
    model_available: bool | None  # None = not probed
    claude_configured: bool
    budget_policy: str
    kratos_cap_usd: str | None  # None = metered (no ceiling)
    monthly_cap_usd: str
    month_to_date_usd: str
    kratos_month_to_date_usd: str
    max_request_chars: int
    max_output_tokens: int
    rate_per_minute: int
    max_concurrency: int
    timeout_seconds: float
    max_retries: int
    effort: str


@router.get("/config", response_model=ConfigOut, responses={401: {}, 503: {}})
async def config(
    request: Request,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    probe: bool = False,
) -> ConfigOut:
    """What a KRATOS call would run with right now. `probe=true` also asks the API whether the
    key can use the selected model (GET /v1/models; spends no tokens)."""
    check_token(settings, authorization)
    gateway: ClaudeGateway | None = getattr(request.app.state, "ai", None)
    configured = bool(gateway is not None and gateway.configured)
    model = settings.KRATOS_AI_MODEL.strip()
    available: bool | None = None
    if probe and configured and model and gateway is not None:
        try:
            available = model in await gateway.probe()
        except GatewayError as exc:
            raise _err(exc.status, exc.code, str(exc)) from None
    spend = await gateway.store.spend() if gateway is not None else None
    metered = BUDGET in settings.ai_metered_budgets
    return ConfigOut(
        model_selected=model,
        model_priced=bool(model and settings.claude_price(model) is not None),
        model_available=available,
        claude_configured=configured,
        budget_policy=settings.KRATOS_AI_BUDGET_POLICY,
        kratos_cap_usd=None if metered else str(settings.AI_AGENT_BUDGETS_USD.get(BUDGET, 0)),
        monthly_cap_usd=str(settings.AI_MONTHLY_BUDGET_USD),
        # The spend the monthly cap is compared with (a metered kratos budget is not part of it).
        month_to_date_usd=str(shared_total(settings, spend)) if spend else "0",
        kratos_month_to_date_usd=str(spend.by_budget.get(BUDGET, 0)) if spend else "0",
        max_request_chars=settings.KRATOS_AI_MAX_REQUEST_CHARS,
        max_output_tokens=settings.KRATOS_AI_MAX_OUTPUT_TOKENS,
        rate_per_minute=settings.KRATOS_AI_RATE_PER_MINUTE,
        max_concurrency=settings.KRATOS_AI_MAX_CONCURRENCY,
        timeout_seconds=settings.AI_TIMEOUT_SECONDS,
        max_retries=settings.AI_MAX_RETRIES,
        effort=settings.AI_EFFORT,
    )


__all__ = ["BUDGET", "check_token", "router", "token_problem"]
