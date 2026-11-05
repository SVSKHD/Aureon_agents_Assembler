"""Dhan live-feed health and the market-data recovery control: /api/v1/dhan/feed.

    GET  /health      signed in    -> feed health (docs/28-dhan-operations.md, "Feed health")
    POST /reconnect   admin+CSRF   -> ask the feed worker to reconnect (cooldown, audited)

Reconnecting is a market-data action only: it touches no account, order, mode, risk policy or
credential. It needs an admin session (not a trading permission), is shared-cooldown limited
with the automatic recovery, and every request (accepted or refused) is audited.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.api.ui import ui_meta, ui_module
from app.auth.deps import AdminRequired, Current, DbSession
from app.core.config import Settings
from app.services import audit
from app.services import dhan_feed_control as ctl
from app.services.audit import ClientInfo

UI_MODULE = ui_module("dhan", label="Dhan", group="markets", icon="landmark", order=26)
router = APIRouter(prefix="/v1/dhan/feed", tags=["dhan"])


class FeedHealthOut(BaseModel):
    status: Literal[
        "ok", "degraded", "down", "market_closed", "not_running", "unknown", "not_configured"
    ]
    healthy: bool
    problems: list[str]
    codes: list[str]
    market: dict[str, Any]
    configured_instruments: int
    feed: dict[str, Any] | None
    worker: dict[str, Any] | None
    last_crash: dict[str, Any] | None
    checked_at: datetime


class ReconnectOut(BaseModel):
    accepted: bool
    receivers: int
    detail: str


@router.get(
    "/health",
    response_model=FeedHealthOut,
    openapi_extra=ui_meta(label="Feed health", kind="detail"),
)
async def health(request: Request, _: Current) -> FeedHealthOut:
    """Never `healthy` without fresh data while the market is open; `market_closed` is its
    own status, never `ok`."""
    st = request.app.state
    data = await ctl.load_health(st.sessionmaker, getattr(st, "redis", None), st.settings)
    return FeedHealthOut.model_validate(data)


@router.post(
    "/reconnect",
    response_model=ReconnectOut,
    openapi_extra=ui_meta(label="Reconnect feed", kind="action", danger="confirm"),
)
async def reconnect(request: Request, admin: AdminRequired, db: DbSession) -> ReconnectOut:
    settings: Settings = request.app.state.settings
    client = ClientInfo.from_request(request)
    if not settings.DHAN_CLIENT_ID or not settings.DHAN_FEED_INSTRUMENTS:
        raise HTTPException(
            409, {"code": "dhan_feed_not_configured", "detail": "the Dhan feed is not configured"}
        )
    try:
        res = await ctl.request_reconnect(
            getattr(request.app.state, "redis", None), settings, f"admin:{admin.user.id}"
        )
    except ctl.ControlUnavailable as exc:
        raise HTTPException(
            503, {"code": "unavailable", "detail": "feed control is unavailable (Redis)"}
        ) from exc
    audit.record(
        db,
        "dhan_feed_reconnect_requested",
        client,
        actor_user_id=admin.user.id,
        details={"accepted": res.accepted, "receivers": res.receivers},
    )
    await db.commit()
    if not res.accepted:
        raise HTTPException(
            429,
            {"code": "cooldown", "detail": "a feed reconnect was requested recently"},
            headers={"Retry-After": str(res.retry_after_s)},
        )
    detail = (
        "reconnect requested"
        if res.receivers
        else "requested, but no feed worker is listening (is the dhan profile running?)"
    )
    return ReconnectOut(accepted=True, receivers=res.receivers, detail=detail)
