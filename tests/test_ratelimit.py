"""Shared Redis rate limiter (needs local Redis; skipped otherwise)."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from redis.asyncio import Redis

from app.agents.trading.ratelimit import BrokerRateLimiter, RateLimitExceeded, Window
from tests.conftest import TEST_REDIS_URL


@pytest.fixture
async def redis():  # type: ignore[no-untyped-def]
    r = Redis.from_url(TEST_REDIS_URL)
    try:
        await r.ping()
    except Exception:
        pytest.skip("redis not available")
    yield r
    await r.aclose()


async def test_per_second_window_spaces_calls(redis: Redis) -> None:
    lim = BrokerRateLimiter(redis, f"t:{uuid.uuid4()}", {"q": [Window(1, 2)]})
    t0 = time.monotonic()
    for _ in range(5):
        await lim.acquire("q")
    # 5 calls at 2/s need at least two window rollovers.
    assert time.monotonic() - t0 >= 1.0


async def test_limit_is_shared_between_limiter_instances(redis: Redis) -> None:
    prefix = f"t:{uuid.uuid4()}"
    a = BrokerRateLimiter(redis, prefix, {"x": [Window(60, 3)]})
    b = BrokerRateLimiter(redis, prefix, {"x": [Window(60, 3)]})
    await asyncio.gather(a.acquire("x"), b.acquire("x"), a.acquire("x"))
    with pytest.raises(RateLimitExceeded) as exc:
        await b.acquire("x", max_wait=0.5)
    assert exc.value.bucket == "x" and exc.value.wait_s > 0.5


async def test_every_window_must_have_room(redis: Redis) -> None:
    lim = BrokerRateLimiter(redis, f"t:{uuid.uuid4()}", {"d": [Window(1, 100), Window(3600, 2)]})
    await lim.acquire("d")
    await lim.acquire("d")
    with pytest.raises(RateLimitExceeded):
        await lim.acquire("d", max_wait=0.2)
