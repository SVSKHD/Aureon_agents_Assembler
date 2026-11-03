"""Daily OpenRouter spend and token counter (docs/25-openrouter.md), in Redis.

One hash per UTC day, `ai:openrouter:usage:YYYY-MM-DD`, kept 8 days:

    cost_micro         USD x 1e6, summed from OpenRouter's reported `usage.cost`
    prompt_tokens      summed
    completion_tokens  summed
    calls              external calls attempted (success or not)
    failures           external calls that failed (and fell back)

`precheck` runs before every external call: a cap of 0 or a reached cap keeps the request
local. If Redis cannot be read, the spend is unknown, so the request stays local too (fail
closed). Calls running at the same time can overshoot the cap by at most their own cost.
"""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from app.core.logging import get_logger

log = get_logger(__name__)

KEY_PREFIX = "ai:openrouter:usage:"
TTL_SECONDS = 8 * 24 * 3600
MICRO = Decimal("1000000")


class _Redis(Protocol):
    def hgetall(self, name: str) -> Awaitable[Any]: ...
    def hincrby(self, name: str, key: str, amount: int = 1) -> Awaitable[Any]: ...
    def expire(self, name: str, time: int) -> Awaitable[Any]: ...


def utc_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(UTC).date()


def day_key(day: date) -> str:
    return f"{KEY_PREFIX}{day.isoformat()}"


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
    def __init__(self, redis: _Redis) -> None:
        self.redis = redis

    async def today(self, now: datetime | None = None) -> DailyUsage:
        """Raises whatever Redis raises; callers decide how to fail."""
        day = utc_today(now)
        raw = await self.redis.hgetall(day_key(day)) or {}
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
            log.warning("openrouter_usage_unreadable", error=type(exc).__name__)
            return "usage_unavailable"
        if usage.cost_micro >= to_micro(limit_usd):
            return "daily_cap_reached"
        return None

    async def record(
        self,
        *,
        cost_usd: Decimal | None,
        prompt_tokens: int,
        completion_tokens: int,
        failed: bool,
        now: datetime | None = None,
    ) -> None:
        """Best effort: a Redis error is logged, never raised into the chat."""
        key = day_key(utc_today(now))
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
            log.warning("openrouter_usage_record_failed", error=type(exc).__name__)


__all__ = ["DailyUsage", "UsageTracker", "day_key", "to_micro", "utc_today"]
