"""MT5 bot push ingest + dashboard reads (docs/12-mt5-adapter.md §3).

    POST /api/trading/mt5/ingest        machine-to-machine: bearer MT5_INGEST_TOKEN, no session,
                                        no CSRF; 202 accepted / 200 duplicate / 401 / 413 / 415 /
                                        422 / 429 / 503
    GET  /api/trading/mt5/bot-status    status card (online / stale / error, last heartbeat)
    GET  /api/trading/mt5/market        per symbol: open/closed, session, last price, candles+EMA
    GET  /api/trading/mt5/positions     open positions with the guardian phase / SL / secured / peak
    GET  /api/trading/mt5/signals       timeline (filter by kind / symbol)
    GET  /api/trading/mt5/failures      classified failures with the "why"
    GET  /api/trading/mt5/strategy      strategy config the bot reported
    GET  /api/trading/mt5/report        P&L (points) per symbol/day, ema921e trades, backtests

Read-only: nothing here places, modifies or closes an order, or changes an account's mode.
The reads need a signed-in session (same as the other trading GETs); live updates arrive on
GET /api/trading/stream as `mt5` events.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.agents.trading.mt5.push import (
    BodyTooLarge,
    BodyUnsupported,
    PushInvalid,
    decode_body,
    parse_push,
)
from app.agents.trading.ratelimit import BrokerRateLimiter, RateLimitExceeded, Window
from app.api.ui import ui_module
from app.auth.deps import Current, DbSession, SettingsDep
from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.mt5 import (
    Mt5BotStatusOut,
    Mt5FailuresOut,
    Mt5IngestAccepted,
    Mt5IngestDuplicate,
    Mt5MarketOut,
    Mt5PositionsOut,
    Mt5ReportOut,
    Mt5SignalsOut,
    Mt5StrategyOut,
)
from app.services import mt5_dashboard
from app.services.mt5_ingest import TIMELINE_KINDS, DbIngestStore, IngestStore

log = get_logger(__name__)
UI_MODULE = ui_module("mt5", label="MT5", group="markets", icon="activity", order=24)
router = APIRouter(prefix="/trading/mt5", tags=["trading", "mt5"])

INGEST_PATH = "/api/trading/mt5/ingest"


def _err(code: int, key: str, detail: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(code, detail={"code": key, "detail": detail}, headers=headers)


def check_ingest_token(settings: Settings, authorization: str | None) -> None:
    """503 when ingest is off; 401 unless `Authorization: Bearer <MT5_INGEST_TOKEN>`.
    Constant-time: hmac.compare_digest on bytes, evaluated even for a wrong scheme."""
    token = settings.MT5_INGEST_TOKEN.get_secret_value() if settings.MT5_INGEST_TOKEN else ""
    if not token:
        raise _err(503, "ingest_disabled", "MT5 ingest is disabled (MT5_INGEST_TOKEN unset)")
    scheme, _, got = (authorization or "").partition(" ")
    match = hmac.compare_digest(got.strip().encode(), token.encode())
    if not (match and scheme.lower() == "bearer"):
        raise _err(
            401, "unauthorized", "bad or missing bearer token", {"WWW-Authenticate": "Bearer"}
        )


def _limiter(request: Request, settings: Settings) -> BrokerRateLimiter:
    lim: BrokerRateLimiter | None = getattr(request.app.state, "mt5_ingest_limiter", None)
    if lim is None:
        lim = BrokerRateLimiter(
            request.app.state.redis,
            "ratelimit:mt5-ingest",
            {"push": [Window(60, settings.MT5_INGEST_RATE_PER_MINUTE)]},
        )
        request.app.state.mt5_ingest_limiter = lim
    return lim


def _store(request: Request) -> IngestStore:
    store: IngestStore | None = getattr(request.app.state, "mt5_ingest_store", None)
    if store is None:
        store = DbIngestStore(request.app.state.sessionmaker)
        request.app.state.mt5_ingest_store = store
    return store


async def _read_capped(request: Request, cap: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > cap:
        raise BodyTooLarge("body too large")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > cap:
            raise BodyTooLarge("body too large")
    return bytes(buf)


@router.post(
    "/ingest",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Mt5IngestAccepted,
    responses={
        200: {"model": Mt5IngestDuplicate, "description": "push_id already ingested"},
        401: {"description": "bad or missing bearer token"},
        413: {"description": "body over 2 MiB, or over 8 MiB decompressed"},
        415: {"description": "Content-Encoding other than gzip"},
        422: {"description": "not aureon.mt5.push/v1"},
        429: {"description": "too many pushes"},
        503: {"description": "ingest disabled (MT5_INGEST_TOKEN unset) or storage unavailable"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    },
)
async def ingest(
    request: Request,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    content_encoding: Annotated[str | None, Header()] = None,
    x_aureon_push_id: Annotated[str | None, Header(max_length=64)] = None,
) -> Mt5IngestAccepted | JSONResponse:
    """The MT5 bot pushes one aureon.mt5.push/v1 document per status poll. Idempotent by
    push_id (a repeat answers 200 {"duplicate": true}) and per event id. Never touches orders."""
    check_ingest_token(settings, authorization)
    try:
        await _limiter(request, settings).acquire("push", max_wait=0)
    except RateLimitExceeded as exc:
        raise _err(
            429, "rate_limited", "too many pushes", {"Retry-After": str(max(1, int(exc.wait_s)))}
        ) from exc
    except Exception as exc:  # Redis down: the token still gates the route; fail open
        log.warning("mt5_ingest_ratelimit_unavailable", error=type(exc).__name__)
    try:
        raw = await _read_capped(request, settings.MT5_INGEST_MAX_BYTES)
        body = decode_body(raw, content_encoding, settings.MT5_INGEST_MAX_DECOMPRESSED_BYTES)
        push = parse_push(body)
    except BodyTooLarge as exc:
        raise _err(413, "too_large", str(exc)) from exc
    except BodyUnsupported as exc:
        raise _err(415, "unsupported_encoding", str(exc)) from exc
    except PushInvalid as exc:
        raise HTTPException(
            422, detail={"code": "invalid_push", "detail": exc.detail, "fields": exc.fields}
        ) from exc
    if x_aureon_push_id is not None:
        try:
            header_id = uuid.UUID(x_aureon_push_id)
        except ValueError:
            header_id = None
        if header_id != push.push_id:
            raise HTTPException(
                422,
                detail={
                    "code": "invalid_push",
                    "detail": "X-Aureon-Push-Id does not match body.push_id",
                    "fields": ["push_id"],
                },
            )
    try:
        res = await _store(request).store(push, len(raw), settings)
    except (SQLAlchemyError, OSError) as exc:
        log.error("mt5_ingest_store_failed", error=type(exc).__name__)
        raise _err(503, "storage_unavailable", "could not store the push; retry") from exc
    if res.duplicate:
        return JSONResponse(
            Mt5IngestDuplicate(push_id=res.push_id).model_dump(mode="json"), status_code=200
        )
    return Mt5IngestAccepted(
        push_id=res.push_id, events_new=res.events_new, events_duplicate=res.events_duplicate
    )


# --- dashboard reads (session) ----------------------------------------------------------------


@router.get("/bot-status", response_model=Mt5BotStatusOut)
async def bot_status(db: DbSession, _: Current, settings: SettingsDep) -> Mt5BotStatusOut:
    return await mt5_dashboard.bot_status(db, settings)


@router.get("/market", response_model=Mt5MarketOut)
async def market(
    db: DbSession,
    _: Current,
    timeframe: Annotated[str | None, Query(pattern=r"^[A-Za-z0-9]{1,4}$")] = None,
    limit: Annotated[int, Query(ge=1, le=300)] = 120,
) -> Mt5MarketOut:
    return await mt5_dashboard.market(db, timeframe, limit)


@router.get("/positions", response_model=Mt5PositionsOut)
async def positions(db: DbSession, _: Current, settings: SettingsDep) -> Mt5PositionsOut:
    return await mt5_dashboard.positions(db, settings)


@router.get("/signals", response_model=Mt5SignalsOut)
async def signals(
    db: DbSession,
    _: Current,
    kind: Annotated[list[str] | None, Query(max_length=20)] = None,
    symbol: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Mt5SignalsOut:
    """Journal timeline, newest first. `kind` (repeatable): P, cross, protected, secured, exit,
    closed, error, or a raw event name (position_seen, process_start, ...)."""
    kinds = [k[:32] for k in kind] if kind else None
    return await mt5_dashboard.signals(db, kinds=kinds, symbol=symbol, limit=limit, offset=offset)


@router.get("/failures", response_model=Mt5FailuresOut)
async def failures(
    db: DbSession,
    _: Current,
    settings: SettingsDep,
    include_resolved: bool = True,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> Mt5FailuresOut:
    return await mt5_dashboard.failures(
        db, settings, include_resolved=include_resolved, limit=limit
    )


@router.get("/strategy", response_model=Mt5StrategyOut)
async def strategy(db: DbSession, _: Current) -> Mt5StrategyOut:
    return await mt5_dashboard.strategy(db)


@router.get("/report", response_model=Mt5ReportOut)
async def report(
    db: DbSession, _: Current, days: Annotated[int, Query(ge=1, le=90)] = 7
) -> Mt5ReportOut:
    return await mt5_dashboard.report(db, days)


__all__ = ["INGEST_PATH", "TIMELINE_KINDS", "check_ingest_token", "router"]
