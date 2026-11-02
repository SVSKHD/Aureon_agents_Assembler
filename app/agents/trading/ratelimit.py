"""Redis-backed rate limiter shared by every process that calls a broker.

The API, the scheduler and the Dhan feed worker all spend the same broker quota, so the
counters live in Redis rather than in process memory. Each bucket has one or more
fixed windows (per second, minute, hour, day). `acquire()` waits until every window has
room, then counts the call in all of them atomically.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from redis.asyncio import Redis

# KEYS: one per window. ARGV: [limit_1, ttl_1, limit_2, ttl_2, ...]
# Returns 0 when counted, or the ms to wait before retrying.
_LUA = """
for i = 1, #KEYS do
  local n = tonumber(redis.call('GET', KEYS[i]) or '0')
  if n >= tonumber(ARGV[2*i-1]) then
    local ttl = redis.call('PTTL', KEYS[i])
    if ttl < 0 then ttl = 50 end
    return ttl
  end
end
for i = 1, #KEYS do
  local n = redis.call('INCR', KEYS[i])
  if n == 1 then redis.call('PEXPIRE', KEYS[i], ARGV[2*i]) end
end
return 0
"""


@dataclass(frozen=True, slots=True)
class Window:
    seconds: int
    limit: int


class RateLimitExceeded(Exception):
    def __init__(self, bucket: str, wait_s: float) -> None:
        super().__init__(f"rate limit for {bucket!r} would need a {wait_s:.1f}s wait")
        self.bucket = bucket
        self.wait_s = wait_s


class BrokerRateLimiter:
    def __init__(self, redis: Redis, prefix: str, buckets: dict[str, list[Window]]) -> None:
        self.redis = redis
        self.prefix = prefix
        self.buckets = buckets
        self._script = redis.register_script(_LUA)

    def _keys(self, bucket: str, now: float) -> tuple[list[str], list[int]]:
        keys, argv = [], []
        for w in self.buckets[bucket]:
            slot = int(now // w.seconds)
            keys.append(f"{self.prefix}:{bucket}:{w.seconds}:{slot}")
            argv += [w.limit, w.seconds * 1000 + 250]
        return keys, argv

    async def acquire(self, bucket: str, max_wait: float = 30.0) -> None:
        """Wait (up to max_wait) for room in every window of `bucket`, then count one call."""
        deadline = time.monotonic() + max_wait
        while True:
            keys, argv = self._keys(bucket, time.time())
            wait_ms = int(await self._script(keys=keys, args=argv))
            if wait_ms == 0:
                return
            wait = wait_ms / 1000
            if time.monotonic() + wait > deadline:
                raise RateLimitExceeded(bucket, wait)
            await asyncio.sleep(min(wait, 1.0) + 0.01)
