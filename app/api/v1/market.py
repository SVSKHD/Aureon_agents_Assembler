"""Market terminal endpoints (docs/35-market-data.md; ASTRA-FRONTEND docs/market-terminal.md).

All under /api/trading, cookie session required; every write needs the CSRF header.

- Screener: POST /screener/run, GET /screener/runs[/{id}], DELETE /screener/runs/{id},
  GET /screener/status. Runs read stored candles only: a scan never calls Dhan.
- Watchlists: owner-only CRUD (an admin cannot read another user's list either).
- Instruments: GET /instruments/status (instrument-master sync diagnostics).
- Indicators: GET /indicators/compute (on demand, same code and window as the screener).
- Candle history: POST/GET /candles/backfill (queued for the research worker under the shared
  Dhan rate limit).

Nothing here places, modifies or cancels an order.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.agents.registry import effective_state
from app.agents.research import indicators as ind
from app.agents.trading.types import Timeframe
from app.api.ui import ui_module
from app.auth.deps import CsrfProtected, Current, DbSession, SettingsDep
from app.core.logging import get_logger
from app.models.agents import Agent
from app.models.auth import ROLE_ADMIN
from app.models.trading import (
    CandleBackfillRequest,
    Instrument,
    ScreenerRun,
    Watchlist,
    WatchlistItem,
)
from app.schemas.common import Page
from app.schemas.market import (
    BackfillIn,
    BackfillOut,
    BackfillQueued,
    InstrumentStatus,
    ScreenerRequest,
    ScreenerResponse,
    ScreenerRunOut,
    ScreenerSort,
    ScreenerStatus,
    SortField,
    WatchlistIn,
    WatchlistItemIn,
    WatchlistItemOut,
    WatchlistOut,
    WatchlistPatch,
)
from app.schemas.trading import IndicatorOut, InstrumentOut
from app.services import market_data
from app.services import screener as svc
from app.services.market_data import MarketDataError
from app.services.research import TF_SECONDS
from app.services.screener import ScreenerError

log = get_logger(__name__)
UI_MODULE = ui_module(
    "market", label="Market terminal", group="markets", icon="scan-search", order=21
)
router = APIRouter(prefix="/trading", tags=["market"])
SCREENER_AGENT = "screener"
BACKFILL_AGENT = "candle-backfill"


def _err(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code, {"code": code, "message": message})


async def _budget(request: Request, user_id: uuid.UUID, settings: SettingsDep) -> None:
    """Per-user fixed-window budget for the heavy market endpoints
    (MARKET_USER_REQUESTS_PER_MINUTE). A Redis outage does not block reads."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return
    key = f"rl:market:{user_id}:{int(datetime.now(UTC).timestamp()) // 60}"
    try:
        n = await redis.incr(key)
        if n == 1:
            await redis.expire(key, 70)
    except Exception as exc:  # pragma: no cover - fail open on a Redis outage
        log.warning("market_budget_unavailable", error=type(exc).__name__)
        return
    if n > settings.MARKET_USER_REQUESTS_PER_MINUTE:
        raise _err(429, "rate_limited", "too many market requests this minute; retry shortly")


# --- screener -----------------------------------------------------------------------------


@router.post(
    "/screener/run",
    response_model=ScreenerResponse,
    responses={
        202: {"model": ScreenerResponse, "description": "queued: poll /screener/runs/{run_id}"},
        404: {"description": "watchlist not found (or not yours)"},
        422: {"description": "invalid filters, unsupported timeframe or universe too large"},
        429: {"description": "too many active scans or requests"},
    },
)
async def run_screener(
    body: ScreenerRequest,
    request: Request,
    db: DbSession,
    current: CsrfProtected,
    settings: SettingsDep,
) -> ScreenerResponse | JSONResponse:
    """Screen NSE/BSE instruments on stored candles. Universes up to SCREENER_INLINE_MAX are
    evaluated in the request (200, status `done`); larger ones are queued for the research
    worker (202, status `queued`, poll GET /screener/runs/{run_id}). A confirmed-candle scan
    repeated within SCREENER_CACHE_SECONDS and the same bar period returns the stored run."""
    owner = current.user.id
    await _budget(request, owner, settings)
    now = datetime.now(UTC)
    try:
        svc.check_supported(body)
        h = svc.request_hash(body, now)
        reuse = await svc.find_reusable(db, owner, h, settings, now)
        if reuse is not None:
            out = await svc.response_for(
                db, reuse, body.sort, body.limit, body.offset, cached=reuse.status == "done"
            )
            return out if reuse.status == "done" else _accepted(out)
        if await svc.active_runs(db, owner) >= settings.SCREENER_USER_ACTIVE_MAX:
            raise ScreenerError(
                429,
                "too_many_active_scans",
                f"at most {settings.SCREENER_USER_ACTIVE_MAX} scans may run at once "
                "(SCREENER_USER_ACTIVE_MAX); cancel one or wait",
            )
        ids = await svc.resolve_universe(db, body, owner, settings)
    except ScreenerError as exc:
        raise _err(exc.status, exc.code, exc.message) from exc
    inline = len(ids) <= settings.SCREENER_INLINE_MAX
    run = await svc.create_run(
        db, body, owner, h, len(ids), status="running" if inline else "queued"
    )
    run_id = run.id
    await db.commit()
    if inline:
        await svc.execute_run(request.app.state.sessionmaker, run_id, settings, now=now)
    db.expire_all()
    done = await db.get(ScreenerRun, run_id)
    assert done is not None
    out = await svc.response_for(db, done, body.sort, body.limit, body.offset)
    return out if done.status != "queued" else _accepted(out)


def _accepted(out: ScreenerResponse) -> JSONResponse:
    return JSONResponse(status_code=202, content=out.model_dump(mode="json"))


async def _own_run(db: DbSession, run_id: uuid.UUID, owner: uuid.UUID) -> ScreenerRun:
    run = await db.get(ScreenerRun, run_id)
    if run is None or run.owner_user_id != owner:
        raise _err(404, "run_not_found", "screener run not found")
    return run


@router.get("/screener/runs", response_model=Page[ScreenerRunOut])
async def list_runs(
    db: DbSession,
    current: Current,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ScreenerRunOut]:
    stmt = select(ScreenerRun).where(ScreenerRun.owner_user_id == current.user.id)
    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        await db.scalars(stmt.order_by(ScreenerRun.created_at.desc()).limit(limit).offset(offset))
    ).all()
    return Page[ScreenerRunOut](
        items=[_run_out(r) for r in rows], total=total, limit=limit, offset=offset
    )


def _run_out(r: ScreenerRun) -> ScreenerRunOut:
    return ScreenerRunOut(
        id=r.id,
        status=r.status,
        timeframe=r.timeframe,
        universe_size=r.universe_size,
        scanned=r.scanned,
        matched=r.matched,
        skipped={k: int(v) for k, v in (r.skipped or {}).items()},
        data_as_of=r.data_as_of,
        error=r.error,
        created_at=r.created_at,
        started_at=r.started_at,
        finished_at=r.finished_at,
    )


@router.get("/screener/runs/{run_id}", response_model=ScreenerResponse)
async def get_run(
    run_id: uuid.UUID,
    db: DbSession,
    current: Current,
    sort_field: Annotated[SortField, Query(alias="sort")] = "change_pct",
    sort_dir: Annotated[Literal["asc", "desc"], Query(alias="dir")] = "desc",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> ScreenerResponse:
    """A run's state and (once `done`) one page of its matches, sorted server-side."""
    run = await _own_run(db, run_id, current.user.id)
    return await svc.response_for(
        db, run, ScreenerSort(field=sort_field, dir=sort_dir), limit, offset
    )


@router.delete("/screener/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_run(run_id: uuid.UUID, db: DbSession, current: CsrfProtected) -> Response:
    """Cancel a queued or running scan (a running one stops at its next chunk)."""
    run = await _own_run(db, run_id, current.user.id)
    if run.status in svc.ACTIVE:
        values: dict[str, object] = {"cancel_requested": True}
        if run.status == "queued":
            values.update(status="cancelled", finished_at=func.now())
        await db.execute(update(ScreenerRun).where(ScreenerRun.id == run.id).values(**values))
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/screener/status", response_model=ScreenerStatus)
async def screener_status(db: DbSession, current: Current, settings: SettingsDep) -> ScreenerStatus:
    last = await db.scalar(
        select(ScreenerRun)
        .where(ScreenerRun.owner_user_id == current.user.id, ScreenerRun.status == "done")
        .order_by(ScreenerRun.finished_at.desc())
        .limit(1)
    )
    agent = await db.get(Agent, SCREENER_AGENT)
    worker: Literal["ok", "stale", "down"] = "down"
    if agent is not None:
        state = effective_state(agent, settings.AGENT_STALE_SECONDS)
        worker = "stale" if state in ("stale", "paused") else "ok"
    queued = int(
        await db.scalar(
            select(func.count()).select_from(ScreenerRun).where(ScreenerRun.status == "queued")
        )
        or 0
    )
    tfs = sorted(svc.SUPPORTED_TIMEFRAMES, key=lambda t: TF_SECONDS[t])
    return ScreenerStatus(
        last_run_at=last.finished_at if last else None,
        universe_size=last.universe_size if last else None,
        candles_as_of=await market_data.candles_as_of(db, tfs),
        worker=worker,
        queued_runs=queued,
        queued_backfills=await market_data.backfill_counts(db),
    )


# --- watchlists ---------------------------------------------------------------------------


async def _own_watchlist(db: DbSession, wl_id: uuid.UUID, owner: uuid.UUID) -> Watchlist:
    wl = await db.get(Watchlist, wl_id)
    if wl is None or wl.owner_user_id != owner:
        raise _err(404, "watchlist_not_found", "watchlist not found")
    return wl


async def _watchlists_out(db: DbSession, lists: list[Watchlist]) -> list[WatchlistOut]:
    if not lists:
        return []
    rows = (
        await db.execute(
            select(WatchlistItem, Instrument)
            .join(Instrument, Instrument.id == WatchlistItem.instrument_id)
            .where(WatchlistItem.watchlist_id.in_([w.id for w in lists]))
            .order_by(WatchlistItem.position, WatchlistItem.added_at)
        )
    ).all()
    items: dict[uuid.UUID, list[WatchlistItemOut]] = {w.id: [] for w in lists}
    for it, inst in rows:
        items[it.watchlist_id].append(
            WatchlistItemOut(
                instrument_id=it.instrument_id,
                added_at=it.added_at,
                position=it.position,
                instrument=InstrumentOut.model_validate(inst) if inst.is_active else None,
            )
        )
    return [
        WatchlistOut(
            id=w.id,
            name=w.name,
            description=w.description,
            items=items[w.id],
            created_at=w.created_at,
            updated_at=w.updated_at,
        )
        for w in lists
    ]


@router.get("/watchlists", response_model=list[WatchlistOut])
async def list_watchlists(db: DbSession, current: Current) -> list[WatchlistOut]:
    lists = list(
        (
            await db.scalars(
                select(Watchlist)
                .where(Watchlist.owner_user_id == current.user.id)
                .order_by(Watchlist.name)
            )
        ).all()
    )
    return await _watchlists_out(db, lists)


@router.post("/watchlists", response_model=WatchlistOut, status_code=status.HTTP_201_CREATED)
async def create_watchlist(
    body: WatchlistIn, db: DbSession, current: CsrfProtected, settings: SettingsDep
) -> WatchlistOut:
    owner = current.user.id
    n = int(
        await db.scalar(
            select(func.count()).select_from(Watchlist).where(Watchlist.owner_user_id == owner)
        )
        or 0
    )
    if n >= settings.WATCHLIST_MAX_PER_USER:
        raise _err(
            409, "too_many_watchlists", f"at most {settings.WATCHLIST_MAX_PER_USER} watchlists"
        )
    wl = Watchlist(owner_user_id=owner, name=body.name, description=body.description)
    db.add(wl)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _err(409, "duplicate_name", "you already have a watchlist with that name") from exc
    await db.commit()
    await db.refresh(wl)
    return (await _watchlists_out(db, [wl]))[0]


@router.get("/watchlists/{watchlist_id}", response_model=WatchlistOut)
async def get_watchlist(watchlist_id: uuid.UUID, db: DbSession, current: Current) -> WatchlistOut:
    wl = await _own_watchlist(db, watchlist_id, current.user.id)
    return (await _watchlists_out(db, [wl]))[0]


@router.patch("/watchlists/{watchlist_id}", response_model=WatchlistOut)
async def patch_watchlist(
    watchlist_id: uuid.UUID, body: WatchlistPatch, db: DbSession, current: CsrfProtected
) -> WatchlistOut:
    wl = await _own_watchlist(db, watchlist_id, current.user.id)
    fields = body.model_fields_set
    if "name" in fields and body.name is not None:
        wl.name = body.name
    if "description" in fields:
        wl.description = body.description
    wl.updated_at = datetime.now(UTC)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _err(409, "duplicate_name", "you already have a watchlist with that name") from exc
    await db.commit()
    await db.refresh(wl)
    return (await _watchlists_out(db, [wl]))[0]


@router.delete("/watchlists/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watchlist(
    watchlist_id: uuid.UUID, db: DbSession, current: CsrfProtected
) -> Response:
    wl = await _own_watchlist(db, watchlist_id, current.user.id)
    await db.delete(wl)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/watchlists/{watchlist_id}/items",
    response_model=WatchlistItemOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_watchlist_item(
    watchlist_id: uuid.UUID,
    body: WatchlistItemIn,
    db: DbSession,
    current: CsrfProtected,
    settings: SettingsDep,
) -> WatchlistItemOut:
    """Add an instrument (409 if already present). Adding never starts a feed subscription."""
    wl = await _own_watchlist(db, watchlist_id, current.user.id)
    inst = await db.get(Instrument, body.instrument_id)
    if inst is None or not inst.is_active:
        raise _err(404, "instrument_not_found", "instrument not found or inactive")
    n, top = (
        await db.execute(
            select(func.count(), func.coalesce(func.max(WatchlistItem.position), -1)).where(
                WatchlistItem.watchlist_id == wl.id
            )
        )
    ).one()
    if await db.get(WatchlistItem, (wl.id, inst.id)) is not None:
        raise _err(409, "already_present", "instrument already in this watchlist")
    if int(n) >= settings.WATCHLIST_MAX_ITEMS:
        raise _err(409, "watchlist_full", f"at most {settings.WATCHLIST_MAX_ITEMS} items")
    item = WatchlistItem(watchlist_id=wl.id, instrument_id=inst.id, position=int(top) + 1)
    db.add(item)
    wl.updated_at = datetime.now(UTC)
    try:
        await db.flush()
    except IntegrityError as exc:  # a concurrent add of the same instrument
        await db.rollback()
        raise _err(409, "already_present", "instrument already in this watchlist") from exc
    await db.commit()
    await db.refresh(item)
    return WatchlistItemOut(
        instrument_id=item.instrument_id,
        added_at=item.added_at,
        position=item.position,
        instrument=InstrumentOut.model_validate(inst),
    )


@router.delete(
    "/watchlists/{watchlist_id}/items/{instrument_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_watchlist_item(
    watchlist_id: uuid.UUID, instrument_id: int, db: DbSession, current: CsrfProtected
) -> Response:
    wl = await _own_watchlist(db, watchlist_id, current.user.id)
    item = await db.get(WatchlistItem, (wl.id, instrument_id))
    if item is None:
        raise _err(404, "item_not_found", "instrument is not in this watchlist")
    await db.delete(item)
    wl.updated_at = datetime.now(UTC)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- instruments + indicators -------------------------------------------------------------


@router.get("/instruments/status", response_model=InstrumentStatus)
async def instruments_status(db: DbSession, _: Current, settings: SettingsDep) -> InstrumentStatus:
    """Instrument-master freshness: last successful sync, active count per segment, and the
    last run's error. `stale` once no sync succeeded within DHAN_INSTRUMENT_MAX_AGE_HOURS."""
    return await market_data.instrument_status(db, settings)


@router.get("/indicators/compute", response_model=list[IndicatorOut])
async def compute_indicators(
    request: Request,
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    instrument_id: int,
    timeframe: Timeframe,
    specs: Annotated[
        str,
        Query(
            min_length=1,
            max_length=400,
            description="comma-separated, max 12: ema:8,ema:32,rsi:14,adx:14,atr:14,sma:50,"
            "macd:12:26:9,bb:20:2,vwap",
        ),
    ],
    limit: Annotated[int, Query(ge=1, le=2000)] = 200,
    candle: Literal["confirmed", "live"] = "confirmed",
) -> list[IndicatorOut]:
    """Indicators computed now from stored candles with the screener's code and window
    (newest max(SCREENER_LOOKBACK_BARS, warm-up) real candles), returned for the newest
    `limit` bars (at most that window). `confirmed` excludes a still-forming candle."""
    await _budget(request, current.user.id, settings)
    names = list(dict.fromkeys(s.strip() for s in specs.split(",") if s.strip()))
    if not names or len(names) > 12:
        raise _err(422, "bad_specs", "between 1 and 12 indicator specs")
    try:
        parsed = [ind.parse_spec(s) for s in names]
    except ind.SpecError as exc:
        raise _err(422, "bad_specs", str(exc)) from exc
    if timeframe is Timeframe.D1 and any(p.kind == "vwap" for p in parsed):
        raise _err(422, "bad_specs", "vwap needs an intraday timeframe (it resets daily)")
    inst = await db.get(Instrument, instrument_id)
    if inst is None:
        raise _err(404, "instrument_not_found", "instrument not found")
    rows = await market_data.compute_on_demand(
        db, inst, timeframe, parsed, limit=limit, candle=candle, settings=settings
    )
    return [IndicatorOut.model_validate(r) for r in rows]


# --- candle history -----------------------------------------------------------------------


@router.post(
    "/candles/backfill",
    response_model=BackfillQueued,
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_backfill(
    body: BackfillIn,
    request: Request,
    db: DbSession,
    current: CsrfProtected,
    settings: SettingsDep,
) -> BackfillQueued:
    """Queue Dhan history for up to 200 instruments. Worked by the research worker's
    `candle-backfill` lane under the shared Dhan rate limit; only missing bars are fetched.
    Instruments Dhan cannot serve (inactive/expired, unsupported segment) are `rejected`."""
    await _budget(request, current.user.id, settings)
    try:
        out = await market_data.enqueue_backfill(
            db,
            body.instrument_ids,
            body.timeframe,
            body.days,
            settings,
            source="user",
            requested_by=current.user.id,
        )
    except MarketDataError as exc:
        raise _err(exc.status, exc.code, exc.message) from exc
    await db.commit()
    return out


@router.get("/candles/backfill", response_model=Page[BackfillOut])
async def list_backfill(
    db: DbSession,
    current: Current,
    instrument_id: int | None = None,
    status_filter: Annotated[
        Literal["queued", "running", "done", "failed", "cancelled"] | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[BackfillOut]:
    """Your history requests; an admin also sees universe/system requests."""
    stmt = select(CandleBackfillRequest)
    if current.user.role.name != ROLE_ADMIN:
        stmt = stmt.where(CandleBackfillRequest.requested_by == current.user.id)
    if instrument_id is not None:
        stmt = stmt.where(CandleBackfillRequest.instrument_id == instrument_id)
    if status_filter is not None:
        stmt = stmt.where(CandleBackfillRequest.status == status_filter)
    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        await db.scalars(
            stmt.order_by(CandleBackfillRequest.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()
    return Page[BackfillOut](
        items=[BackfillOut.model_validate(r) for r in rows], total=total, limit=limit, offset=offset
    )
