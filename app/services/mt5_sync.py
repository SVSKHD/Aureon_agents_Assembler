"""MT5 bot export → database (read-only on the bot side). Runs every minute from the scheduler.

One pass reads the export once and writes:

- **agents.agents "mt5-bot"** (kind "trading"): state, version, details, and
  `last_heartbeat_at` = the export's own `generated_at`. A bot that stops exporting therefore
  goes stale after `details.stale_after_s` (MT5_STALE_SECONDS), even though this job keeps running.
- **agents.agent_logs**: allowlisted journal events (signal, exit, error, ...), each written once
  (deduplicated on a hash of the event).
- **trading.strategies / strategy_versions**: "mt5:<mode>" v1, created on first sight.
- **trading.trading_signals**: journal `signal` events, deduplicated on
  (strategy version, instrument, timeframe, candle time). Only for symbols registered in
  trading.instruments (broker "mt5"): contract specs are not in the export and are never guessed.
- **trading.positions_snapshots** + **mt5.terminals**: only when MT5_ACCOUNT_ID names a
  registered mt5 broker account.

Nothing is written from an export that fails validation; an unreachable bot writes nothing
(and so goes stale).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.registry import AgentSpec
from app.agents.trading.mt5.adapter import MT5Adapter, positions_from
from app.agents.trading.mt5.status import BotStatus
from app.agents.trading.types import SignalSide, session_for
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.agents import Agent, AgentLog
from app.models.brokers import Mt5Terminal
from app.models.trading import (
    BrokerAccount,
    Instrument,
    PositionSnapshot,
    Strategy,
    StrategyVersion,
    TradingSignal,
)

log = get_logger(__name__)

MT5_BOT = AgentSpec("mt5-bot", "Aureon MT5 bot", "trading")
SIDES = {"LONG": SignalSide.BUY, "SHORT": SignalSide.SELL}
ERROR_EVENTS = {"error"}
WARNING_EVENTS = {"watchdog_open", "process_exit"}


def _jsonable(v: Any) -> Any:
    """Decimals → canonical strings, recursively (JSONB must never get a float)."""
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v else None
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    return v


def event_key(ev: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(_jsonable(ev), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]


def bot_state(status: BotStatus, ok: bool) -> str:
    if status.halt:
        return "stopped"
    return "idle" if ok else "error"


def _message(ev: dict[str, Any]) -> str:
    parts = [str(ev.get("event"))]
    for k in ("symbol", "side", "signal_kind", "reason", "ticket", "points", "price"):
        if ev.get(k) not in (None, ""):
            parts.append(f"{k}={_jsonable(ev[k])}")
    return " ".join(parts)[:500]


def _level(event: str) -> str:
    if event in ERROR_EVENTS:
        return "error"
    if event in WARNING_EVENTS:
        return "warning"
    return "info"


async def _heartbeat(
    db: AsyncSession, status: BotStatus, adapter: MT5Adapter, settings: Settings
) -> None:
    ok, why = adapter.assess(status)
    details = {
        "source": adapter.source.kind,
        "generated_at": status.generated_at.isoformat(),
        "stale_after_s": settings.MT5_STALE_SECONDS,
        "mode": status.bot.mode,
        "symbols": status.bot.symbols,
        "exec_tf": status.bot.exec_tf,
        "dry": status.bot.dry,
        "ok": ok,
        "detail": why,
        "halt": _jsonable(status.halt),
        "agents": {
            s: {
                "strategy": a.strategy,
                "mt5": a.mt5,
                "alive": a.alive,
                "market_open": a.market_open,
                "positions": len(a.positions),
                "status": a.health.get("status"),
                "last_signal": a.health.get("last_signal"),
            }
            for s, a in status.agents.items()
        },
        "export_errors": status.errors,
    }
    values = {
        "id": MT5_BOT.id,
        "name": MT5_BOT.name,
        "kind": MT5_BOT.kind,
        "state": bot_state(status, ok),
        "version": (status.bot.version or "")[:64] or None,
        "host": "aureon-mt5-bot",
        "pid": status.bot.pid,
        "details": details,
        "last_heartbeat_at": status.generated_at,
        "last_activity_at": func.now(),
    }
    ins = insert(Agent).values(**values)
    await db.execute(
        ins.on_conflict_do_update(
            index_elements=[Agent.id],
            # never touches `paused` (admin-owned) or `registered_at`
            set_={k: ins.excluded[k] for k in values if k not in ("id", "last_activity_at")}
            | {"last_activity_at": func.now()},
        )
    )


async def _logs(db: AsyncSession, status: BotStatus) -> int:
    events = [e for e in status.events if isinstance(e.get("t"), int) and e.get("event")]
    if not events:
        return 0
    first = datetime.fromtimestamp(min(e["t"] for e in events), UTC)
    seen = set(
        (
            await db.scalars(
                select(AgentLog.context["key"].astext).where(
                    AgentLog.agent_id == MT5_BOT.id, AgentLog.created_at >= first
                )
            )
        ).all()
    )
    rows = []
    for ev in events:
        key = event_key(ev)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "agent_id": MT5_BOT.id,
                "level": _level(str(ev["event"])),
                "message": _message(ev),
                "context": {"key": key, "source": "mt5-bot-journal", "event": _jsonable(ev)},
                "created_at": datetime.fromtimestamp(ev["t"], UTC),
            }
        )
    if rows:
        await db.execute(insert(AgentLog).values(rows))
    return len(rows)


async def _strategy_version(db: AsyncSession, mode: str, bot_version: str | None) -> Any:
    name = f"mt5:{mode}"[:100]
    sid = await db.scalar(
        insert(Strategy)
        .values(name=name, description=f"Aureon MT5 bot strategy {mode} (observed, not run here)")
        .on_conflict_do_update(constraint="uq_strategies_name", set_={"name": name})
        .returning(Strategy.id)
    )
    return await db.scalar(
        insert(StrategyVersion)
        .values(
            strategy_id=sid,
            version=1,
            code_ref=f"aureon_agentic_bot:{mode}"[:100],
            params={"source": "mt5-bot", "first_seen_bot_version": bot_version},
        )
        .on_conflict_do_update(
            constraint="uq_strategy_versions_strategy_id_version", set_={"version": 1}
        )
        .returning(StrategyVersion.id)
    )


async def _instrument_ids(db: AsyncSession, symbols: set[str]) -> dict[str, int]:
    if not symbols:
        return {}
    rows = await db.execute(
        select(Instrument.broker_instrument_id, Instrument.id).where(
            Instrument.broker == "mt5", Instrument.broker_instrument_id.in_(symbols)
        )
    )
    return {str(k): int(v) for k, v in rows.all()}


async def _signals(db: AsyncSession, status: BotStatus) -> dict[str, Any]:
    sig_events = [e for e in status.events if e.get("event") == "signal"]
    counts: dict[str, Any] = {"seen": len(sig_events), "inserted": 0, "unknown_instrument": []}
    if not sig_events:
        return counts
    inst = await _instrument_ids(db, {str(e.get("symbol")) for e in sig_events})
    versions: dict[str, Any] = {}
    skipped = 0
    for ev in sig_events:
        sym = str(ev.get("symbol") or "")
        side = SIDES.get(str(ev.get("side") or "").upper())
        bar = ev.get("bar")
        tf = status.timeframe(sym)
        mode = str(ev.get("mode") or status.bot.mode or "")
        if side is None or not isinstance(bar, int) or tf is None or not mode:
            skipped += 1
            continue
        if sym not in inst:
            if sym not in counts["unknown_instrument"]:
                counts["unknown_instrument"].append(sym)
            continue
        if mode not in versions:
            versions[mode] = await _strategy_version(db, mode, status.bot.version)
        candle_time = status.server_to_utc(bar, sym)
        inputs = {k: v for k, v in ev.items() if k not in ("event",)}
        inputs["bot_version"] = status.bot.version
        inputs["offset_h"] = status.offset_hours(sym)
        res = await db.execute(
            insert(TradingSignal)
            .values(
                strategy_version_id=versions[mode],
                instrument_id=inst[sym],
                timeframe=tf.value,
                candle_time=candle_time,
                session=session_for(candle_time).value,
                side=side.value,
                inputs=_jsonable(inputs),
            )
            .on_conflict_do_nothing(constraint="uq_trading_signals_dedupe")
            .returning(TradingSignal.id)
        )
        if res.scalar() is not None:
            counts["inserted"] += 1
    counts["malformed"] = skipped
    return counts


async def _account(db: AsyncSession, settings: Settings) -> BrokerAccount | None:
    if not settings.MT5_ACCOUNT_ID:
        return None
    acct: BrokerAccount | None = await db.scalar(
        select(BrokerAccount).where(
            BrokerAccount.broker == "mt5",
            BrokerAccount.account_id == settings.MT5_ACCOUNT_ID,
            BrokerAccount.is_active.is_(True),
        )
    )
    return acct


async def _account_rows(
    db: AsyncSession,
    status: BotStatus,
    adapter: MT5Adapter,
    account: BrokerAccount,
    settings: Settings,
) -> int:
    ins = insert(Mt5Terminal).values(
        broker_account_id=account.id,
        login=str(settings.MT5_ACCOUNT_ID)[:32],
        server=(settings.MT5_SERVER or "unknown")[:100],
        currency=account.base_currency,
        raw={
            "bot_version": status.bot.version,
            "mode": status.bot.mode,
            "symbols": status.bot.symbols,
            "mt5": {s: a.mt5 for s, a in status.agents.items()},
        },
        last_seen_at=status.generated_at,
    )
    await db.execute(
        ins.on_conflict_do_update(
            index_elements=[Mt5Terminal.broker_account_id],
            set_={
                k: ins.excluded[k] for k in ("login", "server", "currency", "raw", "last_seen_at")
            },
        )
    )
    ok, _ = adapter.assess(status)
    if not ok:  # a stale or disconnected bot's positions are not "as of now"
        return 0
    positions = positions_from(status, account.account_id, account.base_currency)
    if not positions:
        return 0
    inst = await _instrument_ids(db, {p.symbol for p in positions})
    await db.execute(
        insert(PositionSnapshot).values(
            [
                {
                    "broker_account_id": account.id,
                    "instrument_id": inst.get(p.symbol),
                    "symbol": p.symbol,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                    "last_price": p.last_price,
                    "unrealized_pnl": None,
                    "realized_pnl": None,
                    "currency": p.currency,
                    "source": "broker",
                    "as_of": status.generated_at,
                }
                for p in positions
            ]
        )
    )
    return len(positions)


async def sync_status(
    maker: async_sessionmaker[AsyncSession], adapter: MT5Adapter, settings: Settings
) -> dict[str, Any]:
    status = await adapter.status()  # raises BrokerError / StatusInvalid: nothing is written
    async with maker() as db, db.begin():
        await _heartbeat(db, status, adapter, settings)
        logs = await _logs(db, status)
        signals = await _signals(db, status)
        account = await _account(db, settings)
        positions = await _account_rows(db, status, adapter, account, settings) if account else None
    out = {
        "generated_at": status.generated_at.isoformat(),
        "age_s": int(status.age().total_seconds()),
        "logs_inserted": logs,
        "signals": signals,
        "positions": positions,
        "account": "registered" if account else "not registered (MT5_ACCOUNT_ID)",
    }
    log.info(
        "mt5_sync",
        **{k: v for k, v in out.items() if k != "signals"},
        **{"signals_inserted": signals["inserted"]},
    )
    return out


# --- performance (calculated from the export; never from MT5) --------------------------------


def performance(status: BotStatus, now: datetime | None = None) -> dict[str, Any]:
    """Per-symbol stats from journal `exit` events (points) and ema921e trade rows (USD)."""
    now = now or datetime.now(UTC)
    by_symbol: dict[str, dict[str, Any]] = {}
    for ev in status.events:
        if ev.get("event") != "exit" or ev.get("points") is None:
            continue
        try:
            pts = Decimal(str(ev["points"]))
        except ArithmeticError:
            continue
        s = by_symbol.setdefault(
            str(ev.get("symbol")), {"exits": 0, "wins": 0, "losses": 0, "net_points": Decimal(0)}
        )
        s["exits"] += 1
        s["wins"] += pts > 0
        s["losses"] += pts < 0
        s["net_points"] += pts
    for s in by_symbol.values():
        s["win_rate"] = (Decimal(s["wins"]) / s["exits"]).quantize(Decimal("0.0001"))
        s["net_points"] = _jsonable(s["net_points"])
        s["win_rate"] = _jsonable(s["win_rate"])
    e_pnl = Decimal(0)
    e_n = 0
    for row in status.ema921e_trades:
        raw = row.get("pnl_usd")
        if raw in (None, ""):
            continue
        try:
            e_pnl += Decimal(str(raw))
            e_n += 1
        except ArithmeticError:
            continue
    first = min((e["t"] for e in status.events if isinstance(e.get("t"), int)), default=None)
    return {
        "source": "calculated",
        "window_start": datetime.fromtimestamp(first, UTC).isoformat() if first else None,
        "window_end": status.generated_at.isoformat(),
        "exits_by_symbol": by_symbol,
        "ema921e": {"trades": e_n, "net_usd": _jsonable(e_pnl)} if e_n else None,
        "note": "points are price points as the bot reports them; no MT5 deal history is read",
        "computed_at": now.isoformat(),
    }
