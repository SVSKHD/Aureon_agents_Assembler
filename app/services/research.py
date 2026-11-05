"""Trading Phase D: the research agents' database work (deterministic; no orders).

- **Market data:** adapter candles → trading.market_candles (upsert, UTC, session tag);
  Dhan feed ticks → 1-minute candles; intraday gaps filled with flagged rows; freshness.
- **Technical analysis:** indicators on closed, non-gap candles → trading.indicator_values.
- **Signals:** versioned strategies → trading.trading_signals (BUY / SELL / NO_TRADE, with
  inputs), deduplicated on (strategy_version, instrument, timeframe, candle_time).
- **Risk:** every BUY/SELL signal is checked against each active account of that broker →
  trading.risk_decisions. An approval creates nothing: order intents are Phase F.
- **Backtests:** queued trading.backtest_runs → trades + metrics.

Nothing here can place, modify or cancel an order, or create an order intent.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.research import backtest as bt
from app.agents.research import indicators as ind
from app.agents.research import strategies
from app.agents.trading.base import BrokerAdapter, BrokerError, NotSupported
from app.agents.trading.types import Timeframe, session_for
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.brokers import DhanFundSnapshot, Mt5Terminal
from app.models.trading import (
    BacktestRun,
    BacktestTrade,
    BrokerAccount,
    IndicatorValue,
    Instrument,
    MarketCandle,
    PositionSnapshot,
    RiskDecision,
    RiskPolicy,
    Strategy,
    StrategyVersion,
    TradingSignal,
)

log = get_logger(__name__)

TF_SECONDS = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
}
INTRADAY = {tf for tf in TF_SECONDS if tf is not Timeframe.D1}
BATCH = 2000


def tf_delta(tf: Timeframe | str) -> timedelta:
    return timedelta(seconds=TF_SECONDS[Timeframe(tf)])


def _num(d: Decimal) -> str:
    return format(d.normalize(), "f")


# --- market data --------------------------------------------------------------------------


async def load_candles(
    db: AsyncSession,
    instrument_id: int,
    tf: Timeframe | str,
    *,
    limit: int,
    now: datetime | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[MarketCandle]:
    """Closed, non-gap candles, ascending. A candle is closed once ts + timeframe <= now."""
    now = now or datetime.now(UTC)
    tf = Timeframe(tf)
    q = select(MarketCandle).where(
        MarketCandle.instrument_id == instrument_id,
        MarketCandle.timeframe == tf.value,
        MarketCandle.is_gap.is_(False),
        MarketCandle.ts <= now - tf_delta(tf),
    )
    if start is not None:
        q = q.where(MarketCandle.ts >= start)
    if end is not None:
        q = q.where(MarketCandle.ts < end)
    rows = (await db.scalars(q.order_by(MarketCandle.ts.desc()).limit(limit))).all()
    return list(reversed(rows))


async def upsert_candles(
    db: AsyncSession, instrument_id: int, tf: Timeframe, candles: Sequence[Any], source: str
) -> int:
    for i in range(0, len(candles), BATCH):
        chunk = candles[i : i + BATCH]
        stmt = insert(MarketCandle).values(
            [
                {
                    "instrument_id": instrument_id,
                    "timeframe": tf.value,
                    "ts": c.ts,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                    "oi": getattr(c, "oi", None),
                    "session": session_for(c.ts).value,
                    "source": source,
                    "is_gap": False,
                }
                for c in chunk
            ]
        )
        # A real candle always replaces a gap filler or a tick-built candle.
        await db.execute(
            stmt.on_conflict_do_update(
                constraint="pk_market_candles",
                set_={
                    k: stmt.excluded[k]
                    for k in ("open", "high", "low", "close", "volume", "oi", "source", "is_gap")
                },
            )
        )
    return len(candles)


async def refresh_candles(
    maker: async_sessionmaker[AsyncSession],
    adapter: BrokerAdapter | None,
    instrument: Instrument,
    tf: Timeframe,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch candles newer than the last stored one (or from the oldest open gap) from the
    broker adapter."""
    now = now or datetime.now(UTC)
    if adapter is None:
        return {"supported": False, "detail": f"no {instrument.broker} adapter configured"}
    async with maker() as db:
        last = await db.scalar(
            select(func.max(MarketCandle.ts)).where(
                MarketCandle.instrument_id == instrument.id,
                MarketCandle.timeframe == tf.value,
                MarketCandle.is_gap.is_(False),
            )
        )
        # Re-ask for any bar still filled as a gap, so late data replaces the filler.
        first_gap = await db.scalar(
            select(func.min(MarketCandle.ts)).where(
                MarketCandle.instrument_id == instrument.id,
                MarketCandle.timeframe == tf.value,
                MarketCandle.is_gap.is_(True),
                MarketCandle.ts >= now - timedelta(days=settings.RESEARCH_BACKFILL_DAYS),
            )
        )
    start = last if last is not None else now - timedelta(days=settings.RESEARCH_BACKFILL_DAYS)
    if first_gap is not None:
        start = min(start, first_gap)
    try:
        rows = await adapter.candles(instrument.broker_instrument_id, tf, start, now)
    except NotSupported as exc:
        return {"supported": False, "detail": str(exc)}
    async with maker() as db, db.begin():
        n = await upsert_candles(db, instrument.id, tf, rows, "broker")
        gaps = await mark_gaps(db, instrument.id, tf, since=start)
    return {"supported": True, "candles": n, "gaps": gaps}


async def mark_gaps(db: AsyncSession, instrument_id: int, tf: Timeframe, *, since: datetime) -> int:
    """Intraday only: a missing bar between the first and last real bar of the same UTC day
    is inserted as a flagged filler (O=H=L=C = previous close, volume 0, is_gap). Bars outside
    a day's traded range (nights, holidays) are never treated as gaps. Indicators, signals and
    backtests skip fillers."""
    if tf not in INTRADAY:
        return 0
    step = tf_delta(tf)
    rows = (
        await db.execute(
            select(MarketCandle.ts, MarketCandle.close)
            .where(
                MarketCandle.instrument_id == instrument_id,
                MarketCandle.timeframe == tf.value,
                MarketCandle.ts >= since - timedelta(days=1),
            )
            .order_by(MarketCandle.ts)
        )
    ).all()
    fillers: list[dict[str, Any]] = []
    for (t0, c0), (t1, _) in itertools.pairwise(rows):
        if t0.date() != t1.date():
            continue
        t = t0 + step
        while t < t1:
            fillers.append(
                {
                    "instrument_id": instrument_id,
                    "timeframe": tf.value,
                    "ts": t,
                    "open": c0,
                    "high": c0,
                    "low": c0,
                    "close": c0,
                    "volume": Decimal(0),
                    "session": session_for(t).value,
                    "source": "gap",
                    "is_gap": True,
                }
            )
            t += step
    for i in range(0, len(fillers), BATCH):
        await db.execute(
            insert(MarketCandle).values(fillers[i : i + BATCH]).on_conflict_do_nothing()
        )
    return len(fillers)


@dataclass(frozen=True, slots=True)
class Freshness:
    latest: datetime | None
    age_s: float | None
    stale: bool
    limit_s: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "latest": self.latest.isoformat() if self.latest else None,
            "age_s": round(self.age_s, 1) if self.age_s is not None else None,
            "stale": self.stale,
            "limit_s": self.limit_s,
        }


async def freshness(
    db: AsyncSession,
    instrument_id: int,
    tf: Timeframe | str,
    settings: Settings,
    now: datetime | None = None,
) -> Freshness:
    """Stale when the newest real candle closed more than RESEARCH_STALE_BARS bars ago."""
    now = now or datetime.now(UTC)
    tf = Timeframe(tf)
    latest = await db.scalar(
        select(func.max(MarketCandle.ts)).where(
            MarketCandle.instrument_id == instrument_id,
            MarketCandle.timeframe == tf.value,
            MarketCandle.is_gap.is_(False),
        )
    )
    limit = settings.RESEARCH_STALE_BARS * TF_SECONDS[tf]
    if latest is None:
        return Freshness(None, None, True, limit)
    age = (now - (latest + tf_delta(tf))).total_seconds()
    return Freshness(latest, age, age > limit, limit)


class TickAggregator:
    """Dhan feed packets (the JSON published on `dhan:ticks`) → 1-minute candles.

    Uses last price + last trade time. Volume is the change in the day's cumulative volume
    (0 when the feed mode carries no volume). A minute is emitted once a packet for a later
    minute arrives, so an emitted candle is always complete.
    """

    def __init__(self) -> None:
        self.open: dict[str, dict[str, Any]] = {}
        self.last_volume: dict[str, Decimal] = {}

    def add(self, packet: dict[str, Any]) -> list[tuple[str, Any]]:
        if packet.get("ltp") in (None, "") or not packet.get("ltt"):
            return []
        if not packet.get("segment") or not str(packet.get("security_id") or "").isdigit():
            return []  # not an instrument we can key a candle on
        key = f"{packet.get('segment')}:{packet.get('security_id')}"
        try:
            price = Decimal(str(packet["ltp"]))
            ts = datetime.fromisoformat(str(packet["ltt"])).astimezone(UTC)
        except (ValueError, ArithmeticError):
            return []
        if not price.is_finite() or price <= 0:
            return []
        minute = ts.replace(second=0, microsecond=0)
        vol = Decimal(0)
        if packet.get("volume") not in (None, ""):
            cum = Decimal(str(packet["volume"]))
            prev = self.last_volume.get(key)
            vol = max(cum - prev, Decimal(0)) if prev is not None else Decimal(0)
            self.last_volume[key] = cum
        done: list[tuple[str, Any]] = []
        cur = self.open.get(key)
        if cur is not None and minute < cur["ts"]:
            return []  # late packet for an already-emitted minute: ignored
        if cur is not None and minute > cur["ts"]:
            done.append((key, _Bar(**cur)))
            cur = None
        if cur is None:
            self.open[key] = {
                "ts": minute,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": vol,
            }
        else:
            cur["high"] = max(cur["high"], price)
            cur["low"] = min(cur["low"], price)
            cur["close"] = price
            cur["volume"] += vol
        return done


@dataclass(frozen=True, slots=True)
class _Bar:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    oi: Decimal | None = None


async def store_tick_bars(
    maker: async_sessionmaker[AsyncSession], bars: list[tuple[str, Any]]
) -> int:
    """Tick-built 1m candles; never overwrite a broker candle for the same minute."""
    if not bars:
        return 0
    async with maker() as db, db.begin():
        rows = (
            await db.execute(
                select(Instrument.broker_instrument_id, Instrument.id).where(
                    Instrument.broker == "dhan",
                    Instrument.broker_instrument_id.in_({k for k, _ in bars}),
                )
            )
        ).all()
        ids: dict[str, int] = {str(r[0]): int(r[1]) for r in rows}
        n = 0
        for key, b in bars:
            if key not in ids:
                continue
            stmt = insert(MarketCandle).values(
                instrument_id=ids[key],
                timeframe=Timeframe.M1.value,
                ts=b.ts,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                session=session_for(b.ts).value,
                source="ticks",
            )
            await db.execute(
                stmt.on_conflict_do_update(
                    constraint="pk_market_candles",
                    set_={
                        k: stmt.excluded[k]
                        for k in ("open", "high", "low", "close", "volume", "source", "is_gap")
                    },
                    where=MarketCandle.source.in_(("ticks", "gap")),
                )
            )
            n += 1
    return n


# --- technical analysis -------------------------------------------------------------------


def indicator_set(instrument: Instrument, settings: Settings, extra: set[int]) -> ind.IndicatorSet:
    return ind.IndicatorSet(
        ema_periods=tuple(sorted(set(settings.RESEARCH_EMA_PERIODS) | extra)),
        vwap_tz="Asia/Kolkata" if instrument.broker == "dhan" else "UTC",
    )


async def compute_indicators(
    maker: async_sessionmaker[AsyncSession],
    instrument: Instrument,
    tf: Timeframe,
    settings: Settings,
    extra_ema: set[int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    async with maker() as db:
        candles = await load_candles(
            db, instrument.id, tf, limit=settings.RESEARCH_LOOKBACK_BARS, now=now
        )
    if not candles:
        return {"bars": 0, "rows": 0}
    spec = indicator_set(instrument, settings, extra_ema or set())
    rows = ind.rows_for_storage(ind.compute(ind.frame(candles), spec))
    async with maker() as db, db.begin():
        for i in range(0, len(rows), BATCH):
            stmt = insert(IndicatorValue).values(
                [
                    {
                        "instrument_id": instrument.id,
                        "timeframe": tf.value,
                        "name": name,
                        "ts": ts,
                        "values": values,
                    }
                    for name, ts, values in rows[i : i + BATCH]
                ]
            )
            await db.execute(
                stmt.on_conflict_do_update(
                    constraint="pk_indicator_values",
                    set_={"values": stmt.excluded["values"], "computed_at": func.now()},
                )
            )
    return {"bars": len(candles), "rows": len(rows), "last": candles[-1].ts.isoformat()}


# --- signals ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WatchItem:
    version: StrategyVersion
    strategy_name: str
    instrument: Instrument
    timeframe: Timeframe


async def watch_items(db: AsyncSession) -> list[WatchItem]:
    """Every (active strategy version with a known code_ref) x (its instruments x timeframes)."""
    latest = (
        select(StrategyVersion.strategy_id, func.max(StrategyVersion.version).label("v"))
        .group_by(StrategyVersion.strategy_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(StrategyVersion, Strategy.name)
            .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
            .join(
                latest,
                and_(
                    latest.c.strategy_id == StrategyVersion.strategy_id,
                    latest.c.v == StrategyVersion.version,
                ),
            )
            .where(Strategy.is_active.is_(True))
        )
    ).all()
    items: list[WatchItem] = []
    wanted: list[tuple[StrategyVersion, str, int, str]] = []
    for v, name in rows:
        if strategies.get(v.code_ref) is None:
            continue
        wanted += [(v, name, i, t) for i, t in strategies.watch_list(v.params or {})]
    ids = {i for _, _, i, _ in wanted}
    insts = {
        r.id: r
        for r in (
            await db.scalars(
                select(Instrument).where(Instrument.id.in_(ids), Instrument.is_active.is_(True))
            )
        ).all()
    }
    for v, name, i, t in wanted:
        if i in insts and t in {tf.value for tf in Timeframe}:
            items.append(WatchItem(v, name, insts[i], Timeframe(t)))
    return items


async def evaluate_signal(
    maker: async_sessionmaker[AsyncSession],
    item: WatchItem,
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Decision on the newest closed candle. Returns the new signal id (None if deduped)."""
    fn = strategies.get(item.version.code_ref)
    assert fn is not None
    async with maker() as db:
        candles = await load_candles(
            db, item.instrument.id, item.timeframe, limit=settings.RESEARCH_LOOKBACK_BARS, now=now
        )
    if len(candles) < 2:
        return {"signal_id": None, "detail": "not enough candles"}
    d = fn(ind.frame(candles), item.version.params or {})
    i = len(candles) - 1
    side = str(d.side.iloc[i])
    candle_time = candles[i].ts
    inputs = {
        **d.inputs_at(i),
        "strategy": item.strategy_name,
        "version": item.version.version,
        "code_ref": item.version.code_ref,
        "params": {k: v for k, v in (item.version.params or {}).items()},
        "bars": len(candles),
    }
    async with maker() as db, db.begin():
        new_id = await db.scalar(
            insert(TradingSignal)
            .values(
                strategy_version_id=item.version.id,
                instrument_id=item.instrument.id,
                timeframe=item.timeframe.value,
                candle_time=candle_time,
                session=session_for(candle_time).value,
                side=side,
                inputs=inputs,
            )
            .on_conflict_do_nothing(constraint="uq_trading_signals_dedupe")
            .returning(TradingSignal.id)
        )
    return {"signal_id": new_id, "side": side, "candle_time": candle_time.isoformat()}


# --- risk ---------------------------------------------------------------------------------


@dataclass(slots=True)
class Checks:
    items: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, ok: bool | None, detail: str) -> None:
        self.items.append({"name": name, "ok": ok, "detail": detail})

    @property
    def failed(self) -> list[str]:
        return [c["name"] for c in self.items if c["ok"] is False]


async def _account_state_age(
    db: AsyncSession, account: BrokerAccount, now: datetime
) -> float | None:
    if account.broker == "dhan":
        ts = await db.scalar(
            select(func.max(DhanFundSnapshot.as_of)).where(
                DhanFundSnapshot.broker_account_id == account.id
            )
        )
    else:
        ts = await db.scalar(
            select(Mt5Terminal.last_seen_at).where(Mt5Terminal.broker_account_id == account.id)
        )
    return (now - ts).total_seconds() if ts is not None else None


async def _latest_positions(db: AsyncSession, account_id: uuid.UUID) -> list[PositionSnapshot]:
    last = await db.scalar(
        select(func.max(PositionSnapshot.as_of)).where(
            PositionSnapshot.broker_account_id == account_id
        )
    )
    if last is None:
        return []
    return list(
        (
            await db.scalars(
                select(PositionSnapshot).where(
                    PositionSnapshot.broker_account_id == account_id,
                    PositionSnapshot.as_of == last,
                )
            )
        ).all()
    )


def _policy_snapshot(p: RiskPolicy) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "max_daily_loss": _num(p.max_daily_loss),
        "max_exposure": _num(p.max_exposure),
        "max_position_size": _num(p.max_position_size),
        "max_drawdown_pct": _num(p.max_drawdown_pct),
        "allowed_sessions": list(p.allowed_sessions),
        "kill_switch": p.kill_switch,
    }


async def assess_signal(
    maker: async_sessionmaker[AsyncSession],
    signal_id: uuid.UUID,
    adapters: dict[str, BrokerAdapter],
    settings: Settings,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """One risk decision per active account of the signal's broker. Rejects on any failed
    check, including stale credentials, data or account state."""
    now = now or datetime.now(UTC)
    async with maker() as db:
        sig = await db.get(TradingSignal, signal_id)
        if sig is None or sig.side not in ("BUY", "SELL"):
            return []
        inst = await db.get(Instrument, sig.instrument_id)
        assert inst is not None
        accounts = (
            await db.scalars(
                select(BrokerAccount).where(
                    BrokerAccount.broker == inst.broker, BrokerAccount.is_active.is_(True)
                )
            )
        ).all()
        fresh = await freshness(db, inst.id, sig.timeframe, settings, now)
        last_close = await db.scalar(
            select(MarketCandle.close)
            .where(
                MarketCandle.instrument_id == inst.id,
                MarketCandle.timeframe == sig.timeframe,
                MarketCandle.is_gap.is_(False),
            )
            .order_by(MarketCandle.ts.desc())
            .limit(1)
        )
    adapter = adapters.get(inst.broker)
    health_ok: bool | None = None
    health_detail = f"no {inst.broker} adapter configured"
    if adapter is not None:
        try:
            h = await adapter.health()
            health_ok, health_detail = h.ok, h.detail or "ok"
        except (BrokerError, TimeoutError) as exc:
            health_ok, health_detail = False, str(exc)[:200]

    out = []
    for account in accounts:
        c = Checks()
        async with maker() as db, db.begin():
            policy = await db.scalar(
                select(RiskPolicy).where(RiskPolicy.broker_account_id == account.id)
            )
            c.add("policy_present", policy is not None, "risk policy set" if policy else "none")
            if policy is not None:
                c.add(
                    "kill_switch_off", not policy.kill_switch, f"kill_switch={policy.kill_switch}"
                )
                sess = session_for(now).value
                c.add(
                    "session_allowed",
                    sess in policy.allowed_sessions,
                    f"now={sess} allowed={list(policy.allowed_sessions)}",
                )
            sig_age = (now - (sig.candle_time + tf_delta(sig.timeframe))).total_seconds()
            max_sig_age = settings.RISK_SIGNAL_MAX_AGE_BARS * TF_SECONDS[Timeframe(sig.timeframe)]
            c.add(
                "signal_fresh",
                sig_age <= max_sig_age,
                f"age {int(sig_age)} s (max {max_sig_age:g})",
            )
            c.add("data_fresh", not fresh.stale, f"newest candle age {fresh.age_s} s")
            c.add("credentials_ok", health_ok is True, health_detail)
            acct_age = await _account_state_age(db, account, now)
            c.add(
                "account_state_fresh",
                acct_age is not None and acct_age <= settings.RISK_ACCOUNT_STALE_SECONDS,
                "no account snapshot" if acct_age is None else f"age {int(acct_age)} s",
            )
            qty = inst.lot_size
            positions = await _latest_positions(db, account.id)
            price = last_close if last_close is not None else None
            if policy is not None:
                c.add(
                    "position_size",
                    qty <= policy.max_position_size,
                    f"proposed {_num(qty)} (max {_num(policy.max_position_size)})",
                )
                gross = sum(
                    (abs(p.quantity) * (p.last_price or p.avg_price) for p in positions),
                    Decimal(0),
                )
                if price is None:
                    c.add("exposure", False, "no price for the instrument")
                else:
                    total = gross + qty * price
                    c.add(
                        "exposure",
                        total <= policy.max_exposure,
                        f"gross {_num(gross)} + proposed {_num(qty * price)} "
                        f"(max {_num(policy.max_exposure)})",
                    )
                pnl = sum(
                    ((p.unrealized_pnl or 0) + (p.realized_pnl or 0) for p in positions),
                    Decimal(0),
                )
                c.add(
                    "daily_loss",
                    pnl >= -policy.max_daily_loss,
                    f"open P/L {_num(Decimal(pnl))} (limit -{_num(policy.max_daily_loss)})",
                )
                c.add("drawdown", None, "not evaluated: no equity history yet (Phase F)")
            cost_bps = (
                settings.RISK_SPREAD_BPS
                + 2 * settings.RISK_SLIPPAGE_BPS
                + 2 * settings.RISK_FEE_BPS
            )
            c.add(
                "costs",
                cost_bps <= settings.RISK_MAX_COST_BPS,
                f"round trip {_num(cost_bps)} bps (max {_num(settings.RISK_MAX_COST_BPS)})",
            )
            approved = not c.failed
            reason = "all checks passed" if approved else "rejected: " + ", ".join(c.failed)
            row = RiskDecision(
                broker_account_id=account.id,
                signal_id=sig.id,
                policy_id=policy.id if policy else None,
                approved=approved,
                reason=reason,
                policy_snapshot=_policy_snapshot(policy) if policy else {},
                inputs={
                    "signal": {
                        "id": str(sig.id),
                        "side": sig.side,
                        "timeframe": sig.timeframe,
                        "candle_time": sig.candle_time.isoformat(),
                    },
                    "instrument": {"id": inst.id, "symbol": inst.symbol, "broker": inst.broker},
                    "proposed_quantity": _num(qty),
                    "price": _num(price) if price is not None else None,
                    "assumptions_bps": {
                        "spread": _num(settings.RISK_SPREAD_BPS),
                        "slippage": _num(settings.RISK_SLIPPAGE_BPS),
                        "fee": _num(settings.RISK_FEE_BPS),
                    },
                    "data": fresh.as_dict(),
                    "evaluated_at": now.isoformat(),
                },
                checks=c.items,
            )
            db.add(row)
        out.append({"account": str(account.id), "approved": approved, "reason": reason})
    return out


# --- backtests ----------------------------------------------------------------------------


def grid_params(base: dict[str, Any], grid: dict[str, list[Any]], cap: int) -> list[dict[str, Any]]:
    if not grid:
        return [dict(base)]
    keys = sorted(grid)
    combos = list(itertools.product(*(grid[k] for k in keys)))
    if len(combos) > cap:
        raise ValueError(f"parameter grid has {len(combos)} combinations (max {cap})")
    return [{**base, **dict(zip(keys, combo, strict=True))} for combo in combos]


async def claim_backtest(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID | None:
    async with maker() as db, db.begin():
        run_id = await db.scalar(
            select(BacktestRun.id)
            .where(BacktestRun.status == "queued")
            .order_by(BacktestRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if run_id is None:
            return None
        await db.execute(
            update(BacktestRun)
            .where(BacktestRun.id == run_id)
            .values(status="running", started_at=func.now())
        )
    return run_id


async def run_backtest(
    maker: async_sessionmaker[AsyncSession], run_id: uuid.UUID, settings: Settings
) -> dict[str, Any]:
    async with maker() as db:
        run = await db.get(BacktestRun, run_id)
        assert run is not None
        version = await db.get(StrategyVersion, run.strategy_version_id)
        inst = await db.get(Instrument, run.instrument_id)
        assert version is not None and inst is not None
        candles = await load_candles(
            db,
            inst.id,
            run.timeframe,
            limit=settings.BACKTEST_MAX_BARS,
            start=run.period_start,
            end=run.period_end,
        )
    try:
        fn = strategies.get(version.code_ref)
        if fn is None:
            raise ValueError(f"strategy {version.code_ref!r} has no implementation")
        params = run.params or {}
        qty = Decimal(str(params.get("quantity") or inst.lot_size))
        costs = bt.Costs(**{k: Decimal(str(v)) for k, v in (run.costs or {}).items()})
        # CPU-bound: off the event loop so the worker's other agents keep running.
        result = await asyncio.to_thread(bt.run, candles, fn, params, quantity=qty, costs=costs)
        result.metrics.update(
            {
                "bars": len(candles),
                "first_bar": candles[0].ts.isoformat() if candles else None,
                "last_bar": candles[-1].ts.isoformat() if candles else None,
                "quantity": _num(qty),
            }
        )
    except Exception as exc:  # recorded on the run, never crashes the worker
        async with maker() as db, db.begin():
            await db.execute(
                update(BacktestRun)
                .where(BacktestRun.id == run_id)
                .values(status="failed", error=str(exc)[:1000], finished_at=func.now())
            )
        return {"status": "failed", "error": str(exc)[:200]}
    async with maker() as db, db.begin():
        if result.trades:
            await db.execute(
                insert(BacktestTrade).values(
                    [
                        {
                            "backtest_run_id": run_id,
                            "side": t.side,
                            "quantity": t.quantity,
                            "entry_time": t.entry_time,
                            "entry_price": t.entry_price,
                            "exit_time": t.exit_time,
                            "exit_price": t.exit_price,
                            "pnl": t.pnl,
                            "fees": t.fees,
                            "session": t.session,
                        }
                        for t in result.trades
                    ]
                )
            )
        await db.execute(
            update(BacktestRun)
            .where(BacktestRun.id == run_id)
            .values(status="done", metrics=result.metrics, finished_at=func.now())
        )
    return {"status": "done", "trades": len(result.trades)}
