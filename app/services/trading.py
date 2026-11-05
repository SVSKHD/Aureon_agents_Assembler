"""Trading account access, execution-mode gate, risk policy, and adapter calls."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.trading.base import BrokerAdapter, BrokerError, CredentialsExpired, NotSupported
from app.agents.trading.ratelimit import RateLimitExceeded
from app.agents.trading.types import ExecutionMode
from app.auth.deps import CurrentSession
from app.core.config import Settings
from app.models.auth import ROLE_ADMIN
from app.models.trading import BrokerAccount, RiskPolicy

# What Trading Phase G must provide before LIVE can be selected. Until then this list is the
# reason LIVE stays unreachable even with TRADING_LIVE_ENABLED=true and a valid token.
LIVE_PRECONDITIONS_TODO = [
    "static egress IP preflight (scripts/dhan_preflight.py) — Phase G",
    "account-specific risk policy with a tested kill switch — Phase G",
    "fresh broker credentials — Phase G",
    "execution module behind its own feature flag — Phase G",
]


def is_admin(current: CurrentSession) -> bool:
    return current.user.role.name == ROLE_ADMIN


def visible_accounts(current: CurrentSession) -> Select[tuple[BrokerAccount]]:
    """Owners see their accounts; admins see all. Enforced here, not only in the router."""
    q = select(BrokerAccount)
    if not is_admin(current):
        q = q.where(BrokerAccount.owner_user_id == current.user.id)
    return q


async def get_visible_account(
    db: AsyncSession, current: CurrentSession, account_id: uuid.UUID
) -> BrokerAccount:
    acct = await db.scalar(visible_accounts(current).where(BrokerAccount.id == account_id))
    if acct is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
    return acct


async def visible_account_ids(db: AsyncSession, current: CurrentSession) -> list[uuid.UUID]:
    rows = await db.scalars(visible_accounts(current).with_only_columns(BrokerAccount.id))
    return list(rows)


# --- execution mode -----------------------------------------------------------------------


@dataclass
class ModeChangeDenied(Exception):
    code: str
    message: str
    http_status: int = status.HTTP_403_FORBIDDEN
    unmet: list[str] | None = None


def _token_key(account_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return f"trading:live-token:{account_id}:{user_id}"


async def issue_live_token(
    redis: Redis, settings: Settings, account_id: uuid.UUID, user_id: uuid.UUID
) -> str:
    if not settings.TRADING_LIVE_ENABLED:
        raise ModeChangeDenied("live_disabled", "LIVE execution is disabled (feature flag off)")
    token = secrets.token_urlsafe(24)
    await redis.set(
        _token_key(account_id, user_id),
        hashlib.sha256(token.encode()).hexdigest(),
        ex=settings.TRADING_LIVE_TOKEN_TTL_SECONDS,
    )
    return token


async def _consume_live_token(
    redis: Redis, account_id: uuid.UUID, user_id: uuid.UUID, token: str | None
) -> bool:
    if not token:
        return False
    stored = await redis.getdel(_token_key(account_id, user_id))  # single use
    if stored is None:
        return False
    expected = stored.decode() if isinstance(stored, bytes) else str(stored)
    return hmac.compare_digest(expected, hashlib.sha256(token.encode()).hexdigest())


async def check_mode_change(
    redis: Redis,
    settings: Settings,
    account: BrokerAccount,
    new_mode: ExecutionMode,
    admin_id: uuid.UUID,
    token: str | None,
) -> None:
    """Raise ModeChangeDenied unless the change is allowed. Leaving LIVE is always allowed."""
    if new_mode is not ExecutionMode.LIVE:
        return
    if not settings.TRADING_LIVE_ENABLED:
        raise ModeChangeDenied("live_disabled", "LIVE execution is disabled (feature flag off)")
    if not await _consume_live_token(redis, account.id, admin_id, token):
        raise ModeChangeDenied(
            "confirmation_required", "a valid, unused LIVE confirmation token is required"
        )
    raise ModeChangeDenied(
        "preconditions_unmet",
        "LIVE preconditions are not met",
        status.HTTP_409_CONFLICT,
        LIVE_PRECONDITIONS_TODO,
    )


async def risk_policy(db: AsyncSession, account_id: uuid.UUID) -> RiskPolicy | None:
    found: RiskPolicy | None = await db.scalar(
        select(RiskPolicy).where(RiskPolicy.broker_account_id == account_id)
    )
    return found


async def count(db: AsyncSession, q: Select[Any]) -> int:
    return int(await db.scalar(select(func.count()).select_from(q.subquery())) or 0)


# --- adapter calls ------------------------------------------------------------------------


def adapter_for(adapters: dict[str, BrokerAdapter], broker: str, operation: str) -> BrokerAdapter:
    adapter = adapters.get(broker)
    if adapter is None:
        raise _not_supported(
            NotSupported(
                broker, operation, "no adapter configured yet (Trading Phase B: Dhan, C: MT5)"
            )
        )
    return adapter


def _not_supported(exc: NotSupported) -> HTTPException:
    return HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        detail={
            "code": "not_supported",
            "broker": exc.broker,
            "operation": exc.operation,
            "detail": exc.detail,
        },
    )


async def call_adapter[T](awaitable: Awaitable[T], settings: Settings) -> T:
    """Run a read-only adapter call with a timeout, mapping failures to clean HTTP errors."""
    try:
        async with asyncio.timeout(settings.TRADING_ADAPTER_TIMEOUT):
            return await awaitable
    except NotSupported as exc:
        raise _not_supported(exc) from exc
    except ValueError as exc:  # e.g. a malformed "SEGMENT:SECURITY_ID" reference
        raise HTTPException(422, detail={"code": "bad_request", "detail": str(exc)}) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT, detail={"code": "broker_timeout"}
        ) from exc
    except CredentialsExpired as exc:
        # The broker refused our credential: a person has to log in again. Not a 401 (that
        # would read as "your Aureon session ended" to the SPA).
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "broker_credentials_invalid",
                "detail": "the broker no longer accepts the stored credential; log in again",
            },
        ) from exc
    except RateLimitExceeded as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "broker_rate_limited", "detail": "broker rate limit; try again"},
            headers={"Retry-After": str(max(1, int(exc.wait_s + 0.999)))},
        ) from exc
    except RedisError as exc:  # the shared rate limiter needs Redis: fail closed, cleanly
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "unavailable", "detail": "rate limiter unavailable"},
        ) from exc
    except BrokerError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail={"code": "broker_error", "detail": str(exc)[:300]}
        ) from exc
