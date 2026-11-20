"""Live market API (docs/38): quotes, chart, status, and the browser WebSocket.

| Method | Path | What |
| --- | --- | --- |
| GET | /api/trading/live/quotes | Latest live quotes with the previous-session change |
| GET | /api/trading/live/status | Market session, feed health, subscriptions, worker and hub |
| GET | /api/trading/live/protocol | The WebSocket contract (topics, limits, close codes, events) |
| GET | /api/trading/chart | Candles + EMA 8/32 + session VWAP + crossover markers + sessions |
| WS  | /api/trading/live/ws | Subscribe to quotes, candles, rankings and screener updates |

Every route needs a signed-in user (the session cookie, also on the WebSocket handshake).
Read-only: nothing here writes market data, signals or orders. The existing SSE streams
(/api/trading/stream, /api/trading/stock-signals/stream) are unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.agents.research import live_market as lm
from app.agents.trading.types import Timeframe
from app.api.ui import ui_meta, ui_module
from app.api.v1.market import _budget
from app.auth.deps import Current, CurrentSession, DbSession, SettingsDep
from app.auth.service import AuthService
from app.schemas.live import (
    ChartOut,
    LiveProtocolOut,
    LiveQuoteOut,
    LiveQuotesOut,
    LiveStatusOut,
    LiveTimeframe,
    MarketOut,
    WsClientMessage,
    WsTopicIn,
)
from app.services import dhan_feed_control as feed_ctl
from app.services import live_hub
from app.services import market_live as ml
from app.services.trading_stream import _session_ok

UI_MODULE = ui_module(
    "live-market", label="Live market", group="markets", icon="activity", order=21
)
router = APIRouter(prefix="/trading", tags=["live-market"])

CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN_ORIGIN = 4403
CLOSE_TOO_MANY = 4429
CLOSE_SLOW = 4008
CLOSE_SESSION_ENDED = 4001
CLOSE_POLICY = 1008
CLOSE_GOING_AWAY = 1001
CLOSE_CODES = {
    str(CLOSE_UNAUTHORIZED): "not signed in (no or expired session cookie)",
    str(CLOSE_FORBIDDEN_ORIGIN): "Origin not allowed (CORS_ORIGINS)",
    str(CLOSE_TOO_MANY): "too many live connections for this user",
    str(CLOSE_SLOW): "slow consumer: the client did not read fast enough; reconnect",
    str(CLOSE_SESSION_ENDED): "the session was signed out or expired",
    str(CLOSE_POLICY): "too many client messages, or malformed messages",
    str(CLOSE_GOING_AWAY): "the server is shutting down; reconnect",
}


def _market(now: datetime, settings: Any) -> MarketOut:
    return MarketOut.model_validate(ml.market(now, settings.DHAN_MARKET_HOLIDAYS))


# --- quotes -----------------------------------------------------------------------------------


@router.get("/live/quotes", response_model=LiveQuotesOut)
async def live_quotes(
    request: Request,
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    instrument_id: Annotated[list[int], Query(min_length=1, max_length=100)],
) -> LiveQuotesOut:
    """The newest live quote per instrument: LTP, day OHLC, volume, the previous trading
    session's close (and where it came from) and the change against it, with freshness.
    Instruments nobody watches are not on the feed: subscribe on the WebSocket (or list them
    in DHAN_FEED_INSTRUMENTS / a live universe) first."""
    await _budget(request, current.user.id, settings)
    ids = list(dict.fromkeys(instrument_id))
    insts = await ml.instruments_by_id(db, ids)
    refs = {i.broker_instrument_id: i for i in insts.values()}
    now = datetime.now(UTC)
    try:
        views = await ml.read_quotes(
            request.app.state.redis, list(refs), now=now, settings=settings
        )
    except Exception as exc:
        raise HTTPException(503, f"live quotes unavailable ({type(exc).__name__})") from exc
    items = []
    for ref, view in views.items():
        inst = refs[ref]
        items.append(LiveQuoteOut.model_validate({**view, **ml.info(inst).as_json(), "ref": ref}))
    have = {q.instrument_id for q in items}
    return LiveQuotesOut(
        items=items,
        missing=[i for i in ids if i not in have],
        market=_market(now, settings),
        as_of=now,
    )


# --- status -----------------------------------------------------------------------------------


@router.get("/live/status", response_model=LiveStatusOut)
async def live_status(request: Request, _: Current, settings: SettingsDep) -> LiveStatusOut:
    """Market session, the Dhan feed's health and subscription counts, the market-live worker's
    status record and this process's WebSocket hub."""
    now = datetime.now(UTC)
    redis = request.app.state.redis
    health = await feed_ctl.load_health(request.app.state.sessionmaker, redis, settings, now)
    raw, _readable, _crash = await feed_ctl.read_status(redis)
    worker: dict[str, Any] | None = None
    with contextlib.suppress(Exception):
        worker = await ml.read_json(redis, ml.STATUS_KEY)
    hub = getattr(request.app.state, "live_hub", None)
    subs = None
    if raw is not None:
        subs = {
            "dynamic": raw.get("dynamic"),
            "desired": raw.get("desired"),
            "subscribed": raw.get("subscribed"),
            "capacity": raw.get("capacity"),
            **(raw.get("subscriptions") or {}),
        }
    return LiveStatusOut(
        market=_market(now, settings),
        feed=health,
        subscriptions=subs,
        market_live=worker,
        hub=hub.stats() if hub is not None else {"connections": 0, "started": False},
    )


@router.get("/live/protocol", response_model=LiveProtocolOut)
async def live_protocol(_: Current, settings: SettingsDep) -> LiveProtocolOut:
    """The WebSocket contract: topics, limits, close codes and the event types."""
    return LiveProtocolOut(
        url="/api/trading/live/ws",
        topics={
            "quote:<instrument_id>": "live quote (coalesced: only the newest is kept)",
            "candle:<instrument_id>:<tf>": "live bars of 1m/5m/15m/1h/1d (forming coalesced)",
            "rankings:<universe>": "live gainers / losers / unusual volume of a universe",
            "screener:<tf>": "EMA 8/32 + VWAP state changes: live (forming bar) and confirmed",
        },
        limits={
            "max_topics_per_connection": settings.MARKET_WS_MAX_TOPICS,
            "max_connections_per_user": settings.MARKET_WS_MAX_CONNECTIONS_PER_USER,
            "outbox_max": settings.MARKET_WS_OUTBOX_MAX,
            "send_timeout_seconds": settings.MARKET_WS_SEND_TIMEOUT_SECONDS,
            "client_messages_per_minute": settings.MARKET_WS_MESSAGES_PER_MINUTE,
            "heartbeat_seconds": settings.MARKET_WS_HEARTBEAT_SECONDS,
        },
        close_codes=CLOSE_CODES,
        universes=list(settings.MARKET_LIVE_UNIVERSES),
        screener_timeframes=sorted(
            set(settings.MARKET_LIVE_SCREENER_TIMEFRAMES) | set(settings.STOCK_SIGNALS_TIMEFRAMES)
        ),
        candle_timeframes=[tf.value for tf in lm.LIVE_TIMEFRAMES],
        events={
            "hello": "first message: connection id, limits, market session",
            "ack": "reply to subscribe/unsubscribe/ping: accepted and rejected topics",
            "snapshot": "current state of a topic (after subscribe and after a resync)",
            "quote": "a live quote changed",
            "candle": "a live bar changed (closed=false) or closed (closed=true)",
            "rankings": "the live rankings of a universe were recomputed",
            "screener": "EMA/VWAP states changed (basis live or confirmed)",
            "heartbeat": "every heartbeat_seconds when idle: market and feed state",
            "status": "Redis fan-out lost (down) or back (ok, resync: snapshots follow)",
            "error": "a bad client message (the connection stays open)",
            "bye": "the server is closing the connection (reason)",
        },
    )


# --- chart ------------------------------------------------------------------------------------


@router.get(
    "/chart",
    response_model=ChartOut,
    openapi_extra=ui_meta(label="Chart", kind="detail"),
)
async def chart(
    request: Request,
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    instrument_id: int,
    timeframe: LiveTimeframe = "15m",
    limit: Annotated[int, Query(ge=10, le=2000)] = 300,
    include_live: bool = True,
) -> ChartOut:
    """Candles with EMA 8, EMA 32, session VWAP (intraday), volume, EMA crossover markers and
    session boundaries. History is stored Dhan candles; with `include_live`, today's live bars
    after the newest stored one are appended (`source: ticks`, `partial` when the server did not
    watch them from their start) and the forming bar is last (`is_closed: false`). Missing
    history is never filled. Continue with the `candle:<id>:<tf>` WebSocket topic."""
    await _budget(request, current.user.id, settings)
    insts = await ml.instruments_by_id(db, [instrument_id])
    inst = insts.get(instrument_id)
    if inst is None:
        raise HTTPException(404, "unknown Dhan instrument")
    data = await ml.chart(
        db,
        request.app.state.redis,
        inst,
        Timeframe(timeframe),
        limit=limit,
        include_live=include_live,
        settings=settings,
        now=datetime.now(UTC),
        fast=settings.STOCK_SIGNALS_EMA_FAST,
        slow=settings.STOCK_SIGNALS_EMA_SLOW,
    )
    return ChartOut.model_validate(data)


# --- the WebSocket ----------------------------------------------------------------------------


def origin_allowed(origin: str | None, host: str | None, settings: Any) -> bool:
    """Browsers always send Origin on a WebSocket handshake. Cookies ride along on cross-site
    handshakes too, so the Origin must be this host or one of CORS_ORIGINS (cross-site
    WebSocket hijacking). A client without Origin is not a browser page."""
    if not origin:
        return True
    allowed = {o.rstrip("/") for o in settings.CORS_ORIGINS}
    if origin.rstrip("/") in allowed:
        return True
    return bool(host) and urlsplit(origin).netloc == host


async def authenticate(websocket: WebSocket) -> CurrentSession | None:
    settings = websocket.app.state.settings
    token = websocket.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    async with websocket.app.state.sessionmaker() as db:
        svc = AuthService(db, timedelta(hours=settings.SESSION_TTL_HOURS))
        resolved = await svc.resolve(token)
    if resolved is None:
        return None
    session, user = resolved
    return CurrentSession(user=user, session=session, token=token)


async def session_still_valid(websocket: WebSocket, current: CurrentSession) -> bool:
    async with websocket.app.state.sessionmaker() as db:
        return await _session_ok(db, current.session.id)


async def lookup_instruments(websocket: WebSocket, ids: list[int]) -> dict[int, Any]:
    """Active Dhan instruments by id (only these can be subscribed)."""
    async with websocket.app.state.sessionmaker() as db:
        found = await ml.instruments_by_id(db, ids)
    return {i: inst for i, inst in found.items() if inst.is_active}


async def resolve_topics(
    websocket: WebSocket, wanted: list[WsTopicIn], settings: Any
) -> tuple[list[live_hub.Topic], list[dict[str, str]]]:
    ok: list[live_hub.Topic] = []
    bad: list[dict[str, str]] = []
    ids = [t.instrument_id for t in wanted if t.instrument_id is not None]
    insts = await lookup_instruments(websocket, ids) if ids else {}
    screener_tfs = set(settings.MARKET_LIVE_SCREENER_TIMEFRAMES) | set(
        settings.STOCK_SIGNALS_TIMEFRAMES
    )
    for t in wanted:
        if t.kind in ("quote", "candle"):
            name = f"{t.kind}:{t.instrument_id}" + (f":{t.timeframe}" if t.kind == "candle" else "")
            inst = insts.get(t.instrument_id) if t.instrument_id is not None else None
            if inst is None:
                bad.append({"topic": name, "reason": "unknown_instrument"})
                continue
            if t.kind == "candle" and t.timeframe is None:
                bad.append({"topic": name, "reason": "timeframe_required"})
                continue
            ref = inst.broker_instrument_id
            route = f"quote:{ref}" if t.kind == "quote" else f"candle:{t.timeframe}:{ref}"
            ok.append(
                live_hub.Topic(t.kind, name, route, instrument_id=inst.id, ref=ref, tf=t.timeframe)
            )
        elif t.kind == "rankings":
            name = f"rankings:{t.universe}"
            if t.universe not in settings.MARKET_LIVE_UNIVERSES:
                bad.append({"topic": name, "reason": "unknown_universe"})
                continue
            ok.append(live_hub.Topic("rankings", name, name, universe=t.universe))
        else:
            name = f"screener:{t.timeframe}"
            if t.timeframe not in screener_tfs:
                bad.append({"topic": name, "reason": "timeframe_not_screened"})
                continue
            ok.append(live_hub.Topic("screener", name, name, tf=t.timeframe))
    return ok, bad


async def _feed_brief(websocket: WebSocket, hub: live_hub.LiveHub) -> dict[str, Any]:
    from app.services import dhan_feed_health as fh

    try:
        st = fh.parse_status(await websocket.app.state.redis.get(fh.STATUS_KEY))
    except Exception:
        return {"connected": None, "redis": "down"}
    return {
        "connected": bool(st and st.get("connected")),
        "state": st.get("state") if st else None,
        "redis": "ok" if hub.redis_ok is not False else "down",
    }


@router.websocket("/live/ws")
async def live_ws(websocket: WebSocket) -> None:
    """See the module docstring and GET /api/trading/live/protocol."""
    settings = websocket.app.state.settings
    await websocket.accept()

    async def refuse(code: int, err: str, detail: str) -> None:
        with contextlib.suppress(Exception):
            await websocket.send_text(
                json.dumps({"type": "error", "code": err, "detail": detail, "seq": 0})
            )
            await websocket.close(code=code)

    if not origin_allowed(websocket.headers.get("origin"), websocket.headers.get("host"), settings):
        await refuse(CLOSE_FORBIDDEN_ORIGIN, "forbidden_origin", "Origin not allowed")
        return
    current = await authenticate(websocket)
    if current is None:
        await refuse(CLOSE_UNAUTHORIZED, "unauthorized", "sign in first")
        return
    hub = await live_hub.get_hub(websocket.app)
    conn = live_hub.Connection(
        current.user.id,
        websocket.send_text,
        outbox_max=settings.MARKET_WS_OUTBOX_MAX,
        send_timeout=settings.MARKET_WS_SEND_TIMEOUT_SECONDS,
        max_topics=settings.MARKET_WS_MAX_TOPICS,
    )
    if not await hub.add(conn):
        await refuse(CLOSE_TOO_MANY, "too_many_connections", "close another live tab first")
        return
    sender = asyncio.create_task(conn.run_sender(), name=f"live-ws-{conn.id}")
    now = datetime.now(UTC)
    conn.offer(
        {
            "type": "hello",
            "connection_id": conn.id,
            "server_time": now.isoformat(),
            "heartbeat_seconds": settings.MARKET_WS_HEARTBEAT_SECONDS,
            "max_topics": settings.MARKET_WS_MAX_TOPICS,
            "market": ml.market(now, settings.DHAN_MARKET_HOLIDAYS),
        }
    )
    window_start, window_n = time.monotonic(), 0
    last_check = time.monotonic()
    close_code = CLOSE_GOING_AWAY
    try:
        while True:
            recv = asyncio.ensure_future(websocket.receive_text())
            closed = asyncio.ensure_future(conn.closed.wait())
            done, _ = await asyncio.wait(
                {recv, closed},
                timeout=settings.MARKET_WS_HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if closed in done or conn.closed.is_set():
                recv.cancel()
                close_code = (
                    CLOSE_SLOW if conn.close_reason == "slow_consumer" else CLOSE_GOING_AWAY
                )
                break
            closed.cancel()
            if time.monotonic() - last_check >= settings.MARKET_WS_SESSION_CHECK_SECONDS:
                last_check = time.monotonic()
                if not await session_still_valid(websocket, current):
                    recv.cancel()
                    conn.offer({"type": "bye", "reason": "session_ended"})
                    close_code = CLOSE_SESSION_ENDED
                    conn.close("session_ended", drain=True)
                    break
            if recv not in done:
                recv.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await recv
                now = datetime.now(UTC)
                conn.offer(
                    {
                        "type": "heartbeat",
                        "server_time": now.isoformat(),
                        "market": ml.market(now, settings.DHAN_MARKET_HOLIDAYS),
                        "feed": await _feed_brief(websocket, hub),
                    },
                    coalesce="heartbeat",
                )
                continue
            text = recv.result()
            # Client message budget (fixed one-minute window).
            if time.monotonic() - window_start >= 60:
                window_start, window_n = time.monotonic(), 0
            window_n += 1
            if window_n > settings.MARKET_WS_MESSAGES_PER_MINUTE:
                conn.offer({"type": "bye", "reason": "rate_limited"})
                close_code = CLOSE_POLICY
                conn.close("rate_limited", drain=True)
                break
            await _handle_message(websocket, hub, conn, text, settings)
    except WebSocketDisconnect:
        close_code = 0
    except RuntimeError:  # the socket closed under us
        close_code = 0
    finally:
        if conn.close_reason == "slow_consumer":
            # Best effort: it may not be read, the close code says it too.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    websocket.send_text(json.dumps({"type": "bye", "reason": "slow_consumer"})),
                    0.5,
                )
        await hub.remove(conn)
        # Let the sender deliver a final bye before closing.
        with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(sender), 1.0)
        sender.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await sender
        if close_code:
            with contextlib.suppress(Exception):
                await websocket.close(code=close_code)


async def _handle_message(
    websocket: WebSocket,
    hub: live_hub.LiveHub,
    conn: live_hub.Connection,
    text: str,
    settings: Any,
) -> None:
    try:
        msg = WsClientMessage.model_validate_json(text)
    except ValidationError as exc:
        errors = exc.errors()
        first: dict[str, Any] = dict(errors[0]) if errors else {}
        conn.offer(
            {
                "type": "error",
                "code": "bad_message",
                "detail": f"{'.'.join(str(p) for p in first.get('loc', ()))}: "
                f"{first.get('msg', 'invalid')}"[:200],
            }
        )
        return
    if msg.op == "ping":
        conn.offer({"type": "ack", "op": "ping", "id": msg.id, "accepted": [], "rejected": []})
        return
    topics, rejected = await resolve_topics(websocket, msg.topics, settings)
    if msg.op == "subscribe":
        # The ack goes first; each accepted topic's snapshot follows it.
        names = [t.name for t in topics]
        pre_rejected = list(rejected)
        room = max(0, conn.max_topics - len(conn.topics))
        new = [t for t in topics if t.route not in conn.topics]
        over = {t.name for t in new[room:]}
        accepted = [n for n in names if n not in over]
        rejected = pre_rejected + [{"topic": n, "reason": "topic_limit"} for n in sorted(over)]
        conn.offer(
            {
                "type": "ack",
                "op": "subscribe",
                "id": msg.id,
                "accepted": accepted,
                "rejected": rejected,
            }
        )
        await hub.subscribe(conn, [t for t in topics if t.name not in over])
    else:
        names = [t.name for t in topics] + [r["topic"] for r in rejected]
        gone = await hub.unsubscribe(conn, names)
        conn.offer(
            {
                "type": "ack",
                "op": "unsubscribe",
                "id": msg.id,
                "accepted": gone,
                "rejected": [
                    {"topic": n, "reason": "not_subscribed"} for n in names if n not in gone
                ],
            }
        )
