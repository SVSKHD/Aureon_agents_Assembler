"""Fixed-window login rate limiting in Redis, keyed by (email, client IP) and by client IP."""

from __future__ import annotations

import hashlib

from redis.asyncio import Redis

from app.core.config import Settings


def _h(value: str) -> str:
    # Hash so raw emails/IPs never appear as Redis keys.
    return hashlib.sha256(value.encode()).hexdigest()[:24]


class LoginRateLimiter:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.window = settings.LOGIN_WINDOW_SECONDS
        self.max_pair = settings.LOGIN_MAX_FAILURES
        self.max_ip = settings.LOGIN_MAX_FAILURES_PER_IP

    def _keys(self, email: str, ip: str) -> tuple[str, str]:
        return f"rl:login:pair:{_h(email + '|' + ip)}", f"rl:login:ip:{_h(ip)}"

    async def retry_after(self, email: str, ip: str) -> int | None:
        """Seconds until another attempt is allowed, or None if not limited."""
        pair, ip_key = self._keys(email, ip)
        pair_n, ip_n = await self.redis.mget(pair, ip_key)
        blocked = [
            key
            for key, n, limit in ((pair, pair_n, self.max_pair), (ip_key, ip_n, self.max_ip))
            if n is not None and int(n) >= limit
        ]
        if not blocked:
            return None
        ttls = [await self.redis.ttl(k) for k in blocked]
        return max(1, *ttls)

    async def record_failure(self, email: str, ip: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            for key in self._keys(email, ip):
                pipe.incr(key)
                pipe.expire(key, self.window, nx=True)
            await pipe.execute()

    async def reset(self, email: str, ip: str) -> None:
        # A successful login clears the (email, IP) counter, not the per-IP one.
        await self.redis.delete(self._keys(email, ip)[0])
