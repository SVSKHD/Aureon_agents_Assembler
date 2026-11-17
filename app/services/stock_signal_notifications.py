"""Stock Signals notifications (docs/37 §5): persisted, per user, created by the research
worker after each run, so they arrive whether or not anyone has the screen open.

Who is notified about a crossover on instrument X:
- every user with X on one of their watchlists,
- every user whose preferences ask for all market crossovers (`all_market`),
- the holders of X (Dhan holdings, see portfolio_map), passed in as `holders`,
filtered by each user's preferences (on/off, kinds, timeframes, confirmed only).

Kinds: bullish_cross / bearish_cross (a new crossover), vwap_confirmed (a pending crossover
the VWAP has now confirmed), holding_reversal, portfolio_trend_change, replacement_candidate
(the portfolio ones come from the portfolio map). Each notification has a dedup key unique
per user, so a retried run or a second worker never notifies twice.

The stream (GET /api/trading/stock-signals/stream) is Server-Sent Events polling this table:
its `id:` is the notification id, so reconnecting with Last-Event-ID replays what was missed;
GET /notifications?after_id= is the polling fallback.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.trading.dhan.parse import IST
from app.auth.deps import CurrentSession
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.stock_signals import (
    StockSignalEvent,
    StockSignalNotification,
    StockSignalPrefs,
)
from app.models.trading import Instrument, Watchlist, WatchlistItem
from app.services.trading_stream import PING, _session_ok, sse

log = get_logger("stock_signal_notifications")

KINDS = (
    "bullish_cross",
    "bearish_cross",
    "vwap_confirmed",
    "holding_reversal",
    "portfolio_trend_change",
    "replacement_candidate",
)
CONFIRMED_KINDS = {"vwap_confirmed"}
BATCH = 100


def deep_link(event_id: int | None, instrument_id: int | None = None) -> str:
    if event_id is not None:
        return f"/trading/signals/{event_id}"
    return (
        f"/trading/signals?instrument_id={instrument_id}" if instrument_id else "/trading/signals"
    )


def _s(v: Decimal | None) -> str | None:
    return None if v is None else format(v.normalize(), "f")


def _iso(t: datetime | None) -> str | None:
    return None if t is None else t.isoformat()


def _hhmm(t: datetime | None) -> str:
    return "" if t is None else t.astimezone(IST).strftime("%d %b %H:%M IST")


def event_payload(ev: StockSignalEvent, inst: Instrument, kind: str) -> dict[str, Any]:
    return {
        "signal_id": ev.id,
        "kind": kind,
        "instrument": {
            "id": inst.id,
            "symbol": inst.trading_symbol or inst.symbol,
            "name": inst.display_name or inst.name,
            "exchange": inst.exchange,
            "segment": inst.segment,
        },
        "timeframe": ev.timeframe,
        "direction": ev.direction,
        "state": ev.state,
        "prices": {
            "last_price": _s(ev.last_price),
            "ema_fast": _s(ev.ema_fast),
            "ema_slow": _s(ev.ema_slow),
            "vwap": _s(ev.vwap),
            "confirm_price": _s(ev.confirm_price),
            "confirm_vwap": _s(ev.confirm_vwap),
        },
        "crossed_at": _iso(ev.crossed_at),
        "confirmed_at": _iso(ev.confirmed_at),
        "data_as_of": _iso(ev.data_as_of),
        "data_source": ev.data_source,
        "trend_score": _s(ev.trend_score),
        "deep_link": deep_link(ev.id),
    }


def event_text(ev: StockSignalEvent, inst: Instrument, kind: str) -> tuple[str, str]:
    sym = inst.trading_symbol or inst.symbol
    up = ev.direction == "bullish"
    side, rel = ("above", "above") if up else ("below", "below")
    if kind == "vwap_confirmed":
        title = f"{sym}: {ev.direction} EMA cross confirmed by VWAP ({ev.timeframe})"
        body = (
            f"The {ev.direction} EMA 8/32 cross of {_hhmm(ev.crossed_at)} is confirmed: the "
            f"{_hhmm(ev.confirmed_at)} candle closed at {_s(ev.confirm_price)}, {rel} the "
            f"session VWAP {_s(ev.confirm_vwap)}."
        )
        return title, body
    state = "confirmed" if ev.confirmed_at else "awaiting VWAP confirmation"
    title = f"{sym}: {ev.direction} EMA 8/32 cross ({ev.timeframe}), {state}"
    body = (
        f"EMA 8 {_s(ev.ema_fast)} crossed {side} EMA 32 {_s(ev.ema_slow)} on the candle that "
        f"closed {_hhmm(ev.crossed_at)} (close {_s(ev.last_price)}, session VWAP "
        f"{_s(ev.vwap) or 'n/a'})."
    )
    return title, body


class Prefs:
    """A user's preferences, or the defaults when they never saved any."""

    def __init__(self, row: StockSignalPrefs | None) -> None:
        self.enabled = True if row is None else row.enabled
        self.kinds = set(row.kinds or []) if row else set()
        self.timeframes = set(row.timeframes or []) if row else set()
        self.all_market = False if row is None else row.all_market
        self.confirmed_only = False if row is None else row.confirmed_only

    def wants(self, kind: str, timeframe: str | None, confirmed: bool) -> bool:
        if not self.enabled:
            return False
        if self.kinds and kind not in self.kinds:
            return False
        if self.timeframes and timeframe and timeframe not in self.timeframes:
            return False
        return not (
            self.confirmed_only and kind in ("bullish_cross", "bearish_cross") and not confirmed
        )


async def _prefs(db: AsyncSession, users: Iterable[uuid.UUID]) -> dict[uuid.UUID, Prefs]:
    ids = set(users)
    rows = (
        (await db.scalars(select(StockSignalPrefs).where(StockSignalPrefs.user_id.in_(ids)))).all()
        if ids
        else []
    )
    by = {r.user_id: r for r in rows}
    return {u: Prefs(by.get(u)) for u in ids}


async def watchers(db: AsyncSession, instrument_ids: Sequence[int]) -> dict[int, set[uuid.UUID]]:
    out: dict[int, set[uuid.UUID]] = {}
    if not instrument_ids:
        return out
    rows = await db.execute(
        select(WatchlistItem.instrument_id, Watchlist.owner_user_id)
        .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
        .where(WatchlistItem.instrument_id.in_(instrument_ids))
        .distinct()
    )
    for iid, uid in rows:
        out.setdefault(int(iid), set()).add(uid)
    return out


async def all_market_users(db: AsyncSession) -> set[uuid.UUID]:
    rows = await db.scalars(
        select(StockSignalPrefs.user_id).where(
            StockSignalPrefs.enabled.is_(True), StockSignalPrefs.all_market.is_(True)
        )
    )
    return set(rows.all())


async def insert_many(db: AsyncSession, rows: Sequence[Mapping[str, Any]]) -> int:
    """Insert notifications, skipping any whose (user, dedup key) already exists."""
    if not rows:
        return 0
    stmt = (
        insert(StockSignalNotification)
        .values(list(rows))
        .on_conflict_do_nothing(constraint="uq_stock_signal_notifications_dedup")
        .returning(StockSignalNotification.id)
    )
    return len((await db.execute(stmt)).all())


async def notify_events(
    maker: async_sessionmaker[AsyncSession],
    new_events: Sequence[int],
    confirmed_events: Sequence[int],
    *,
    holders: Mapping[int, set[uuid.UUID]] | None = None,
) -> int:
    """Notifications for a run's new crossovers and VWAP confirmations. Idempotent."""
    wanted = {e: "cross" for e in new_events} | {e: "vwap_confirmed" for e in confirmed_events}
    if not wanted:
        return 0
    async with maker() as db, db.begin():
        pairs = (
            await db.execute(
                select(StockSignalEvent, Instrument)
                .join(Instrument, Instrument.id == StockSignalEvent.instrument_id)
                .where(StockSignalEvent.id.in_(wanted))
            )
        ).all()
        iids = sorted({ev.instrument_id for ev, _ in pairs})
        watch = await watchers(db, iids)
        everyone = await all_market_users(db)
        hold = holders or {}
        recipients = {iid: watch.get(iid, set()) | everyone | hold.get(iid, set()) for iid in iids}
        prefs = await _prefs(db, set().union(*recipients.values()) if recipients else set())
        rows = []
        for ev, inst in pairs:
            kind = wanted[ev.id]
            if kind == "cross":
                kind = "bullish_cross" if ev.direction == "bullish" else "bearish_cross"
            title, body = event_text(ev, inst, kind)
            payload = event_payload(ev, inst, kind)
            for uid in recipients.get(ev.instrument_id, set()):
                if not prefs[uid].wants(kind, ev.timeframe, ev.confirmed_at is not None):
                    continue
                rows.append(
                    {
                        "user_id": uid,
                        "kind": kind,
                        "event_id": ev.id,
                        "instrument_id": inst.id,
                        "timeframe": ev.timeframe,
                        "title": title[:200],
                        "body": body,
                        "payload": payload,
                        "dedup_key": f"{kind}:{ev.id}",
                    }
                )
        return await insert_many(db, rows)


# --- reading ------------------------------------------------------------------------------------


async def unread_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(StockSignalNotification)
            .where(
                StockSignalNotification.user_id == user_id,
                StockSignalNotification.read_at.is_(None),
            )
        )
        or 0
    )


async def list_notifications(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    unread_only: bool = False,
    before_id: int | None = None,
    after_id: int | None = None,
    kind: str | None = None,
    instrument_id: int | None = None,
    timeframe: str | None = None,
    limit: int = 50,
) -> list[StockSignalNotification]:
    """Newest first; with `after_id` (the polling fallback for the stream), oldest first."""
    n = StockSignalNotification
    q = select(n).where(n.user_id == user_id)
    if unread_only:
        q = q.where(n.read_at.is_(None))
    if before_id is not None:
        q = q.where(n.id < before_id)
    if kind:
        q = q.where(n.kind == kind)
    if instrument_id is not None:
        q = q.where(n.instrument_id == instrument_id)
    if timeframe:
        q = q.where(n.timeframe == timeframe)
    if after_id is not None:
        q = q.where(n.id > after_id).order_by(n.id.asc())
    else:
        q = q.order_by(n.id.desc())
    return list((await db.scalars(q.limit(limit))).all())


async def mark_read(
    db: AsyncSession, user_id: uuid.UUID, notification_id: int
) -> StockSignalNotification | None:
    row = await db.scalar(
        select(StockSignalNotification).where(
            StockSignalNotification.id == notification_id,
            StockSignalNotification.user_id == user_id,  # another user's id is "not found"
        )
    )
    if row is not None and row.read_at is None:
        row.read_at = datetime.now(UTC)
    return row


async def mark_all_read(db: AsyncSession, user_id: uuid.UUID, up_to_id: int | None) -> int:
    q = update(StockSignalNotification).where(
        StockSignalNotification.user_id == user_id, StockSignalNotification.read_at.is_(None)
    )
    if up_to_id is not None:
        q = q.where(StockSignalNotification.id <= up_to_id)
    res = await db.execute(q.values(read_at=func.now()))
    return int(res.rowcount or 0)  # type: ignore[attr-defined]


async def get_prefs(db: AsyncSession, user_id: uuid.UUID) -> Prefs:
    return Prefs(await db.get(StockSignalPrefs, user_id))


async def set_prefs(db: AsyncSession, user_id: uuid.UUID, values: Mapping[str, Any]) -> Prefs:
    stmt = insert(StockSignalPrefs).values(user_id=user_id, **values)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["user_id"], set_={**values, "updated_at": func.now()}
        )
    )
    return Prefs(await db.get(StockSignalPrefs, user_id, populate_existing=True))


# --- stream ---------------------------------------------------------------------------------------


def parse_cursor(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    return int(value) if value.isdigit() and len(value) <= 19 else None


def notification_json(row: StockSignalNotification) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "event_id": row.event_id,
        "instrument_id": row.instrument_id,
        "timeframe": row.timeframe,
        "title": row.title,
        "body": row.body,
        "payload": row.payload or {},
        "created_at": _iso(row.created_at),
        "read_at": _iso(row.read_at),
    }


async def stream(
    maker: async_sessionmaker[AsyncSession],
    current: CurrentSession,
    *,
    last_event_id: str | None,
    poll_seconds: float,
    heartbeat_seconds: float,
    max_seconds: float,
) -> AsyncIterator[str]:
    """This user's notifications after the cursor, oldest first, each with its id. Without a
    cursor it starts after the newest one (history comes from GET /notifications). A database
    error skips that poll instead of ending the stream."""
    user_id = current.user.id
    loop = asyncio.get_running_loop()
    started = loop.time()
    cursor = parse_cursor(last_event_id)
    async with maker() as db:
        if cursor is None:
            cursor = int(
                await db.scalar(
                    select(func.coalesce(func.max(StockSignalNotification.id), 0)).where(
                        StockSignalNotification.user_id == user_id
                    )
                )
                or 0
            )
        unread = await unread_count(db, user_id)
    yield "retry: 3000\n\n"
    yield sse(
        "hello",
        {"server_time": _iso(datetime.now(UTC)), "cursor": cursor, "unread_count": unread},
        str(cursor),
    )
    last_sent = loop.time()
    while True:
        rows: list[StockSignalNotification] = []
        try:
            async with maker() as db:
                if not await _session_ok(db, current.session.id):
                    yield sse("bye", {"reason": "session_ended"})
                    return
                rows = await list_notifications(db, user_id, after_id=cursor, limit=BATCH)
        except Exception as exc:  # the database is restarting: try again next poll
            log.warning("stock_signal_stream_poll_failed", error=type(exc).__name__)
        for row in rows:
            cursor = row.id
            yield sse("notification", notification_json(row), str(row.id))
        if rows:
            last_sent = loop.time()
        elif loop.time() - last_sent >= heartbeat_seconds:
            yield PING
            last_sent = loop.time()
        if loop.time() - started >= max_seconds:
            yield sse("bye", {"reason": "max_duration"}, str(cursor))
            return
        if len(rows) < BATCH:
            await asyncio.sleep(poll_seconds)


async def purge(maker: async_sessionmaker[AsyncSession], settings: Settings) -> int:
    """Notifications past STOCK_SIGNALS_RETENTION_DAYS."""
    cutoff = datetime.now(UTC) - timedelta(days=settings.STOCK_SIGNALS_RETENTION_DAYS)
    async with maker() as db, db.begin():
        res = await db.execute(
            delete(StockSignalNotification).where(StockSignalNotification.created_at < cutoff)
        )
    return int(res.rowcount or 0)  # type: ignore[attr-defined]
