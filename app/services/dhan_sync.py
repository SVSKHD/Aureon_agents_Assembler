"""Dhan → database synchronisation (read-only on the broker side).

- **sync_instruments:** instrument master → dhan.instruments + trading.instruments, linked;
  Dhan instruments missing from today's master are marked inactive.
- **reconcile_account:**
  - order book → trading.broker_orders, writing one order_event per new order or per change
    (status / filled qty). Idempotent: an unchanged order writes nothing, and re-runs never
    duplicate events.
  - trade book → trading.executions (deduplicated on the exchange trade id)
  - positions → trading.positions_snapshots (source = broker)
  - funds → dhan.fund_snapshots
  - profile → dhan.account_profiles (token expiry, data plan)
- **backfill_candles:** historical candles → trading.market_candles (upsert, with session tags).

Every event records the Dhan request id and the run's correlation id, so any stored change
can be traced to the API call that observed it.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.trading.dhan import parse
from app.agents.trading.dhan.adapter import DhanAdapter
from app.agents.trading.types import Timeframe, session_for
from app.core.logging import get_logger
from app.models.brokers import DhanAccountProfile, DhanFundSnapshot, DhanInstrument
from app.models.trading import (
    BrokerAccount,
    BrokerOrderRow,
    Execution,
    Instrument,
    MarketCandle,
    OrderEvent,
    OrderIntent,
    PositionSnapshot,
)

log = get_logger(__name__)
BATCH = 2000


def _num(d: Decimal) -> str:
    """Canonical text for a quantity: Decimal("75.000000") and Decimal("75") both give "75"."""
    return format(d.normalize(), "f")


def new_run_id(kind: str) -> str:
    return f"{kind}-{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


# --- instruments --------------------------------------------------------------------------


async def _flush(
    db: AsyncSession, infos: list[Any], rows: list[dict[str, Any]], now: datetime
) -> None:
    if not rows:
        return
    d_ins = insert(DhanInstrument).values([{**r, "fetched_at": now} for r in rows])
    await db.execute(
        d_ins.on_conflict_do_update(
            index_elements=[DhanInstrument.exchange_segment, DhanInstrument.security_id],
            set_={
                c: d_ins.excluded[c]
                for c in (
                    "trading_symbol",
                    "instrument",
                    "lot_size",
                    "tick_size",
                    "expiry_date",
                    "strike_price",
                    "option_type",
                    "underlying_security_id",
                    "raw",
                    "fetched_at",
                )
            },
        )
    )
    t_ins = insert(Instrument).values(
        [
            {
                "broker": "dhan",
                "broker_instrument_id": i.broker_instrument_id,
                "symbol": i.symbol,
                "exchange": i.exchange,
                "segment": i.segment,
                "instrument_type": i.instrument_type,
                "currency": i.currency,
                "lot_size": i.lot_size,
                "tick_size": i.tick_size,
                "expiry": i.expiry,
                "strike": i.strike,
                "option_type": i.option_type,
                "underlying": i.underlying,
                "is_active": True,
                "updated_at": now,
            }
            for i in infos
        ]
    )
    await db.execute(
        t_ins.on_conflict_do_update(
            index_elements=[Instrument.broker, Instrument.broker_instrument_id],
            set_={
                c: t_ins.excluded[c]
                for c in (
                    "symbol",
                    "exchange",
                    "segment",
                    "instrument_type",
                    "currency",
                    "lot_size",
                    "tick_size",
                    "expiry",
                    "strike",
                    "option_type",
                    "underlying",
                    "is_active",
                    "updated_at",
                )
            },
        )
    )


async def sync_instruments(
    maker: async_sessionmaker[AsyncSession], adapter: DhanAdapter
) -> dict[str, Any]:
    now = datetime.now(UTC)
    seen = 0
    infos: list[Any] = []
    rows: list[dict[str, Any]] = []
    keys: set[str] = set()
    async with maker() as db, db.begin():
        async for info, row in adapter.iter_master():
            if info.broker_instrument_id in keys:  # master sometimes repeats rows
                continue
            keys.add(info.broker_instrument_id)
            infos.append(info)
            rows.append(row)
            seen += 1
            if len(rows) >= BATCH:
                await _flush(db, infos, rows, now)
                infos, rows = [], []
        await _flush(db, infos, rows, now)
        if seen == 0:
            raise RuntimeError("instrument master was empty; refusing to deactivate everything")
        deactivated = await db.execute(
            update(Instrument)
            .where(
                Instrument.broker == "dhan",
                Instrument.updated_at < now,
                Instrument.is_active.is_(True),
            )
            .values(is_active=False)
        )
        await db.execute(
            text(
                "UPDATE dhan.instruments d SET trading_instrument_id = t.id "
                "FROM trading.instruments t WHERE t.broker = 'dhan' "
                "AND t.broker_instrument_id = d.exchange_segment || ':' || d.security_id "
                "AND d.trading_instrument_id IS DISTINCT FROM t.id"
            )
        )
    return {"instruments": seen, "deactivated": deactivated.rowcount}  # type: ignore[attr-defined]


# --- account reconciliation ---------------------------------------------------------------


async def _instrument_ids(db: AsyncSession, refs: set[str]) -> dict[str, int]:
    if not refs:
        return {}
    rows = await db.execute(
        select(Instrument.broker_instrument_id, Instrument.id).where(
            Instrument.broker == "dhan", Instrument.broker_instrument_id.in_(refs)
        )
    )
    return {r: i for r, i in rows.tuples()}


async def reconcile_account(
    maker: async_sessionmaker[AsyncSession], adapter: DhanAdapter, account: BrokerAccount
) -> dict[str, Any]:
    run_id = new_run_id("recon")
    acct = account.account_id
    now = datetime.now(UTC)
    orders = await adapter.orders(acct)
    orders_req = adapter.client.last_request_id
    trades = await adapter.trades(acct)
    trades_req = adapter.client.last_request_id
    positions = await adapter.positions(acct)
    funds = await adapter.funds(acct)
    profile = await adapter.client.profile()
    counts = {
        "orders_new": 0,
        "orders_changed": 0,
        "orders_unchanged": 0,
        "executions_new": 0,
        "trades_unknown_instrument": 0,
        "positions": len(positions),
    }

    async with maker() as db, db.begin():
        refs = {f"{o.raw.get('exchangeSegment')}:{o.raw.get('securityId')}" for o in orders}
        refs |= {t.broker_instrument_id for t in trades if t.broker_instrument_id}
        refs |= {p.broker_instrument_id for p in positions if p.broker_instrument_id}
        inst_ids = await _instrument_ids(db, refs)

        existing = {
            r.broker_order_id: r
            for r in (
                await db.scalars(
                    select(BrokerOrderRow).where(
                        BrokerOrderRow.broker_account_id == account.id,
                        BrokerOrderRow.broker_order_id.in_([o.order_id for o in orders]),
                    )
                )
            ).all()
        }
        intents = {}
        cids = [o.correlation_id for o in orders if o.correlation_id]
        if cids:
            intents = {
                c: i
                for c, i in (
                    await db.execute(
                        select(OrderIntent.correlation_id, OrderIntent.id).where(
                            OrderIntent.broker_account_id == account.id,
                            OrderIntent.correlation_id.in_(cids),
                        )
                    )
                ).tuples()
            }

        for o in orders:
            ref = f"{o.raw.get('exchangeSegment')}:{o.raw.get('securityId')}"
            values = {
                "symbol": o.symbol,
                "side": o.side.value,
                "order_type": o.order_type.value,
                "quantity": o.quantity,
                "filled_quantity": o.filled_quantity,
                "price": o.price,
                "avg_fill_price": o.avg_fill_price,
                "raw_status": o.raw_status[:32],
                "normalized_status": o.status.value,
                "correlation_id": (o.correlation_id or None),
                "instrument_id": inst_ids.get(ref),
                "order_intent_id": intents.get(o.correlation_id or ""),
                "placed_at": o.placed_at,
                "raw": o.raw,
                "updated_at": now,
            }
            row = existing.get(o.order_id)
            if row is None:
                row = BrokerOrderRow(
                    broker_account_id=account.id, broker_order_id=o.order_id, **values
                )
                db.add(row)
                await db.flush()
                event, before = "broker_order_seen", None
                counts["orders_new"] += 1
            elif (row.raw_status, row.filled_quantity) != (values["raw_status"], o.filled_quantity):
                before = {"status": row.raw_status, "filled": _num(row.filled_quantity)}
                for k, v in values.items():
                    setattr(row, k, v)
                event = "broker_status_changed"
                counts["orders_changed"] += 1
            else:
                counts["orders_unchanged"] += 1
                continue
            db.add(
                OrderEvent(
                    order_intent_id=values["order_intent_id"],
                    broker_order_row_id=row.id,
                    event_type=event,
                    correlation_id=o.correlation_id or run_id,
                    occurred_at=o.updated_at or o.placed_at,
                    payload={
                        "run_id": run_id,
                        "request_id": orders_req,
                        "before": before,
                        "after": {"status": o.raw_status, "filled": _num(o.filled_quantity)},
                    },
                )
            )

        order_rows = {
            r.broker_order_id: r.id
            for r in (
                await db.scalars(
                    select(BrokerOrderRow).where(BrokerOrderRow.broker_account_id == account.id)
                )
            ).all()
        }
        for t in trades:
            inst_id = inst_ids.get(t.broker_instrument_id or "")
            if inst_id is None:
                counts["trades_unknown_instrument"] += 1
                continue
            res = await db.execute(
                insert(Execution)
                .values(
                    id=uuid.uuid4(),
                    broker_account_id=account.id,
                    broker_order_row_id=order_rows.get(t.order_id),
                    instrument_id=inst_id,
                    broker_trade_id=t.trade_id,
                    side=t.side.value,
                    quantity=t.quantity,
                    price=t.price,
                    currency="INR",
                    executed_at=t.executed_at,
                    session=session_for(t.executed_at).value,
                    is_simulated=False,
                )
                .on_conflict_do_nothing(
                    index_elements=["broker_account_id", "broker_trade_id"],
                    index_where=text("broker_trade_id IS NOT NULL"),
                )
            )
            counts["executions_new"] += res.rowcount  # type: ignore[attr-defined]

        for p in positions:
            db.add(
                PositionSnapshot(
                    broker_account_id=account.id,
                    instrument_id=inst_ids.get(p.broker_instrument_id or ""),
                    symbol=p.symbol,
                    quantity=p.quantity,
                    avg_price=p.avg_price,
                    last_price=p.last_price,
                    unrealized_pnl=p.unrealized_pnl,
                    realized_pnl=p.realized_pnl,
                    currency="INR",
                    source="broker",
                    as_of=now,
                )
            )

        r = funds.raw
        db.add(
            DhanFundSnapshot(
                broker_account_id=account.id,
                available_balance=parse.dec(r.get("availabelBalance")),
                sod_limit=parse.dec(r.get("sodLimit")),
                collateral=parse.dec(r.get("collateralAmount")),
                utilized=parse.dec(r.get("utilizedAmount")),
                withdrawable=parse.dec(r.get("withdrawableBalance")),
                raw=r,
                as_of=now,
            )
        )

        prof = insert(DhanAccountProfile).values(
            broker_account_id=account.id,
            dhan_client_id=str(profile.get("dhanClientId", acct)),
            token_expires_at=parse.token_expiry(profile),
            active_segments=[
                s.strip() for s in str(profile.get("activeSegment", "")).split(",") if s.strip()
            ],
            data_plan=str(profile.get("dataPlan") or "")[:32] or None,
            raw=profile,
            refreshed_at=now,
        )
        await db.execute(
            prof.on_conflict_do_update(
                index_elements=[DhanAccountProfile.broker_account_id],
                set_={
                    c: prof.excluded[c]
                    for c in (
                        "dhan_client_id",
                        "token_expires_at",
                        "active_segments",
                        "data_plan",
                        "raw",
                        "refreshed_at",
                    )
                },
            )
        )
    log.info(
        "dhan_reconciled", run_id=run_id, account=str(account.id), trades_req=trades_req, **counts
    )
    return {"run_id": run_id, **counts}


# --- candles ------------------------------------------------------------------------------


async def backfill_candles(
    maker: async_sessionmaker[AsyncSession],
    adapter: DhanAdapter,
    refs: list[str],
    timeframes: list[Timeframe],
    days: int,
) -> dict[str, Any]:
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    total, missing = 0, []
    async with maker() as db:
        ids = await _instrument_ids(db, set(refs))
    for ref in refs:
        inst_id = ids.get(ref)
        if inst_id is None:
            missing.append(ref)
            continue
        for tf in timeframes:
            rows = await adapter.candles(ref, tf, start, end)
            if not rows:
                continue
            async with maker() as db, db.begin():
                for i in range(0, len(rows), BATCH):
                    chunk = rows[i : i + BATCH]
                    stmt = insert(MarketCandle).values(
                        [
                            {
                                "instrument_id": inst_id,
                                "timeframe": tf.value,
                                "ts": c.ts,
                                "open": c.open,
                                "high": c.high,
                                "low": c.low,
                                "close": c.close,
                                "volume": c.volume,
                                "oi": c.oi,
                                "session": c.session.value,
                                "source": "broker",
                            }
                            for c in chunk
                        ]
                    )
                    await db.execute(
                        stmt.on_conflict_do_update(
                            constraint="pk_market_candles",
                            set_={
                                k: stmt.excluded[k]
                                for k in ("open", "high", "low", "close", "volume", "oi")
                            },
                        )
                    )
            total += len(rows)
    return {"candles": total, "unknown_instruments": missing}


async def dhan_accounts(
    maker: async_sessionmaker[AsyncSession], client_id: str
) -> list[BrokerAccount]:
    async with maker() as db:
        return list(
            (
                await db.scalars(
                    select(BrokerAccount).where(
                        and_(
                            BrokerAccount.broker == "dhan",
                            BrokerAccount.account_id == client_id,
                            BrokerAccount.is_active.is_(True),
                        )
                    )
                )
            ).all()
        )
