"""Daily external-AI spend and token counters (docs/25-openrouter.md, docs/26), in Redis.

One hash per provider and UTC day, `ai:<provider>:usage:YYYY-MM-DD` (OpenRouter's key is
unchanged: `ai:openrouter:usage:...`), kept 8 days:

    cost_micro         USD x 1e6, summed from OpenRouter's reported `usage.cost`
    prompt_tokens      summed
    completion_tokens  summed
    calls              external calls attempted (success or not)
    failures           external calls that failed (and fell back)

`precheck` runs before every external call: a cap of 0 or a reached cap keeps the request
local. If Redis cannot be read, the spend is unknown, so the request stays local too (fail
closed).

Concurrency. For a priced provider (Anthropic, Gemini) `reserve` holds the call's worst case
(prompt estimate + max output tokens, at the configured price) in `ai:<p>:reserved:<day>`
before the call starts, atomically (HINCRBY, then check), so calls running at the same time
cannot together pass the cap; `record` releases it. A reservation left by a crashed process
expires with its key (RESERVE_TTL_SECONDS after the last reservation), which only ever makes
the cap stricter for that long. OpenRouter reports its cost only at the end, so it keeps the
old rule: concurrent calls can overshoot by at most their own cost.

The shared cap (AI_EXTERNAL_DAILY_LIMIT_USD) is checked against the sum of every provider's
spend + reservations; `minute_check` is the local per-provider request rate limit.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from app.core.logging import get_logger

log = get_logger(__name__)

KEY_PREFIX = "ai:openrouter:usage:"  # OpenRouter's (kept for existing callers)
TTL_SECONDS = 8 * 24 * 3600
RESERVE_TTL_SECONDS = 15 * 60
MICRO = Decimal("1000000")
PROVIDERS = ("openrouter", "anthropic", "gemini")


class _Redis(Protocol):
    def hgetall(self, name: str) -> Awaitable[Any]: ...
    def hincrby(self, name: str, key: str, amount: int = 1) -> Awaitable[Any]: ...
    def expire(self, name: str, time: int) -> Awaitable[Any]: ...


def utc_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(UTC).date()


def day_key(day: date, provider: str = "openrouter") -> str:
    return f"ai:{provider}:usage:{day.isoformat()}"


def reserve_key(day: date, provider: str) -> str:
    return f"ai:{provider}:reserved:{day.isoformat()}"


def to_micro(usd: Decimal | None) -> int:
    if usd is None or usd <= 0:
        return 0
    return int((usd * MICRO).to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class DailyUsage:
    day: date
    cost_micro: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    failures: int = 0

    @property
    def spent_usd(self) -> Decimal:
        return Decimal(self.cost_micro) / MICRO


def _int(raw: dict[Any, Any], name: str) -> int:
    v = raw.get(name, raw.get(name.encode()))
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


class UsageTracker:
    def __init__(self, redis: _Redis, provider: str = "openrouter") -> None:
        self.redis = redis
        self.provider = provider

    def for_provider(self, provider: str) -> UsageTracker:
        """The same Redis, another provider's counters."""
        return self if provider == self.provider else UsageTracker(self.redis, provider)

    async def today(self, now: datetime | None = None) -> DailyUsage:
        """Raises whatever Redis raises; callers decide how to fail."""
        day = utc_today(now)
        raw = await self.redis.hgetall(day_key(day, self.provider)) or {}
        return DailyUsage(
            day,
            _int(raw, "cost_micro"),
            _int(raw, "prompt_tokens"),
            _int(raw, "completion_tokens"),
            _int(raw, "calls"),
            _int(raw, "failures"),
        )

    async def precheck(self, limit_usd: Decimal, now: datetime | None = None) -> str | None:
        """None when an external call may start; else the fallback reason code."""
        if limit_usd <= 0:
            return "no_budget"
        try:
            usage = await self.today(now)
        except Exception as exc:  # unknown spend: stay local
            log.warning("ai_usage_unreadable", provider=self.provider, error=type(exc).__name__)
            return "usage_unavailable"
        if usage.cost_micro >= to_micro(limit_usd):
            return "daily_cap_reached"
        return None

    async def reserved_micro(self, now: datetime | None = None) -> int:
        raw = await self.redis.hgetall(reserve_key(utc_today(now), self.provider)) or {}
        return max(_int(raw, "micro"), 0)

    async def total_today_micro(self, now: datetime | None = None) -> int:
        """Spend + open reservations today, summed over every external provider."""
        total = 0
        for p in PROVIDERS:
            t = self.for_provider(p)
            total += (await t.today(now)).cost_micro + await t.reserved_micro(now)
        return total

    async def reserve(
        self,
        limit_usd: Decimal,
        estimate_usd: Decimal | None,
        global_limit_usd: Decimal | None = None,
        now: datetime | None = None,
    ) -> tuple[str | None, int]:
        """(None, reserved micro-USD) when the call may start; else (reason, 0).

        Run after `precheck`. Holds `estimate_usd` against this provider's cap and the shared
        cap, atomically. Any Redis error refuses the call (fail closed)."""
        est = to_micro(estimate_usd)
        held = 0
        try:
            if est:
                key = reserve_key(utc_today(now), self.provider)
                reserved = int(await self.redis.hincrby(key, "micro", est))
                held = est
                await self.redis.expire(key, RESERVE_TTL_SECONDS)
                spent = (await self.today(now)).cost_micro
                if spent + reserved > to_micro(limit_usd):
                    await self.release(held, now)
                    return "daily_cap_reached", 0
            if global_limit_usd is not None:
                total = await self.total_today_micro(now)
                cap = to_micro(global_limit_usd)
                if total > cap or (not est and total >= cap):
                    await self.release(held, now)
                    return "global_cap_reached", 0
        except Exception as exc:
            log.warning("ai_usage_reserve_failed", provider=self.provider, error=type(exc).__name__)
            await self.release(held, now)
            return "usage_unavailable", 0
        return None, held

    async def release(self, micro: int, now: datetime | None = None) -> None:
        if micro <= 0:
            return
        try:
            await self.redis.hincrby(reserve_key(utc_today(now), self.provider), "micro", -micro)
        except Exception as exc:  # the key expires on its own
            log.warning("ai_usage_release_failed", provider=self.provider, error=type(exc).__name__)

    async def minute_check(self, per_minute: int, now: datetime | None = None) -> str | None:
        """Count one call in this minute's window; "rate_limited_local" over `per_minute`."""
        if per_minute <= 0:
            return None
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        key = f"ai:{self.provider}:rpm:{moment.strftime('%Y%m%d%H%M')}"
        try:
            n = int(await self.redis.hincrby(key, "n", 1))
            await self.redis.expire(key, 120)
        except Exception as exc:
            log.warning("ai_rate_unreadable", provider=self.provider, error=type(exc).__name__)
            return "usage_unavailable"
        return "rate_limited_local" if n > per_minute else None

    async def record(
        self,
        *,
        cost_usd: Decimal | None,
        prompt_tokens: int,
        completion_tokens: int,
        failed: bool,
        now: datetime | None = None,
        reserved_micro: int = 0,
    ) -> None:
        """Best effort: a Redis error is logged, never raised into the chat. Releases the
        call's reservation (if any) once its actual cost is counted."""
        key = day_key(utc_today(now), self.provider)
        try:
            await self.redis.hincrby(key, "calls", 1)
            if failed:
                await self.redis.hincrby(key, "failures", 1)
            if micro := to_micro(cost_usd):
                await self.redis.hincrby(key, "cost_micro", micro)
            if prompt_tokens:
                await self.redis.hincrby(key, "prompt_tokens", int(prompt_tokens))
            if completion_tokens:
                await self.redis.hincrby(key, "completion_tokens", int(completion_tokens))
            await self.redis.expire(key, TTL_SECONDS)
        except Exception as exc:
            log.warning("ai_usage_record_failed", provider=self.provider, error=type(exc).__name__)
        await self.release(reserved_micro, now)


__all__ = [
    "PROVIDERS",
    "DailyUsage",
    "UsageTracker",
    "day_key",
    "reserve_key",
    "to_micro",
    "utc_today",
]
