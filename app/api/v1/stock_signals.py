"""Stock Signals API (docs/37-stock-signals.md), under /api/trading/stock-signals.

Every route needs a signed-in user; writes need the CSRF token. Signals, rankings and the
portfolio map are computed by the research worker and the server, never in the browser.
Nothing here places, changes or cancels an order.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.api.ui import ui_meta, ui_module
from app.auth.deps import CsrfProtected, Current, DbSession, SettingsDep
from app.schemas.stock_signals import (
    NotificationKind,
    SignalTimeframe,
    StockSignalNotificationOut,
    StockSignalNotificationsOut,
    StockSignalPrefsIn,
    StockSignalPrefsOut,
    StockSignalReadAllIn,
    StockSignalUnreadOut,
)
from app.services import stock_signal_notifications as notes

UI_MODULE = ui_module(
    "stock-signals", label="Stock signals", group="markets", icon="activity", order=22
)
router = APIRouter(prefix="/trading/stock-signals", tags=["stock-signals"])


def _prefs_out(p: notes.Prefs) -> StockSignalPrefsOut:
    return StockSignalPrefsOut(
        enabled=p.enabled,
        kinds=sorted(p.kinds),
        timeframes=sorted(p.timeframes),
        all_market=p.all_market,
        confirmed_only=p.confirmed_only,
    )


# --- notifications ------------------------------------------------------------------------------


@router.get("/notifications", response_model=StockSignalNotificationsOut)
async def notifications(
    db: DbSession,
    current: Current,
    unread_only: bool = False,
    kind: NotificationKind | None = None,
    instrument_id: Annotated[int | None, Query(ge=1)] = None,
    timeframe: SignalTimeframe | None = None,
    before_id: Annotated[int | None, Query(ge=1, description="Older than this id")] = None,
    after_id: Annotated[
        int | None, Query(ge=0, description="Polling fallback for /stream: newer, oldest first")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> StockSignalNotificationsOut:
    """Your Stock Signals notifications, newest first (or, with after_id, oldest first)."""
    rows = await notes.list_notifications(
        db,
        current.user.id,
        unread_only=unread_only,
        before_id=before_id,
        after_id=after_id,
        kind=kind,
        instrument_id=instrument_id,
        timeframe=timeframe,
        limit=limit,
    )
    return StockSignalNotificationsOut(
        items=[StockSignalNotificationOut.model_validate(r) for r in rows],
        unread_count=await notes.unread_count(db, current.user.id),
        next_before_id=rows[-1].id if after_id is None and len(rows) == limit else None,
        last_id=rows[-1].id if after_id is not None and rows else after_id,
    )


@router.patch("/notifications/{notification_id}/read", response_model=StockSignalNotificationOut)
async def mark_read(
    notification_id: int, db: DbSession, current: CsrfProtected
) -> StockSignalNotificationOut:
    row = await notes.mark_read(db, current.user.id, notification_id)
    if row is None:
        raise HTTPException(404, {"code": "not_found", "message": "notification not found"})
    await db.commit()
    return StockSignalNotificationOut.model_validate(row)


@router.post("/notifications/read-all", response_model=StockSignalUnreadOut)
async def mark_all_read(
    body: StockSignalReadAllIn, db: DbSession, current: CsrfProtected
) -> StockSignalUnreadOut:
    await notes.mark_all_read(db, current.user.id, body.up_to_id)
    await db.commit()
    return StockSignalUnreadOut(unread_count=await notes.unread_count(db, current.user.id))


@router.get("/notifications/preferences", response_model=StockSignalPrefsOut)
async def get_preferences(db: DbSession, current: Current) -> StockSignalPrefsOut:
    return _prefs_out(await notes.get_prefs(db, current.user.id))


@router.put("/notifications/preferences", response_model=StockSignalPrefsOut)
async def put_preferences(
    body: StockSignalPrefsIn, db: DbSession, current: CsrfProtected
) -> StockSignalPrefsOut:
    p = await notes.set_prefs(db, current.user.id, body.model_dump())
    await db.commit()
    return _prefs_out(p)


@router.get(
    "/stream",
    response_class=StreamingResponse,
    status_code=200,
    responses={200: {"content": {"text/event-stream": {}}}},
    openapi_extra=ui_meta(kind="stream"),
)
async def stream(
    request: Request,
    current: Current,
    settings: SettingsDep,
    last_event_id: Annotated[str | None, Header(max_length=32)] = None,
    cursor: Annotated[str | None, Query(alias="last_event_id", max_length=32)] = None,
) -> StreamingResponse:
    """Server-Sent Events: `notification` events (data = a notification, `id:` its id).
    Reconnecting with Last-Event-ID (or ?last_event_id=) replays what was missed; `hello`
    carries the cursor and unread count, `bye` ends the stream. Polling fallback:
    GET /notifications?after_id=."""
    gen = notes.stream(
        request.app.state.sessionmaker,
        current,
        last_event_id=last_event_id or cursor,
        poll_seconds=settings.STOCK_SIGNALS_STREAM_POLL_SECONDS,
        heartbeat_seconds=settings.STOCK_SIGNALS_STREAM_HEARTBEAT_SECONDS,
        max_seconds=settings.STOCK_SIGNALS_STREAM_MAX_SECONDS,
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
