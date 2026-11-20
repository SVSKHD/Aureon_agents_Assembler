"""Stock Signals API (docs/37-stock-signals.md), under /api/trading/stock-signals.

Every route needs a signed-in user; writes need the CSRF token. Signals, rankings and the
portfolio map are computed by the research worker and the server, never in the browser.
Nothing here places, changes or cancels an order.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.api.ui import ui_meta, ui_module
from app.api.v1.market import _budget
from app.auth.deps import CsrfProtected, Current, DbSession, SettingsDep
from app.schemas.stock_signals import (
    NotificationKind,
    PortfolioMapOut,
    RankingCoverageOut,
    RankingOut,
    RankingScopeOut,
    RebalanceIn,
    RebalancePreviewOut,
    SignalState,
    SignalTimeframe,
    StockSignalDetailOut,
    StockSignalEventOut,
    StockSignalNotificationOut,
    StockSignalNotificationsOut,
    StockSignalPrefsIn,
    StockSignalPrefsOut,
    StockSignalReadAllIn,
    StockSignalsPage,
    StockSignalStateOut,
    StockSignalStatesPage,
    StockSignalUnreadOut,
)
from app.services import market_live as ml
from app.services import portfolio_map, stock_rankings
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


# --- signals, states and rankings ---------------------------------------------------------------
# Static paths come before /{signal_id} so they never parse as an id.

Exchange = Annotated[str | None, Query(pattern="^(NSE|BSE|nse|bse)$")]
Basis = Literal["confirmed", "live"]
RANK_NOTE = (
    "From the latest finished Stock Signals run (no Dhan call per request). Gainers and losers "
    "rank by % change from the previous session's last close; trending ranks by the 0-100 "
    "trend score, which orders trend strength and is not a probability of profit."
)


def _coverage(c: dict[str, object]) -> RankingCoverageOut:
    return RankingCoverageOut.model_validate(c)


@router.get("", response_model=StockSignalsPage)
async def list_signals(
    request: Request,
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    timeframe: SignalTimeframe | None = None,
    exchange: Exchange = None,
    symbol: Annotated[str | None, Query(min_length=1, max_length=40)] = None,
    direction: Literal["bullish", "bearish"] | None = None,
    confirmation: Literal["confirmed", "pending"] | None = None,
    in_holdings: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    min_trend_score: Annotated[Decimal | None, Query(ge=0, le=100)] = None,
    fresh_only: Annotated[
        bool, Query(description="Only instruments whose current data is usable")
    ] = False,
    sort: stock_rankings.EventSort = "-crossed_at",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StockSignalsPage:
    """EMA 8/32 crossover events (one per crossover; a VWAP confirmation updates it)."""
    for name, value in (("since", since), ("until", until)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(422, f"{name} must include a timezone (e.g. Z)")
    await _budget(request, current.user.id, settings)
    rows, total = await stock_rankings.list_events(
        db,
        timeframe=timeframe,
        exchange=exchange,
        symbol=symbol,
        direction=direction,
        confirmation=confirmation,
        in_holdings=in_holdings,
        since=since,
        until=until,
        min_trend_score=min_trend_score,
        fresh_only=fresh_only,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return StockSignalsPage(
        items=[StockSignalEventOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/states", response_model=StockSignalStatesPage)
async def list_states(
    request: Request,
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    timeframe: SignalTimeframe = "15m",
    state: Annotated[list[SignalState] | None, Query(max_length=8)] = None,
    exchange: Exchange = None,
    in_holdings: bool | None = None,
    include_unavailable: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> StockSignalStatesPage:
    """The latest EMA 8/32 + VWAP state of every evaluated instrument (best score first)."""
    await _budget(request, current.user.id, settings)
    rows, total = await stock_rankings.list_states(
        db,
        timeframe,
        states=state or [],
        exchange=exchange,
        in_holdings=in_holdings,
        include_unavailable=include_unavailable,
        limit=limit,
        offset=offset,
    )
    return StockSignalStatesPage(
        items=[StockSignalStateOut.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/market-movers", response_model=RankingOut)
async def market_movers(
    request: Request,
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    kind: Literal["gainers", "losers", "unusual_volume"] = "gainers",
    timeframe: SignalTimeframe = "15m",
    exchange: Exchange = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    basis: Basis = "confirmed",
    universe: Annotated[str | None, Query(max_length=40)] = None,
    min_price: Annotated[float | None, Query(ge=0)] = None,
    min_avg_turnover: Annotated[float | None, Query(ge=0)] = None,
    include_stale: bool = False,
) -> RankingOut:
    """Top gainers / losers by real % change, or unusual volume (relative volume at or above
    STOCK_SIGNALS_UNUSUAL_RVOL), among liquid instruments with usable data.

    `basis=confirmed` (default): from the latest finished Stock Signals run (closed candles).
    `basis=live`: from live quotes of a MARKET_LIVE_UNIVERSES `universe`, change against the
    previous session's close, relative volume adjusted for time of day; `min_price`,
    `min_avg_turnover` and `include_stale` override the liquidity defaults. `scope` says what
    the list covers and is never market-wide for a subset."""
    await _budget(request, current.user.id, settings)
    if basis == "live":
        return await _live_movers(
            request,
            db,
            settings,
            kind,
            timeframe,
            exchange,
            limit,
            universe,
            min_price,
            min_avg_turnover,
            include_stale,
        )
    if kind == "unusual_volume":
        rows = await stock_rankings.unusual_volume(
            db, timeframe, settings.STOCK_SIGNALS_UNUSUAL_RVOL, limit=limit, exchange=exchange
        )
    else:
        rows = await stock_rankings.movers(db, timeframe, kind, limit=limit, exchange=exchange)
    return RankingOut(
        kind=kind,
        coverage=_coverage(await stock_rankings.coverage(db, timeframe)),
        items=[StockSignalStateOut.model_validate(r) for r in rows],
        note=RANK_NOTE,
    )


@router.get("/trending", response_model=RankingOut)
async def trending(
    request: Request,
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    direction: Literal["bullish", "bearish"] = "bullish",
    timeframe: SignalTimeframe = "15m",
    exchange: Exchange = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    basis: Basis = "confirmed",
) -> RankingOut:
    """Top trending bullish (highest score) or bearish (lowest score) instruments whose EMAs
    agree with the direction. `basis=live` ranks the live EMA/VWAP state of the bar that is
    still forming (MARKET_LIVE_SCREENER_TIMEFRAMES only): it can change until the bar closes,
    and only the confirmed basis creates signals or notifications."""
    await _budget(request, current.user.id, settings)
    if basis == "live":
        return await _live_trending(request, db, settings, direction, timeframe, exchange, limit)
    rows = await stock_rankings.trending(db, timeframe, direction, limit=limit, exchange=exchange)
    return RankingOut(
        kind=f"trending_{direction}",
        coverage=_coverage(await stock_rankings.coverage(db, timeframe)),
        items=[StockSignalStateOut.model_validate(r) for r in rows],
        note=RANK_NOTE,
    )


# --- live rankings (docs/38) ---------------------------------------------------------------------

LIVE_NOTE = (
    "Live: ranked from live Dhan quotes of the named universe, change against the previous "
    "trading session's close (prev_close_source says where it came from). Only instruments the "
    "feed is subscribed to are ranked; `scope` says what the list covers. Confirmed signal "
    "fields (state, trend_score, EMAs) are from the latest closed-candle run."
)
LIVE_TREND_NOTE = (
    "Live: the EMA/VWAP state of the bar that is still forming, from stored candles plus today's "
    "live bars. It can change until the bar closes; confirmed signals come from closed candles."
)


async def _live_snapshot(request: Request, key: str) -> dict[str, Any] | None:
    try:
        snap = await ml.read_json(request.app.state.redis, key)
    except Exception as exc:
        raise HTTPException(503, f"live data unavailable ({type(exc).__name__})") from exc
    return snap if isinstance(snap, dict) else None


async def _live_movers(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    kind: str,
    timeframe: str,
    exchange: str | None,
    limit: int,
    universe: str | None,
    min_price: float | None,
    min_avg_turnover: float | None,
    include_stale: bool,
) -> RankingOut:
    universes = list(settings.MARKET_LIVE_UNIVERSES)
    name = universe or (universes[0] if universes else "")
    if name not in universes:
        raise HTTPException(422, f"universe must be one of MARKET_LIVE_UNIVERSES {universes}")
    snap = await _live_snapshot(request, ml.MOVERS_KEY.format(universe=name))
    if snap is None:
        raise HTTPException(
            503, "no live rankings yet: the market-live worker has not published this universe"
        )
    summary = ml.movers_summary(
        snap,
        kinds=(kind,),  # type: ignore[arg-type]
        limit=limit,
        min_price=settings.MARKET_LIVE_MIN_PRICE if min_price is None else min_price,
        min_avg_turnover=(
            settings.MARKET_LIVE_MIN_AVG_TURNOVER if min_avg_turnover is None else min_avg_turnover
        ),
        min_rvol=settings.STOCK_SIGNALS_UNUSUAL_RVOL,
        include_stale=include_stale,
        exchange=exchange,
    )
    rows = summary["top"][kind]
    confirmed = await ml.confirmed_states(
        db, [int(r["instrument_id"]) for r in rows if r.get("instrument_id")], timeframe
    )
    cov = summary["coverage"]
    return RankingOut(
        kind=kind,
        basis="live",
        coverage=RankingCoverageOut(
            timeframe=timeframe,
            run_id=None,
            data_as_of=summary.get("quotes_as_of"),
            universe_size=cov["universe_size"],
            evaluated=cov["evaluated"],
            unavailable=cov["no_data"],
            illiquid=cov["illiquid"],
            universe=name,
            with_quote=cov["with_quote"],
            live=cov["live"],
            stale=cov["stale"],
            closed=cov["closed"],
            no_data=cov["no_data"],
            no_prev_close=cov["no_prev_close"],
            liquidity_unknown=cov["liquidity_unknown"],
            quotes_as_of=summary.get("quotes_as_of"),
            computed_at=summary.get("computed_at"),
        ),
        scope=RankingScopeOut.model_validate(summary["scope"]),
        market=summary.get("market"),
        items=[
            StockSignalStateOut.model_validate(
                ml.live_mover_item(r, confirmed.get(int(r.get("instrument_id") or 0)), timeframe)
            )
            for r in rows
        ],
        note=LIVE_NOTE,
    )


async def _live_trending(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    direction: str,
    timeframe: str,
    exchange: str | None,
    limit: int,
) -> RankingOut:
    if timeframe not in settings.MARKET_LIVE_SCREENER_TIMEFRAMES:
        raise HTTPException(
            422,
            "the live state is computed for MARKET_LIVE_SCREENER_TIMEFRAMES "
            f"{settings.MARKET_LIVE_SCREENER_TIMEFRAMES} only",
        )
    snap = await _live_snapshot(request, ml.SCREENER_KEY.format(tf=timeframe))
    if snap is None:
        raise HTTPException(503, "no live state yet: the market-live worker has not published it")
    all_rows = list(snap.get("rows") or [])
    rows = ml.live_trending(all_rows, direction, limit=limit, exchange=exchange)
    confirmed = await ml.confirmed_states(
        db, [int(r["instrument_id"]) for r in rows if r.get("instrument_id")], timeframe
    )
    unavailable = sum(1 for r in all_rows if r.get("state") == "UNAVAILABLE")
    universes = ",".join(snap.get("universes") or [])
    usable = len(all_rows) - unavailable
    return RankingOut(
        kind=f"trending_{direction}",
        basis="live",
        coverage=RankingCoverageOut(
            timeframe=timeframe,
            run_id=None,
            data_as_of=snap.get("computed_at"),
            universe_size=len(all_rows),
            evaluated=usable,
            unavailable=unavailable,
            universe=universes or None,
            computed_at=snap.get("computed_at"),
        ),
        scope=RankingScopeOut(
            universe=universes or None,
            market_wide=False,
            capped=False,
            universe_total=len(all_rows),
            label=f"{universes} universe ({usable} of {len(all_rows)} instruments with a "
            "usable live state)",
        ),
        market=snap.get("market"),
        items=[
            StockSignalStateOut.model_validate(
                ml.live_state_item(r, confirmed.get(int(r.get("instrument_id") or 0)))
            )
            for r in rows
        ],
        note=LIVE_TREND_NOTE,
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


# --- portfolio signal map -------------------------------------------------------------------------


@router.get("/portfolio-map", response_model=PortfolioMapOut)
async def portfolio_map_view(
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    timeframe: SignalTimeframe = "15m",
) -> PortfolioMapOut:
    """Your Dhan holdings with value, P&L, allocation, cash, EMA/VWAP state and an advisory
    class (HOLD_CANDIDATE, MONITOR, REVIEW_RISK, POTENTIAL_ADD). Read-only; an admin sees
    every Dhan account. Classes come from deterministic rules, not from a model."""
    return PortfolioMapOut.model_validate(
        await portfolio_map.build(db, portfolio_map.viewer(current), settings, timeframe)
    )


@router.post("/rebalance-preview", response_model=RebalancePreviewOut)
async def rebalance_preview(
    body: RebalanceIn,
    request: Request,
    db: DbSession,
    current: CsrfProtected,
    settings: SettingsDep,
) -> RebalancePreviewOut:
    """A deterministic preview: trims of overweight holdings, liquid bullish alternatives,
    cash needed, Dhan delivery charges and the allocation after. Nothing is stored and no
    order is placed or drafted."""
    await _budget(request, current.user.id, settings)
    out = await portfolio_map.rebalance_preview(
        db,
        portfolio_map.viewer(current),
        settings,
        tf=body.timeframe,
        max_weight_pct=body.max_weight_pct or Decimal(str(settings.PORTFOLIO_MAX_WEIGHT_PCT)),
        cash_buffer_pct=(
            body.cash_buffer_pct
            if body.cash_buffer_pct is not None
            else Decimal(str(settings.PORTFOLIO_CASH_BUFFER_PCT))
        ),
        max_new=body.max_new if body.max_new is not None else settings.PORTFOLIO_CANDIDATES,
        min_trade_value=(
            body.min_trade_value
            if body.min_trade_value is not None
            else Decimal(str(settings.PORTFOLIO_MIN_TRADE_VALUE))
        ),
    )
    return RebalancePreviewOut.model_validate(out)


# Last: /{signal_id} would otherwise swallow the static paths above.
@router.get("/{signal_id}", response_model=StockSignalDetailOut)
async def get_signal(signal_id: int, db: DbSession, current: Current) -> StockSignalDetailOut:
    """One crossover event and the instrument's current state on that timeframe."""
    got = await stock_rankings.get_event(db, signal_id)
    if got is None:
        raise HTTPException(404, {"code": "not_found", "message": "signal not found"})
    return StockSignalDetailOut.model_validate(got)
