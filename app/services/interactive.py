"""Interactive AI work in progress (chat, voice): heavy background jobs yield to it.

A Redis sorted set `aureon:ai:interactive`: member = one request, score = when it expires.
The API marks a chat (and the voice agent marks a request) while it runs; the image worker
suspends its generation while any unexpired member exists (docs/image-agent.md). An expired
member is ignored, so a crashed request never pauses images for longer than its TTL.
Best effort both ways: with Redis down nothing is marked and nothing is paused.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

KEY = "aureon:ai:interactive"


async def mark(redis: Any, who: str, ttl_seconds: float) -> str | None:
    """Mark one interactive request; returns the token to clear it with."""
    token = f"{who}:{secrets.token_hex(6)}"
    try:
        await redis.zadd(KEY, {token: time.time() + ttl_seconds})
        return token
    except Exception:
        return None


async def clear(redis: Any, token: str | None) -> None:
    if not token:
        return
    try:
        await redis.zrem(KEY, token)
    except Exception:
        return


async def active(redis: Any) -> int:
    """How many interactive requests are running now (expired ones are pruned)."""
    try:
        await redis.zremrangebyscore(KEY, "-inf", time.time())
        return int(await redis.zcard(KEY))
    except Exception:
        return 0
