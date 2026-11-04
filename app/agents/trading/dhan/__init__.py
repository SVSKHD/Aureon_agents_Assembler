"""DhanHQ v2 read-only integration (Trading Phase B). See docs/11-dhan.md."""

from __future__ import annotations

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.trading.dhan.adapter import DbInstrumentResolver, DhanAdapter
from app.agents.trading.dhan.client import BUCKETS, DhanClient
from app.agents.trading.ratelimit import BrokerRateLimiter
from app.core.config import Settings


def build_dhan_adapter(
    settings: Settings, redis: Redis, maker: async_sessionmaker[AsyncSession]
) -> DhanAdapter | None:
    """The adapter for the configured Dhan client, or None without a usable token: the
    logged-in session's token while valid, else the manual DHAN_ACCESS_TOKEN."""
    if not settings.DHAN_CLIENT_ID or settings.dhan_access_token() is None:
        return None

    def token() -> str | None:
        current = settings.dhan_access_token()
        return current.get_secret_value() if current is not None else None

    client = DhanClient(
        settings.DHAN_CLIENT_ID,
        token,
        base_url=settings.DHAN_BASE_URL,
        timeout=settings.DHAN_HTTP_TIMEOUT,
        limiter=BrokerRateLimiter(redis, f"rl:dhan:{settings.DHAN_CLIENT_ID}", BUCKETS),
    )
    return DhanAdapter(
        client,
        DbInstrumentResolver(maker),
        instrument_csv_url=settings.DHAN_INSTRUMENT_CSV_URL,
        token_warn_hours=settings.DHAN_TOKEN_WARN_HOURS,
    )
