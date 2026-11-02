"""Session re-auth stamps for the approval engine (docs/23-approval-engine.md).

`POST /api/v1/approvals/reauth {password}` verifies the admin's password and stores a stamp in
Redis keyed by the login session's id:

    approvals:reauth:<session id>  ->  {"user_id": ..., "at": ISO-8601}   (TTL seconds)

The TTL is the largest `reauth_max_age_seconds` of the enabled policies, capped by
APPROVALS_REAUTH_STAMP_MAX_SECONDS. Each policy decides whether a stamp of a given age counts
(`reauth_max_age_seconds`, 0 = never). The stamp is server-side only: nothing is put in a
cookie or returned that could be replayed, and it dies with the session (the key names the
session, and an approve call needs that session to be valid anyway).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.approvals import ApprovalPolicy

STAMP_KEY = "approvals:reauth:{}"
FAIL_KEY = "approvals:reauth_fail:{}"
FAIL_WINDOW_SECONDS = 900


async def stamp_ttl(db: AsyncSession, settings: Settings) -> int:
    longest = await db.scalar(
        select(func.max(ApprovalPolicy.reauth_max_age_seconds)).where(
            ApprovalPolicy.enabled.is_(True)
        )
    )
    return max(0, min(int(longest or 0), settings.APPROVALS_REAUTH_STAMP_MAX_SECONDS))


async def set_stamp(
    redis: Redis, session_id: uuid.UUID, user_id: uuid.UUID, ttl: int, at: datetime | None = None
) -> datetime:
    at = at or datetime.now(UTC)
    value = json.dumps({"user_id": str(user_id), "at": at.isoformat()})
    await redis.set(STAMP_KEY.format(session_id), value, ex=ttl)
    await redis.delete(FAIL_KEY.format(session_id))
    return at


async def get_stamp(redis: Redis, session_id: uuid.UUID, user_id: uuid.UUID) -> datetime | None:
    """The stamp's time, if one exists for this session and this user."""
    try:
        raw = await redis.get(STAMP_KEY.format(session_id))
    except Exception:  # Redis down: no stamp, the password is needed
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if data.get("user_id") != str(user_id):
            return None
        at = datetime.fromisoformat(str(data["at"]))
    except (ValueError, KeyError, TypeError):
        return None
    return at if at.tzinfo else at.replace(tzinfo=UTC)


async def stamp_expires_in(redis: Redis, session_id: uuid.UUID) -> int:
    try:
        ttl = await redis.ttl(STAMP_KEY.format(session_id))
    except Exception:
        return -2
    return int(ttl)


async def clear_stamp(redis: Redis, session_id: uuid.UUID) -> None:
    await redis.delete(STAMP_KEY.format(session_id))


async def count_failure(redis: Redis, session_id: uuid.UUID) -> int:
    key = FAIL_KEY.format(session_id)
    n = int(await redis.incr(key))
    if n == 1:
        await redis.expire(key, FAIL_WINDOW_SECONDS)
    return n


async def failures(redis: Redis, session_id: uuid.UUID) -> int:
    raw = await redis.get(FAIL_KEY.format(session_id))
    return int(raw or 0)
