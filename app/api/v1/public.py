"""Public, unauthenticated, read-only endpoints for the frontend.

    GET /api/public/shares/{doc_id}    a shared document (namespace aureon-shares) whose
                                       data.isPublic is true; anything else is 404
    GET /api/public/forex?limit=       server-pulled news items (namespace forex), newest first

Both are rate limited per client IP (PUBLIC_RATE_LIMIT_PER_MINUTE) in Redis. Owner ids
(`ownerId`, `userId`) are stripped from the returned data at every depth.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy import and_, select

from app.agents.trading.ratelimit import BrokerRateLimiter, RateLimitExceeded, Window
from app.api.ui import ui_module
from app.auth.deps import DbSession, SettingsDep
from app.core.logging import get_logger
from app.models.finance import astra_documents
from app.schemas.finance import PublicDocumentOut
from app.services.audit import ClientInfo

log = get_logger(__name__)
UI_MODULE = ui_module("public", label="Public", group="public", icon="globe", order=990)
router = APIRouter(prefix="/public", tags=["public"])

SHARES_NAMESPACE = "aureon-shares"
FOREX_NAMESPACE = "forex"
OWNER_KEYS = frozenset({"ownerId", "userId"})


def strip_owner(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: strip_owner(v) for k, v in value.items() if k not in OWNER_KEYS}
    if isinstance(value, list):
        return [strip_owner(v) for v in value]
    return value


async def rate_limited(request: Request, settings: SettingsDep) -> None:
    ip = ClientInfo.from_request(request).ip or "unknown"
    key = hashlib.sha256(ip.encode()).hexdigest()[:24]
    limiter = BrokerRateLimiter(
        request.app.state.redis,
        f"rl:public:{key}",
        {"public": [Window(60, settings.PUBLIC_RATE_LIMIT_PER_MINUTE)]},
    )
    try:
        await limiter.acquire("public", max_wait=0)
    except RateLimitExceeded as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many requests",
            headers={"Retry-After": str(max(1, int(exc.wait_s + 0.999)))},
        ) from exc
    except Exception:  # Redis down: serve read-only public data rather than fail (D-Dbe-6)
        log.warning("public_rate_limit_unavailable")


Limited = Depends(rate_limited)


def _out(row: Any) -> PublicDocumentOut:
    return PublicDocumentOut(
        doc_id=row.doc_id, data=strip_owner(row.data), updated_at=row.updated_at
    )


@router.get("/shares/{doc_id}", response_model=PublicDocumentOut, dependencies=[Limited])
async def get_share(
    doc_id: Annotated[str, Path(min_length=1, max_length=256)], db: DbSession
) -> PublicDocumentOut:
    t = astra_documents
    row = (
        await db.execute(
            select(t).where(
                t.c.namespace == SHARES_NAMESPACE,
                t.c.doc_id == doc_id,
                t.c.data["isPublic"].astext == "true",
            )
        )
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "share not found")
    return _out(row)


@router.get("/forex", response_model=list[PublicDocumentOut], dependencies=[Limited])
async def list_forex(
    db: DbSession, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[PublicDocumentOut]:
    # Only server-written items (no owner): a user's own "forex" documents stay private.
    t = astra_documents
    published = t.c.data["published"].astext
    rows = (
        await db.execute(
            select(t)
            .where(and_(t.c.namespace == FOREX_NAMESPACE, t.c.aureon_user_id.is_(None)))
            .order_by(published.desc().nulls_last(), t.c.updated_at.desc(), t.c.doc_id)
            .limit(limit)
        )
    ).all()
    return [_out(r) for r in rows]
