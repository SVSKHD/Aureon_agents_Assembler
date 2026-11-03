"""Real-time trading events for the frontend: one Server-Sent Events stream per browser tab.

    GET /api/trading/stream?instrument_id=42&instrument_id=7

Events (each `data:` is JSON):

- `hello`     once: server time and the poll interval.
- `signal`    a new trading_signals row (all users see signals, as in GET /signals).
- `risk`      a new risk_decisions row for an account the user can see.
- `backtest`  a backtest run that changed status (queued → running → done/failed).
- `agent`     a trading agent's effective state changed (idle / busy / error / stale / paused).
- `quote`     the latest Dhan feed values for the requested instruments, when they change.
- `mt5`       the MT5 bot pushed (new push_id) or its open failures changed; the /trading/mt5
              page refetches its panels. Current state, may repeat after a reconnect.
- `bye`       the stream is ending (max duration or session expired); the client reconnects.

Every data event carries an `id:` cursor. A reconnecting EventSource sends it back as
`Last-Event-ID`, and the stream resumes exactly after it: signals and risk decisions are
neither repeated nor skipped (they are emitted ~2 s after insert). Backtest and agent events
describe current state and may repeat after a reconnect; apply them idempotently by id.
Without a cursor the stream starts "now" (no backlog: the page loads history through the
normal list endpoints).

Read-only: the stream never writes anything. It holds no DB connection between polls, and
it re-checks the login session every poll so a logout or revocation ends it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.registry import effective_state
from app.auth.deps import CurrentSession
from app.models.agents import Agent
from app.models.auth import UserSession
from app.models.brokers import Mt5BotSnapshot, Mt5Failure
from app.models.trading import BacktestRun, BrokerAccount, Instrument, RiskDecision, TradingSignal
from app.services.trading import visible_accounts

PING = ": ping\n\n"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _json_default(v: Any) -> str:
    return v.isoformat() if isinstance(v, datetime) else str(v)  # ISO-8601 for the browser


def sse(event: str, data: dict[str, Any], event_id: str | None = None) -> str:
    body = json.dumps(data, default=_json_default, separators=(",", ":"))
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {body}\n\n"


@dataclass
class Cursor:
    """Where the client is: the last signal (created_at, id), the last risk decision id and the
    last backtest change time. Encoded into every event id."""

    signal_at: datetime
    signal_id: str
    risk_id: int
    backtest_at: datetime

    def encode(self) -> str:
        raw = json.dumps(
            [
                self.signal_at.isoformat(),
                self.signal_id,
                self.risk_id,
                self.backtest_at.isoformat(),
            ],
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str | None) -> Cursor | None:
        if not value:
            return None
        try:
            pad = "=" * (-len(value) % 4)
            s, sid, r, b = json.loads(base64.urlsafe_b64decode(value + pad))
            cur = cls(datetime.fromisoformat(s), str(sid), int(r), datetime.fromisoformat(b))
        except (ValueError, TypeError):
            return None
        if cur.signal_at.tzinfo is None or cur.backtest_at.tzinfo is None:
            return None
        return cur


# Signals and risk decisions are only emitted once they are LAG old. A row's created_at is its
# transaction's start, so a row that commits shortly after a newer one is still picked up, and
# the strict (created_at, id) / id cursor means a reconnect never repeats or skips a row.
LAG = timedelta(seconds=2)
NIL_ID = "00000000-0000-0000-0000-000000000000"


@dataclass
class StreamState:
    cursor: Cursor
    backtests: dict[str, str] = field(default_factory=dict)  # id -> last status sent
    agents: dict[str, str] = field(default_factory=dict)
    quotes: dict[str, str] = field(default_factory=dict)
    mt5: str | None = None


async def _start_cursor(db: AsyncSession) -> Cursor:
    """'Now': rows that already exist are not replayed (pages load history via the lists)."""
    now = await db.scalar(select(func.now()))
    assert now is not None
    risk = await db.scalar(select(func.max(RiskDecision.id)))
    return Cursor(now, NIL_ID, int(risk or 0), now)


async def _session_ok(db: AsyncSession, session_id: uuid.UUID) -> bool:
    s = await db.get(UserSession, session_id)
    return bool(s and s.revoked_at is None and s.expires_at > datetime.now(UTC))


async def poll(
    db: AsyncSession,
    current: CurrentSession,
    state: StreamState,
    *,
    stale_after: float,
    limit: int = 500,
) -> list[tuple[str, dict[str, Any]]]:
    """One pass over the tables; advances the cursor. Returns (event, data) pairs."""
    out: list[tuple[str, dict[str, Any]]] = []
    c = state.cursor
    now = await db.scalar(select(func.now()))
    assert now is not None
    ready = now - LAG

    sigs = (
        await db.scalars(
            select(TradingSignal)
            .where(
                tuple_(TradingSignal.created_at, TradingSignal.id)
                > tuple_(literal(c.signal_at), literal(uuid.UUID(c.signal_id))),
                TradingSignal.created_at <= ready,
            )
            .order_by(TradingSignal.created_at, TradingSignal.id)
            .limit(limit)
        )
    ).all()
    for s in sigs:
        c.signal_at, c.signal_id = s.created_at, str(s.id)
        out.append(
            (
                "signal",
                {
                    "id": s.id,
                    "strategy_version_id": s.strategy_version_id,
                    "instrument_id": s.instrument_id,
                    "timeframe": s.timeframe,
                    "candle_time": s.candle_time,
                    "session": s.session,
                    "side": s.side,
                    "created_at": s.created_at,
                },
            )
        )

    allowed = visible_accounts(current).with_only_columns(BrokerAccount.id)
    risks = (
        await db.scalars(
            select(RiskDecision)
            .where(RiskDecision.id > c.risk_id, RiskDecision.created_at <= ready)
            .order_by(RiskDecision.id)
            .limit(limit)
        )
    ).all()
    visible = set((await db.scalars(allowed)).all()) if risks else set()
    for r in risks:
        c.risk_id = r.id
        if r.broker_account_id not in visible:
            continue
        out.append(
            (
                "risk",
                {
                    "id": r.id,
                    "broker_account_id": r.broker_account_id,
                    "signal_id": r.signal_id,
                    "approved": r.approved,
                    "reason": r.reason,
                    "created_at": r.created_at,
                },
            )
        )

    # Backtests change status in place; send each (id, status) once per stream.
    since = c.backtest_at - LAG
    changed = (
        await db.scalars(
            select(BacktestRun)
            .where(
                or_(
                    BacktestRun.created_at > since,
                    BacktestRun.started_at > since,
                    BacktestRun.finished_at > since,
                )
            )
            .limit(limit)
        )
    ).all()
    for b in sorted(changed, key=_bt_ts):
        c.backtest_at = max(c.backtest_at, min(_bt_ts(b), now))
        key = str(b.id)
        if state.backtests.get(key) == b.status:
            continue
        state.backtests[key] = b.status
        out.append(
            (
                "backtest",
                {
                    "id": b.id,
                    "status": b.status,
                    "strategy_version_id": b.strategy_version_id,
                    "instrument_id": b.instrument_id,
                    "timeframe": b.timeframe,
                    "error": b.error,
                },
            )
        )

    agents = (await db.scalars(select(Agent).where(Agent.kind == "trading"))).all()
    for a in agents:
        st = effective_state(a, stale_after)
        if state.agents.get(a.id) != st:
            state.agents[a.id] = st
            out.append(
                (
                    "agent",
                    {
                        "id": a.id,
                        "name": a.name,
                        "state": st,
                        "last_heartbeat_at": a.last_heartbeat_at,
                    },
                )
            )
    out += await mt5_events(db, state)
    return out


async def mt5_events(db: AsyncSession, state: StreamState) -> list[tuple[str, dict[str, Any]]]:
    """One `mt5` event when the bot's latest push or its open failures change."""
    snap = (
        await db.execute(
            select(
                Mt5BotSnapshot.push_id,
                Mt5BotSnapshot.seq,
                Mt5BotSnapshot.received_at,
                Mt5BotSnapshot.generated_at,
            ).where(Mt5BotSnapshot.bot_id == "mt5-bot")
        )
    ).first()
    if snap is None:
        return []
    n_open, last_seen = (
        await db.execute(
            select(func.count(), func.max(Mt5Failure.last_seen)).where(
                Mt5Failure.resolved_at.is_(None)
            )
        )
    ).one()
    sig = f"{snap.push_id}|{n_open}|{last_seen.isoformat() if last_seen else ''}"
    if state.mt5 == sig:
        return []
    state.mt5 = sig
    return [
        (
            "mt5",
            {
                "push_id": snap.push_id,
                "seq": snap.seq,
                "received_at": snap.received_at,
                "generated_at": snap.generated_at,
                "open_failures": int(n_open or 0),
            },
        )
    ]


def _bt_ts(b: BacktestRun) -> datetime:
    return max(_ts(b.created_at), _ts(b.started_at), _ts(b.finished_at))


def _ts(v: datetime | None) -> datetime:
    return v if v is not None else EPOCH


async def quotes(
    redis: Redis, db: AsyncSession, instrument_ids: list[int], state: StreamState
) -> list[tuple[str, dict[str, Any]]]:
    """Latest Dhan feed hash per requested instrument (written by the dhan-feed worker)."""
    if not instrument_ids:
        return []
    rows = (
        await db.execute(
            select(Instrument.id, Instrument.broker_instrument_id).where(
                Instrument.id.in_(instrument_ids), Instrument.broker == "dhan"
            )
        )
    ).all()
    out = []
    for inst_id, ref in rows:
        h = await redis.hgetall(f"dhan:tick:{ref}")
        if not h:
            continue
        data = {
            (k.decode() if isinstance(k, bytes) else str(k)): (
                v.decode() if isinstance(v, bytes) else str(v)
            )
            for k, v in h.items()
        }
        sig = json.dumps(data, sort_keys=True)
        if state.quotes.get(ref) == sig:
            continue
        state.quotes[ref] = sig
        out.append(
            (
                "quote",
                {
                    "instrument_id": inst_id,
                    "ref": ref,
                    "ltp": data.get("ltp"),
                    "ltt": data.get("ltt"),
                    "volume": data.get("volume"),
                    "stale": data.get("stale") == "1",
                    "received_at": data.get("received_at"),
                    "source": "broker",
                },
            )
        )
    return out


async def events(
    maker: async_sessionmaker[AsyncSession],
    redis: Redis,
    current: CurrentSession,
    *,
    last_event_id: str | None,
    instrument_ids: list[int],
    poll_seconds: float,
    heartbeat_seconds: float,
    max_seconds: float,
    stale_after: float,
) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    async with maker() as db:
        cursor = Cursor.decode(last_event_id) or await _start_cursor(db)
    state = StreamState(cursor)
    yield "retry: 3000\n\n"
    yield sse("hello", {"server_time": datetime.now(UTC), "poll_seconds": poll_seconds})
    last_sent = loop.time()
    while True:
        async with maker() as db:
            if not await _session_ok(db, current.session.id):
                yield sse("bye", {"reason": "session_ended"})
                return
            batch = await poll(db, current, state, stale_after=stale_after)
            try:
                batch += await quotes(redis, db, instrument_ids, state)
            except Exception:  # Redis down: trading events still flow
                batch.append(("quote_error", {"detail": "quote feed unavailable"}))
        for name, data in batch:
            yield sse(name, data, state.cursor.encode())
        if batch:
            last_sent = loop.time()
        elif loop.time() - last_sent >= heartbeat_seconds:
            yield PING
            last_sent = loop.time()
        if loop.time() - started >= max_seconds:
            yield sse("bye", {"reason": "max_duration"}, state.cursor.encode())
            return
        await asyncio.sleep(poll_seconds)
