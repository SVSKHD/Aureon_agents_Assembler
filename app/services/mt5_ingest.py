"""MT5 push ingest (schema aureon.mt5.push/v1) and the dashboard's reads.

POST /api/trading/mt5/ingest → `DbIngestStore.store()`, one transaction:

1. mt5.pushes: INSERT ... ON CONFLICT (push_id) DO NOTHING. A known push_id is a duplicate:
   nothing else is written and the API answers 200 {"duplicate": true}.
2. The v1 part goes through the SAME code as the pull path (`mt5_sync.apply_status`):
   agents.agents "mt5-bot" heartbeat (= receive time), agent_logs, trading_signals,
   mt5.terminals + positions_snapshots.
3. mt5.events: journal events + tracebacks, ON CONFLICT (id) DO NOTHING (per-event idempotency).
4. mt5.bot_snapshots: the latest push (guardian, strategy, health, signals_latest, ...).
5. mt5.candles: upsert of the pushed bars (UTC open time).
6. mt5.failures: classification (`classify`) of this push, with reason text.

Read-only towards the bot and the broker: nothing here can place, modify or close an order,
and nothing changes an account's mode.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.trading.mt5.adapter import assess_status
from app.agents.trading.mt5.push import PushDoc
from app.agents.trading.types import session_for
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.brokers import Mt5BotSnapshot, Mt5Candle, Mt5Event, Mt5Failure, Mt5Push
from app.services import mt5_sync
from app.services.mt5_sync import MT5_BOT, _jsonable, event_key

log = get_logger(__name__)

BOT_ID = MT5_BOT.id
# Failure kinds that describe a CONDITION: resolved by the first push that no longer shows it.
STATE_KINDS = frozenset({"mt5_disconnected", "stale_ticks", "discord_error"})
# Failure kinds that describe OCCURRENCES: resolved after MT5_FAILURE_QUIET_SECONDS without one.
EVENT_KINDS = frozenset({"guardian_error", "exception"})
TIMELINE_KINDS = ("P", "cross", "protected", "secured", "exit", "closed", "error")


# --- event normalisation ------------------------------------------------------------------


def event_kind(ev: dict[str, Any]) -> str:
    """Timeline badge for a journal event: P, cross, protected, secured, exit, closed, error,
    the signal kind for other signals (reentry, ...), or the raw event name for anything else
    (position_seen, locked, trail_update, ...). Any event string is accepted."""
    name = str(ev.get("event") or "").strip()
    low = name.lower()
    if low in ("error", "traceback", "exception", "agent_crash"):
        return "error"
    if low == "signal":
        sk = str(ev.get("signal_kind") or ev.get("kind") or "").strip()
        if not sk or sk.lower() == "cross":
            return "cross"  # the bot's default signal is the EMA cross
        if sk.lower() in ("p", "pullback", "pb") or sk.upper().startswith("P_"):
            return "P"
        return sk[:32]
    if low in ("protected", "secured", "exit", "closed"):
        return low
    if low in ("closed_by_rule", "position_closed", "close"):
        return "closed"
    return name[:32] or "unknown"


def _epoch(v: Any) -> datetime | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | Decimal) and not isinstance(v, bool):
        try:
            return datetime.fromtimestamp(int(v), UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _int(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, Decimal) and v == v.to_integral_value():
        return int(v)
    return None


@dataclass
class EventRow:
    id: str
    t: datetime
    event: str
    kind: str
    symbol: str | None
    ticket: int | None
    payload: dict[str, Any]


def event_rows(push: PushDoc, now: datetime) -> list[EventRow]:
    """Journal events and tracebacks as mt5.events rows (an id-less event gets a content hash)."""
    rows: dict[str, EventRow] = {}
    for ev in push.events:
        if not ev.get("event"):
            continue
        eid = str(ev.get("id") or event_key(ev))
        sym = ev.get("symbol")
        rows[eid] = EventRow(
            id=eid,
            t=_epoch(ev.get("t")) or now,
            event=str(ev["event"])[:64],
            kind=event_kind(ev),
            symbol=str(sym)[:64] if sym else None,
            ticket=_int(ev.get("ticket")),
            payload=_jsonable({k: v for k, v in ev.items() if k != "id"}),
        )
    for tb in push.tracebacks:
        rows[tb.id] = EventRow(
            id=tb.id,
            t=_epoch(tb.t) or now,
            event="traceback",
            kind="error",
            symbol=_where_symbol(tb.where),
            ticket=None,
            payload=tb.model_dump(mode="json", exclude={"id"}),
        )
    return list(rows.values())


def _where_symbol(where: str) -> str | None:
    # "agent:XAUUSD" / "guardian:XAUUSD" → XAUUSD; "main", "discord", "guardian" → None
    _, sep, rest = where.partition(":")
    return rest[:64] or None if sep else None


# --- failure classification -----------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    kind: str
    symbol: str | None
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.symbol or '*'}"


def _text(*parts: Any) -> str:
    return " ".join(str(p) for p in parts if p not in (None, "")).strip()[:1000]


def _error_message(payload: dict[str, Any]) -> str:
    return _text(
        payload.get("type"),
        payload.get("message") or payload.get("error") or payload.get("reason"),
    ) or _text(json.dumps(payload, sort_keys=True, default=str)[:300])


def classify(push: PushDoc, new_events: Iterable[EventRow], settings: Settings) -> list[Finding]:
    """The contract's server-side classification. `new_events` are only the events and
    tracebacks first seen in this push, so a re-sent traceback is not counted twice.
    (stale_heartbeat is not here: it is the absence of pushes, see `stale_heartbeat`.)"""
    out: list[Finding] = []
    h = push.health
    # mt5_disconnected: terminal link, or any agent reporting connected:false
    if h.mt5.connected is False:
        out.append(
            Finding(
                "mt5_disconnected",
                None,
                _text(
                    "MT5 terminal not connected",
                    f"({h.mt5.terminal})" if h.mt5.terminal else None,
                    f": {h.mt5.last_error}" if h.mt5.last_error else None,
                ),
            )
        )
    for sym, a in sorted(push.agents.items()):
        if a.connected is False:
            out.append(
                Finding(
                    "mt5_disconnected",
                    sym,
                    _text(
                        f"agent {sym} reports connected=false", f"(mt5={a.mt5})" if a.mt5 else None
                    ),
                )
            )
    # stale_ticks: market open but the newest tick too old
    limit = settings.MT5_STALE_TICK_SECONDS
    for sym, m in sorted(h.market.items()):
        if m.open and m.tick_age_s is not None and m.tick_age_s > Decimal(str(limit)):
            out.append(
                Finding(
                    "stale_ticks",
                    sym,
                    f"market open but the last {sym} tick is {int(m.tick_age_s)} s old "
                    f"(limit {int(limit)} s)",
                    {"tick_age_s": str(m.tick_age_s)},
                )
            )
    # discord_error: health flag
    if h.discord.ok is False:
        out.append(
            Finding(
                "discord_error",
                None,
                _text(
                    "Discord notifications failing",
                    f": {h.discord.last_error}" if h.discord.last_error else None,
                ),
                {"last_error_at": h.discord.last_error_at},
            )
        )
    # tracebacks and error events (new ones only)
    for ev in new_events:
        if ev.kind != "error":
            continue
        p = ev.payload
        if ev.event == "traceback":
            where = str(p.get("where") or "")
            msg = _text(f"{p.get('type') or 'Exception'}:", p.get("message"), f"(in {where})")
            if where.startswith("guardian"):
                kind = "guardian_error"
            elif where == "discord":
                kind = "discord_error"
            else:
                kind = "exception"
        else:
            msg = _error_message(p)
            blob = json.dumps(p, default=str).lower()
            kind = "guardian_error" if "guardian" in blob else "exception"
        out.append(Finding(kind, ev.symbol, msg or "error", {"event_id": ev.id}))
    return out


def stale_heartbeat(
    last_push: datetime | None, now: datetime, settings: Settings
) -> Finding | None:
    limit = settings.MT5_STALE_HEARTBEAT_SECONDS
    if last_push is None:
        return None  # never pushed: nothing to be stale against (status card says "no push yet")
    age = (now - last_push).total_seconds()
    if age <= limit:
        return None
    return Finding(
        "stale_heartbeat",
        None,
        f"no push from the bot for {int(age)} s (limit {int(limit)} s); "
        f"last push at {last_push.isoformat()}",
    )


def heartbeat_state(
    last_push: datetime | None,
    now: datetime,
    settings: Settings,
    *,
    health_status: str | None,
    v1_ok: bool,
    open_failures: int,
) -> tuple[str, list[str]]:
    """online / stale / error, with the reasons the card shows."""
    if last_push is None:
        return "stale", ["no push received yet"]
    stale = stale_heartbeat(last_push, now, settings)
    if stale is not None:
        return "stale", [stale.reason]
    reasons: list[str] = []
    if (health_status or "").lower() == "error":
        reasons.append("bot reports health status error")
    if not v1_ok:
        reasons.append("bot or MT5 link not healthy")
    if reasons:
        return "error", reasons
    if (health_status or "").lower() == "degraded" or open_failures:
        return "online", [f"{open_failures} open failure(s)"] if open_failures else ["degraded"]
    return "online", []


# --- storage --------------------------------------------------------------------------------


@dataclass
class IngestResult:
    duplicate: bool
    push_id: uuid.UUID
    events_new: int = 0
    events_duplicate: int = 0
    failures: list[str] = field(default_factory=list)


class IngestStore(Protocol):
    async def store(self, push: PushDoc, size_bytes: int, settings: Settings) -> IngestResult: ...


def _server_to_utc(push: PushDoc, symbol: str, epoch: int) -> datetime:
    return push.server_to_utc(epoch, symbol)


async def _upsert_failures(
    db: AsyncSession, findings: list[Finding], now: datetime, settings: Settings, *, push: bool
) -> None:
    for f in findings:
        ins = insert(Mt5Failure).values(
            key=f.key,
            kind=f.kind,
            symbol=f.symbol,
            reason=f.reason,
            first_seen=now,
            last_seen=now,
            count=1,
            details=_jsonable(f.details),
        )
        await db.execute(
            ins.on_conflict_do_update(
                index_elements=[Mt5Failure.key],
                index_where=Mt5Failure.resolved_at.is_(None),
                set_={
                    "last_seen": now,
                    "reason": ins.excluded.reason,
                    "details": ins.excluded.details,
                    "count": Mt5Failure.count + 1,
                },
            )
        )
    if not push:
        return
    # A push clears stale_heartbeat and every condition this push no longer shows ...
    present = {f.key for f in findings if f.kind in STATE_KINDS}
    cond = [
        Mt5Failure.resolved_at.is_(None),
        Mt5Failure.kind.in_(STATE_KINDS | {"stale_heartbeat"}),
    ]
    if present:
        cond.append(Mt5Failure.key.not_in(present))
    await db.execute(update(Mt5Failure).where(*cond).values(resolved_at=now))
    # ... and occurrences that stayed quiet long enough.
    await db.execute(
        update(Mt5Failure)
        .where(
            Mt5Failure.resolved_at.is_(None),
            Mt5Failure.kind.in_(EVENT_KINDS),
            Mt5Failure.last_seen < now - timedelta(seconds=settings.MT5_FAILURE_QUIET_SECONDS),
        )
        .values(resolved_at=now)
    )


def candle_rows(push: PushDoc) -> list[dict[str, Any]]:
    rows: dict[tuple[str, str, datetime], dict[str, Any]] = {}
    for sym, tfs in push.candles.items():
        for tf, bars in tfs.items():
            tfu = tf.strip().upper()[:4]
            for b in bars:
                if b.h < b.l:
                    continue
                ts = _server_to_utc(push, sym, b.t)
                rows[(sym[:64], tfu, ts)] = {
                    "symbol": sym[:64],
                    "timeframe": tfu,
                    "ts": ts,
                    "open": b.o,
                    "high": b.h,
                    "low": b.l,
                    "close": b.c,
                    "volume": b.v,
                    "ema": _jsonable(b.ema),
                }
    return list(rows.values())


class DbIngestStore:
    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self.maker = maker

    async def store(self, push: PushDoc, size_bytes: int, settings: Settings) -> IngestResult:
        async with self.maker() as db, db.begin():
            now = await db.scalar(select(func.now()))
            assert now is not None
            first = await db.scalar(
                insert(Mt5Push)
                .values(
                    push_id=push.push_id,
                    seq=push.seq,
                    generated_at=push.generated_at,
                    received_at=now,
                    size_bytes=size_bytes,
                )
                .on_conflict_do_nothing(index_elements=[Mt5Push.push_id])
                .returning(Mt5Push.push_id)
            )
            if first is None:
                return IngestResult(duplicate=True, push_id=push.push_id)

            ok, why = assess_status(push, None, now)
            await mt5_sync.apply_status(
                db,
                push,
                settings,
                ok=ok,
                why=why,
                source="push",
                stale_after_s=settings.MT5_STALE_HEARTBEAT_SECONDS,
                heartbeat_at=now,  # heartbeat = last push time (server clock)
            )

            rows = event_rows(push, now)
            new_ids: set[str] = set()
            for i in range(0, len(rows), 500):
                chunk = rows[i : i + 500]
                res = await db.scalars(
                    insert(Mt5Event)
                    .values(
                        [
                            {
                                "id": r.id,
                                "t": r.t,
                                "event": r.event,
                                "kind": r.kind,
                                "symbol": r.symbol,
                                "ticket": r.ticket,
                                "payload": r.payload,
                                "push_id": push.push_id,
                                "received_at": now,
                            }
                            for r in chunk
                        ]
                    )
                    .on_conflict_do_nothing(index_elements=[Mt5Event.id])
                    .returning(Mt5Event.id)
                )
                new_ids.update(res.all())
            new_rows = [r for r in rows if r.id in new_ids]

            snap = {
                "bot_id": BOT_ID,
                "push_id": push.push_id,
                "seq": push.seq,
                "schema_name": push.schema_,
                "generated_at": push.generated_at,
                "received_at": now,
                "bot": push.bot.model_dump(mode="json"),
                "agents": _jsonable({s: a.model_dump(mode="json") for s, a in push.agents.items()}),
                "health": push.health.model_dump(mode="json"),
                "guardian": _jsonable(
                    {s: [g.model_dump(mode="json") for g in gs] for s, gs in push.guardian.items()}
                ),
                "signals_latest": _jsonable(push.signals_latest),
                "strategy": push.strategy.model_dump(mode="json") if push.strategy else {},
                "halt": _jsonable(push.halt),
                "console": _jsonable(push.console),
                "ema921e": _jsonable(push.ema921e),
                "ema921e_trades": _jsonable(push.ema921e_trades[-500:]),
                "backtests": _jsonable(push.backtests),
                "errors": push.errors,
            }
            ins = insert(Mt5BotSnapshot).values(**snap)
            await db.execute(
                ins.on_conflict_do_update(
                    index_elements=[Mt5BotSnapshot.bot_id],
                    set_={k: ins.excluded[k] for k in snap if k != "bot_id"},
                    # an out-of-order (older) push never replaces a newer snapshot
                    where=Mt5BotSnapshot.generated_at <= ins.excluded.generated_at,
                )
            )

            candles = candle_rows(push)
            for i in range(0, len(candles), 1000):
                cins = insert(Mt5Candle).values(candles[i : i + 1000])
                await db.execute(
                    cins.on_conflict_do_update(
                        constraint="pk_candles",
                        set_={
                            k: cins.excluded[k]
                            for k in ("open", "high", "low", "close", "volume", "ema")
                        }
                        | {"received_at": now},
                    )
                )

            findings = classify(push, new_rows, settings)
            await _upsert_failures(db, findings, now, settings, push=True)

            dup = len(rows) - len(new_rows)
            await db.execute(
                update(Mt5Push)
                .where(Mt5Push.push_id == push.push_id)
                .values(events_new=len(new_rows), events_duplicate=dup)
            )
        log.info(
            "mt5_ingest",
            push_id=str(push.push_id),
            seq=push.seq,
            events_new=len(new_rows),
            events_duplicate=dup,
            candles=len(candles),
            failures=[f.key for f in findings],
        )
        return IngestResult(
            duplicate=False,
            push_id=push.push_id,
            events_new=len(new_rows),
            events_duplicate=dup,
            failures=[f.key for f in findings],
        )


# --- scheduler tasks --------------------------------------------------------------------------


async def watch(maker: async_sessionmaker[AsyncSession], settings: Settings) -> dict[str, Any]:
    """Every minute: open (or bump) a stale_heartbeat failure when the bot stopped pushing."""
    async with maker() as db, db.begin():
        now = await db.scalar(select(func.now()))
        assert now is not None
        last = await db.scalar(
            select(Mt5BotSnapshot.received_at).where(Mt5BotSnapshot.bot_id == BOT_ID)
        )
        f = stale_heartbeat(last, now, settings)
        if f is not None:
            await _upsert_failures(db, [f], now, settings, push=False)
    return {
        "last_push_at": last.isoformat() if last else None,
        "stale": f is not None,
    }


async def purge(maker: async_sessionmaker[AsyncSession], settings: Settings) -> dict[str, Any]:
    """Retention: candles, events, push ids and resolved failures."""
    c_cut = func.now() - timedelta(days=settings.MT5_CANDLE_RETENTION_DAYS)
    e_cut = func.now() - timedelta(days=settings.MT5_EVENT_RETENTION_DAYS)
    async with maker() as db, db.begin():
        c = await db.execute(delete(Mt5Candle).where(Mt5Candle.ts < c_cut))
        e = await db.execute(delete(Mt5Event).where(Mt5Event.t < e_cut))
        p = await db.execute(delete(Mt5Push).where(Mt5Push.received_at < e_cut))
        f = await db.execute(
            delete(Mt5Failure).where(
                and_(Mt5Failure.resolved_at.is_not(None), Mt5Failure.resolved_at < e_cut)
            )
        )
    return {
        "candles": c.rowcount,  # type: ignore[attr-defined]
        "events": e.rowcount,  # type: ignore[attr-defined]
        "pushes": p.rowcount,  # type: ignore[attr-defined]
        "failures": f.rowcount,  # type: ignore[attr-defined]
    }


def market_session(now: datetime) -> str:
    return session_for(now).value
