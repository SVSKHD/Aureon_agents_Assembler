"""Read side of the MT5 push ingest: what the /trading/mt5 dashboard shows. Read-only."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import effective_state
from app.core.config import Settings
from app.models.agents import Agent
from app.models.brokers import Mt5BotSnapshot, Mt5Candle, Mt5Event, Mt5Failure
from app.schemas.mt5 import (
    Mt5BotStatusOut,
    Mt5CandleOut,
    Mt5DailyOut,
    Mt5EventOut,
    Mt5FailureOut,
    Mt5FailuresOut,
    Mt5GuardianOut,
    Mt5MarketOut,
    Mt5PositionOut,
    Mt5PositionsOut,
    Mt5ReportOut,
    Mt5SignalsOut,
    Mt5StrategyOut,
    Mt5SymbolMarketOut,
)
from app.services import mt5_sync
from app.services.mt5_ingest import BOT_ID, heartbeat_state, market_session, stale_heartbeat


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return Decimal(format(d.normalize(), "f")) if d.is_finite() else None


def _norm(d: Decimal) -> Decimal:
    """2653.50000000 (numeric(24,8)) → 2653.5, never in exponent form."""
    return Decimal(format(d.normalize(), "f"))


def _norm_opt(d: Decimal | None) -> Decimal | None:
    return None if d is None else _norm(d)


async def _now(db: AsyncSession) -> datetime:
    now = await db.scalar(select(func.now()))
    assert now is not None
    return now


async def snapshot(db: AsyncSession) -> Mt5BotSnapshot | None:
    row: Mt5BotSnapshot | None = await db.get(Mt5BotSnapshot, BOT_ID)
    return row


async def _open_failures(db: AsyncSession) -> int:
    n = await db.scalar(
        select(func.count()).select_from(Mt5Failure).where(Mt5Failure.resolved_at.is_(None))
    )
    return int(n or 0)


async def bot_status(db: AsyncSession, settings: Settings) -> Mt5BotStatusOut:
    now = await _now(db)
    snap = await snapshot(db)
    agent: Agent | None = await db.get(Agent, BOT_ID)
    v1_ok = bool((agent.details or {}).get("ok", True)) if agent else True
    reasons_v1 = (agent.details or {}).get("detail") if agent else None
    open_n = await _open_failures(db)
    last = snap.received_at if snap else None
    state, reasons = heartbeat_state(
        last,
        now,
        settings,
        health_status=str((snap.health or {}).get("status") or "") if snap else None,
        v1_ok=v1_ok,
        open_failures=open_n,
    )
    if state == "error" and isinstance(reasons_v1, str) and reasons_v1:
        reasons.append(reasons_v1)
    if snap is not None and snap.halt:
        reasons.append(f"bot halted: {snap.halt.get('reason') or 'manual'}")
    if agent is not None and agent.paused:
        reasons.append("paused in Aureon (" + effective_state(agent, 0, now) + ")")
    return Mt5BotStatusOut(
        state=state,
        reasons=reasons,
        last_heartbeat_at=last,
        age_s=int((now - last).total_seconds()) if last else None,
        stale_after_s=settings.MT5_STALE_HEARTBEAT_SECONDS,
        push_id=snap.push_id if snap else None,
        seq=snap.seq if snap else None,
        generated_at=snap.generated_at if snap else None,
        bot=snap.bot if snap else {},
        health=snap.health if snap else {},
        halt=snap.halt if snap else None,
        open_failures=open_n,
        server_time=now,
    )


def _exec_tf(snap: Mt5BotSnapshot | None) -> str:
    if snap is None:
        return "M5"
    raw = (snap.strategy or {}).get("exec_tf") or (snap.bot or {}).get("exec_tf") or "M5"
    return str(raw).strip().upper()[:4]


def _symbols(snap: Mt5BotSnapshot | None) -> list[str]:
    if snap is None:
        return []
    seen: list[str] = []
    for src in (
        (snap.strategy or {}).get("symbols") or [],
        (snap.bot or {}).get("symbols") or [],
        list((snap.agents or {}).keys()),
        list(((snap.health or {}).get("market") or {}).keys()),
    ):
        for s in src:
            if isinstance(s, str) and s not in seen:
                seen.append(s)
    return seen


async def market(db: AsyncSession, timeframe: str | None, limit: int) -> Mt5MarketOut:
    now = await _now(db)
    snap = await snapshot(db)
    tf = (timeframe or _exec_tf(snap)).strip().upper()[:4]
    out: list[Mt5SymbolMarketOut] = []
    for sym in _symbols(snap):
        assert snap is not None
        m = ((snap.health or {}).get("market") or {}).get(sym) or {}
        agent = (snap.agents or {}).get(sym) or {}
        is_open = m.get("open") if isinstance(m.get("open"), bool) else agent.get("market_open")
        rows = (
            await db.scalars(
                select(Mt5Candle)
                .where(Mt5Candle.symbol == sym, Mt5Candle.timeframe == tf)
                .order_by(Mt5Candle.ts.desc())
                .limit(limit)
            )
        ).all()
        candles = [
            Mt5CandleOut(
                t=r.ts,
                o=_norm(r.open),
                h=_norm(r.high),
                l=_norm(r.low),
                c=_norm(r.close),
                v=_norm_opt(r.volume),
                ema={str(k): _dec(v) for k, v in (r.ema or {}).items()},
            )
            for r in reversed(rows)
        ]
        last_price = candles[-1].c if candles else None
        last_at = candles[-1].t if candles else None
        if last_price is None:
            for p in agent.get("positions") or []:
                if (cur := _dec(p.get("current"))) is not None:
                    last_price, last_at = cur, snap.generated_at
                    break
        periods = sorted({k for c in candles for k in c.ema}, key=lambda x: (len(x), x))
        out.append(
            Mt5SymbolMarketOut(
                symbol=sym,
                open=is_open if isinstance(is_open, bool) else None,
                session=market_session(now),
                bot_session=str(m["session"]) if m.get("session") else None,
                tick_age_s=_dec(m.get("tick_age_s", agent.get("tick_age"))),
                last_price=last_price,
                last_price_at=last_at,
                timeframe=tf,
                ema_periods=periods,
                candles=candles,
            )
        )
    return Mt5MarketOut(
        server_time=now,
        session=market_session(now),
        as_of=snap.received_at if snap else None,
        symbols=out,
    )


def _guardian(g: dict[str, Any]) -> Mt5GuardianOut:
    return Mt5GuardianOut(
        phase=str(g["phase"]) if g.get("phase") is not None else None,
        leg_phase=str(g["leg_phase"]) if g.get("leg_phase") is not None else None,
        profile=str(g["profile"]) if g.get("profile") is not None else None,
        secured_pts=_dec(g.get("secured_pts")),
        peak_pts=_dec(g.get("peak_pts")),
        bars=g["bars"]
        if isinstance(g.get("bars"), int) and not isinstance(g["bars"], bool)
        else None,
        frozen=g["frozen"] if isinstance(g.get("frozen"), bool) else None,
        sl=_dec(g.get("sl")),
        secured_level=_dec(g.get("secured_level")),
        peak=_dec(g.get("peak")),
        direction=str(g["direction"]) if g.get("direction") is not None else None,
        entry=_dec(g.get("entry")),
        updated_at=str(g["updated_at"]) if g.get("updated_at") is not None else None,
    )


async def positions(db: AsyncSession, settings: Settings) -> Mt5PositionsOut:
    now = await _now(db)
    snap = await snapshot(db)
    if snap is None:
        return Mt5PositionsOut(as_of=None, stale=True, items=[], guardian_orphans={})
    guard: dict[tuple[str, int], dict[str, Any]] = {}
    for sym, entries in (snap.guardian or {}).items():
        for g in entries or []:
            if isinstance(g, dict) and isinstance(g.get("ticket"), int):
                guard[(sym, g["ticket"])] = g
    items: list[Mt5PositionOut] = []
    for sym, agent in sorted((snap.agents or {}).items()):
        offset = _dec(agent.get("offset_h")) or _dec((snap.bot or {}).get("server_utc_offset"))
        for p in agent.get("positions") or []:
            ticket = p.get("ticket")
            if not isinstance(ticket, int):
                continue
            psym = str(p.get("symbol") or sym)
            opened = None
            if isinstance(p.get("time"), int):
                opened = datetime.fromtimestamp(p["time"], UTC) - timedelta(
                    seconds=int((offset or 0) * 3600)
                )
            g = guard.pop((sym, ticket), None) or guard.pop((psym, ticket), None)
            items.append(
                Mt5PositionOut(
                    symbol=psym,
                    ticket=ticket,
                    direction=str(p.get("direction") or ""),
                    volume=_dec(p.get("volume")) or Decimal(0),
                    price_open=_dec(p.get("price_open")) or Decimal(0),
                    current=_dec(p.get("current")),
                    sl=_dec(p.get("sl")),
                    tp=_dec(p.get("tp")),
                    points=_dec(p.get("points")),
                    opened_at=opened,
                    guardian=_guardian(g) if g else None,
                )
            )
    orphans: dict[str, list[Mt5GuardianOut]] = defaultdict(list)
    for (sym, _), g in guard.items():
        orphans[sym].append(_guardian(g))
    return Mt5PositionsOut(
        as_of=snap.received_at,
        stale=stale_heartbeat(snap.received_at, now, settings) is not None,
        items=items,
        guardian_orphans=dict(orphans),
    )


def _event_out(r: Mt5Event) -> Mt5EventOut:
    return Mt5EventOut(
        id=r.id,
        t=r.t,
        event=r.event,
        kind=r.kind,
        symbol=r.symbol,
        ticket=r.ticket,
        payload=r.payload or {},
    )


async def signals(
    db: AsyncSession,
    *,
    kinds: list[str] | None,
    symbol: str | None,
    limit: int,
    offset: int,
) -> Mt5SignalsOut:
    stmt = select(Mt5Event)
    if kinds:
        stmt = stmt.where(Mt5Event.kind.in_(kinds))
    if symbol:
        stmt = stmt.where(Mt5Event.symbol == symbol)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await db.scalars(stmt.order_by(Mt5Event.t.desc(), Mt5Event.id).limit(limit).offset(offset))
    ).all()
    snap = await snapshot(db)
    return Mt5SignalsOut(
        items=[_event_out(r) for r in rows],
        total=int(total or 0),
        limit=limit,
        offset=offset,
        latest=(snap.signals_latest or {}) if snap else {},
    )


async def failures(
    db: AsyncSession, settings: Settings, *, include_resolved: bool, limit: int
) -> Mt5FailuresOut:
    now = await _now(db)
    stmt = select(Mt5Failure)
    if not include_resolved:
        stmt = stmt.where(Mt5Failure.resolved_at.is_(None))
    rows = (
        await db.scalars(
            stmt.order_by(Mt5Failure.resolved_at.is_not(None), Mt5Failure.last_seen.desc()).limit(
                limit
            )
        )
    ).all()
    items = [
        Mt5FailureOut(
            id=r.id,
            kind=r.kind,
            symbol=r.symbol,
            reason=r.reason,
            first_seen=r.first_seen,
            last_seen=r.last_seen,
            count=r.count,
            resolved_at=r.resolved_at,
            open=r.resolved_at is None,
        )
        for r in rows
    ]
    # On-read stale detection: shown even before the watch job (every minute) stores it.
    snap = await snapshot(db)
    stale = stale_heartbeat(snap.received_at if snap else None, now, settings)
    if stale is not None and not any(i.kind == "stale_heartbeat" and i.open for i in items):
        assert snap is not None
        items.insert(
            0,
            Mt5FailureOut(
                id=None,
                kind="stale_heartbeat",
                symbol=None,
                reason=stale.reason,
                first_seen=snap.received_at
                + timedelta(seconds=settings.MT5_STALE_HEARTBEAT_SECONDS),
                last_seen=now,
                count=1,
                resolved_at=None,
                open=True,
            ),
        )
    return Mt5FailuresOut(open_count=sum(1 for i in items if i.open), items=items)


async def strategy(db: AsyncSession) -> Mt5StrategyOut:
    snap = await snapshot(db)
    st: dict[str, Any] = (snap.strategy or {}) if snap else {}
    bot: dict[str, Any] = (snap.bot or {}) if snap else {}
    return Mt5StrategyOut(
        as_of=snap.received_at if snap else None,
        bot_version=bot.get("version"),
        mode=st.get("mode") or bot.get("mode"),
        symbols=list(st.get("symbols") or bot.get("symbols") or []),
        exec_tf=st.get("exec_tf") or bot.get("exec_tf"),
        timeframes=list(st.get("timeframes") or []),
        ema=dict(st.get("ema") or {}),
        params=dict(st.get("params") or {}),
    )


async def report(db: AsyncSession, days: int) -> Mt5ReportOut:
    now = await _now(db)
    since = now - timedelta(days=days)
    rows = (
        await db.scalars(
            select(Mt5Event)
            .where(Mt5Event.event == "exit", Mt5Event.t >= since)
            .order_by(Mt5Event.t)
        )
    ).all()
    # performance() reads the journal shape: payload + the real-UTC epoch `t`.
    events = [{**(r.payload or {}), "t": int(r.t.timestamp())} for r in rows]
    snap = await snapshot(db)
    trades = list(snap.ema921e_trades or []) if snap else []
    perf = mt5_sync.performance_from(events, trades, snap.received_at if snap else now, now)
    daily: dict[str, dict[str, Any]] = {}
    for ev in events:
        pts = _dec(ev.get("points"))
        if pts is None:
            continue
        day = datetime.fromtimestamp(ev["t"], UTC).date().isoformat()
        d = daily.setdefault(
            day, {"date": day, "exits": 0, "wins": 0, "losses": 0, "net_points": Decimal(0)}
        )
        d["exits"] += 1
        d["wins"] += pts > 0
        d["losses"] += pts < 0
        d["net_points"] += pts
    return Mt5ReportOut(
        days=days,
        performance=perf,
        daily=[Mt5DailyOut(**d) for d in sorted(daily.values(), key=lambda x: x["date"])],
        ema921e_trades=trades[-100:],
        backtests=list(snap.backtests or []) if snap else [],
    )
