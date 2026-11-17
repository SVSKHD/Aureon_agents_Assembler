"""Portfolio signal map, rebalance preview and holding notifications (docs/37 §6).

Read-only. Holdings come from the latest trading.holding_snapshots of each Dhan account the
user can see (written by the dhan-reconcile job), cash from the latest dhan.fund_snapshots,
signal states from the Stock Signals runs. All arithmetic is in app/agents/research/
portfolio.py (deterministic, Decimal). Nothing here places, changes or cancels an order.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import false, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.research import portfolio as pf
from app.agents.trading.paper import accounting as acc
from app.auth.deps import CurrentSession
from app.core.config import Settings
from app.models.brokers import DhanFundSnapshot
from app.models.stock_signals import HoldingSnapshot, StockSignalState
from app.models.trading import BrokerAccount, Instrument
from app.services import stock_rankings
from app.services.stock_signal_notifications import deep_link, insert_many
from app.services.stock_signals import RunResult
from app.services.trading import visible_accounts

BULL = {"BULLISH_CONFIRMED", "BULLISH_TREND"}
BEAR = {"BEARISH_CONFIRMED", "BEARISH_TREND"}


def _group(state: str | None) -> str:
    return "bull" if state in BULL else "bear" if state in BEAR else "neutral"


async def _latest_holdings(
    db: AsyncSession, account_ids: Iterable[uuid.UUID]
) -> list[HoldingSnapshot]:
    ids = list(account_ids)
    if not ids:
        return []
    latest = (
        select(HoldingSnapshot.broker_account_id, func.max(HoldingSnapshot.as_of).label("as_of"))
        .where(HoldingSnapshot.broker_account_id.in_(ids))
        .group_by(HoldingSnapshot.broker_account_id)
        .subquery()
    )
    rows = await db.scalars(
        select(HoldingSnapshot)
        .join(
            latest,
            (latest.c.broker_account_id == HoldingSnapshot.broker_account_id)
            & (latest.c.as_of == HoldingSnapshot.as_of),
        )
        .where(HoldingSnapshot.quantity > 0)
        .order_by(HoldingSnapshot.symbol)
    )
    return list(rows.all())


async def _dhan_accounts(db: AsyncSession, current: CurrentSession | None) -> list[BrokerAccount]:
    q = visible_accounts(current) if current is not None else select(BrokerAccount)
    q = q.where(BrokerAccount.broker == "dhan", BrokerAccount.is_active.is_(True))
    return list((await db.scalars(q)).all())


async def holders(db: AsyncSession) -> dict[int, set[uuid.UUID]]:
    """Instrument id → the users who hold it (owners of the Dhan accounts holding it)."""
    accounts = {a.id: a.owner_user_id for a in await _dhan_accounts(db, None)}
    out: dict[int, set[uuid.UUID]] = {}
    for h in await _latest_holdings(db, accounts):
        if h.instrument_id is not None:
            out.setdefault(h.instrument_id, set()).add(accounts[h.broker_account_id])
    return out


async def mark_in_holdings(db: AsyncSession, held: Iterable[int]) -> None:
    ids = list(set(held))
    flag = StockSignalState.instrument_id.in_(ids) if ids else false()
    await db.execute(
        update(StockSignalState)
        .where(StockSignalState.in_holdings.is_distinct_from(flag))
        .values(in_holdings=flag)
    )


async def _cash(db: AsyncSession, account_ids: list[uuid.UUID]) -> tuple[Decimal | None, Any]:
    if not account_ids:
        return None, None
    latest = (
        select(DhanFundSnapshot.broker_account_id, func.max(DhanFundSnapshot.as_of).label("as_of"))
        .where(DhanFundSnapshot.broker_account_id.in_(account_ids))
        .group_by(DhanFundSnapshot.broker_account_id)
        .subquery()
    )
    rows = (
        await db.scalars(
            select(DhanFundSnapshot).join(
                latest,
                (latest.c.broker_account_id == DhanFundSnapshot.broker_account_id)
                & (latest.c.as_of == DhanFundSnapshot.as_of),
            )
        )
    ).all()
    if not rows:
        return None, None
    total = sum((r.available_balance or Decimal(0) for r in rows), Decimal(0))
    return total, min(r.as_of for r in rows)


def _s(d: Decimal | None) -> str | None:
    return None if d is None else format(d, "f")


async def build(
    db: AsyncSession, current: CurrentSession, settings: Settings, tf: str = "15m"
) -> dict[str, Any]:
    """The portfolio signal map for the user's Dhan accounts (all of them for an admin)."""
    accounts = await _dhan_accounts(db, current)
    ids = [a.id for a in accounts]
    holdings = await _latest_holdings(db, ids)
    iids = [h.instrument_id for h in holdings if h.instrument_id is not None]
    states = {
        s.instrument_id: s
        for s in (
            await db.scalars(
                select(StockSignalState).where(
                    StockSignalState.instrument_id.in_(iids), StockSignalState.timeframe == tf
                )
            )
        ).all()
    }
    insts = {
        i.id: i for i in (await db.scalars(select(Instrument).where(Instrument.id.in_(iids)))).all()
    }
    positions, prices = [], {}
    for h in holdings:
        st = states.get(h.instrument_id) if h.instrument_id is not None else None
        # The latest valid price: the newer of Dhan's holding LTP and the last closed candle.
        price, source, at = h.last_price, "dhan_holdings", h.as_of
        if (
            st is not None
            and st.last_price is not None
            and st.candle_close_at is not None
            and (price is None or st.candle_close_at > h.as_of)
        ):
            price, source, at = st.last_price, "candle_close", st.candle_close_at
        prices[h.id] = (price, source, at)
        positions.append(
            pf.Position(
                symbol=h.symbol,
                quantity=h.quantity,
                avg_cost=h.avg_cost,
                price=price,
                instrument_id=h.instrument_id,
                state=st.state if st is not None else None,
                score=st.trend_score if st is not None else None,
            )
        )
    max_w = Decimal(str(settings.PORTFOLIO_MAX_WEIGHT_PCT))
    add_min = Decimal(str(settings.PORTFOLIO_ADD_MIN_SCORE))
    valued, totals = pf.value(positions, max_weight_pct=max_w, add_min_score=add_min)
    cash, cash_as_of = await _cash(db, ids)
    rows = []
    for h, v in zip(holdings, valued, strict=True):
        st = states.get(h.instrument_id) if h.instrument_id is not None else None
        inst = insts.get(h.instrument_id) if h.instrument_id is not None else None
        price, source, at = prices[h.id]
        rows.append(
            {
                "instrument_id": h.instrument_id,
                "symbol": h.symbol,
                "isin": h.isin,
                "exchange": inst.exchange if inst else None,
                "quantity": _s(h.quantity),
                "avg_cost": _s(h.avg_cost),
                "last_price": _s(price),
                "price_source": source if price is not None else None,
                "price_as_of": at if price is not None else None,
                "invested": _s(v.invested),
                "current_value": _s(v.current),
                "unrealized_pnl": _s(v.pnl),
                "unrealized_pnl_pct": _s(v.pnl_pct),
                "allocation_pct": _s(v.allocation_pct),
                "signal_state": st.state if st else None,
                "ema_state": (
                    None
                    if st is None or st.ema_fast is None or st.ema_slow is None
                    else "above"
                    if st.ema_fast > st.ema_slow
                    else "below"
                    if st.ema_fast < st.ema_slow
                    else "equal"
                ),
                "vwap_state": (
                    None
                    if st is None or st.vwap is None or st.last_price is None
                    else "above"
                    if st.last_price > st.vwap
                    else "below"
                    if st.last_price < st.vwap
                    else "at"
                ),
                "trend_score": _s(st.trend_score) if st else None,
                "signal_as_of": st.candle_close_at if st else None,
                "classification": v.classification,
                "reasons": list(v.reasons),
                "deep_link": deep_link(st.event_id if st else None, h.instrument_id),
            }
        )
    snap_as_of = min((h.as_of for h in holdings), default=None)
    return {
        "timeframe": tf,
        "accounts": len(accounts),
        "holdings": rows,
        "totals": {k: _s(v) for k, v in totals.items()},
        "available_cash": _s(cash),
        "sector_exposure": {
            "available": False,
            "reason": "no reliable sector source in the instrument master",
        },
        "freshness": {
            "holdings_as_of": snap_as_of,
            "cash_as_of": cash_as_of,
            "signals_as_of": max(
                (s.candle_close_at for s in states.values() if s.candle_close_at), default=None
            ),
        },
        "classification_counts": {
            c: sum(1 for r in rows if r["classification"] == c) for c in pf.CLASSES
        },
        "disclaimer": (
            "Advisory classifications from deterministic rules; not order instructions. A "
            "bullish crossover alone never justifies replacing a holding."
        ),
    }


async def candidates(
    db: AsyncSession, settings: Settings, tf: str, exclude: set[int | None]
) -> list[pf.Candidate]:
    """Liquid instruments trending bullish with a score at or above PORTFOLIO_ADD_MIN_SCORE,
    not already held: alternatives to look at, never automatic replacements."""
    rows = await stock_rankings.trending(db, tf, "bullish", limit=50)
    out = []
    for r in rows:
        score = r["trend_score"]
        if r["instrument_id"] in exclude or r["last_price"] is None or score is None:
            continue
        if score < Decimal(str(settings.PORTFOLIO_ADD_MIN_SCORE)):
            continue
        out.append(
            pf.Candidate(r["symbol"], r["instrument_id"], r["last_price"], score, r["state"])
        )
    return out


async def rebalance_preview(
    db: AsyncSession,
    current: CurrentSession,
    settings: Settings,
    *,
    tf: str,
    max_weight_pct: Decimal,
    cash_buffer_pct: Decimal,
    max_new: int,
    min_trade_value: Decimal,
) -> dict[str, Any]:
    accounts = await _dhan_accounts(db, current)
    ids = [a.id for a in accounts]
    the_map = await build(db, current, settings, tf)
    holdings = await _latest_holdings(db, ids)
    positions = []
    for h, row in zip(holdings, the_map["holdings"], strict=True):
        positions.append(
            pf.Position(
                symbol=h.symbol,
                quantity=h.quantity,
                avg_cost=h.avg_cost,
                price=Decimal(row["last_price"]) if row["last_price"] else None,
                instrument_id=h.instrument_id,
                state=row["signal_state"],
                score=Decimal(row["trend_score"]) if row["trend_score"] else None,
            )
        )
    add_min = Decimal(str(settings.PORTFOLIO_ADD_MIN_SCORE))
    valued, _ = pf.value(positions, max_weight_pct=max_weight_pct, add_min_score=add_min)
    cash, _ = await _cash(db, ids)
    alts = await candidates(db, settings, tf, {h.instrument_id for h in holdings})
    schedule = {**acc.DEFAULT_DHAN_CHARGES, **settings.PAPER_DHAN_CHARGES}

    def charges(side: str, turnover: Decimal) -> Decimal:
        return acc.dhan_charges("equity", "CNC", side, turnover, schedule).total

    plan = pf.rebalance(
        valued,
        alts,
        cash=cash or Decimal(0),
        max_weight_pct=max_weight_pct,
        cash_buffer_pct=cash_buffer_pct,
        max_new=max_new,
        min_trade_value=min_trade_value,
        charges=charges,
    )
    out = pf.plan_json(plan)
    out |= {
        "timeframe": tf,
        "inputs": {
            "max_weight_pct": _s(max_weight_pct),
            "cash_buffer_pct": _s(cash_buffer_pct),
            "max_new": max_new,
            "min_trade_value": _s(min_trade_value),
        },
        "cash_known": cash is not None,
        "charges_model": "Dhan equity delivery (CNC) schedule, PAPER_DHAN_CHARGES overrides",
        "freshness": the_map["freshness"],
        "generated_at": datetime.now(UTC),
    }
    return out


# --- notifications for holders ----------------------------------------------------------------


async def notify_holdings(
    maker: async_sessionmaker[AsyncSession], result: RunResult, run_id: uuid.UUID | None
) -> int:
    """holding_reversal (a confirmed crossover against the holding's trend) and
    portfolio_trend_change (bullish ↔ bearish without one), for holders only. Also refreshes
    the in_holdings flags. Idempotent per (kind, instrument, timeframe, run)."""
    async with maker() as db, db.begin():
        hold = await holders(db)
        await mark_in_holdings(db, hold)
        flips = [
            (iid, tf, prev, new)
            for iid, tf, prev, new in result.changes
            if iid in hold and {_group(prev), _group(new)} == {"bull", "bear"}
        ]
        if not flips:
            return 0
        insts = {
            i.id: i
            for i in (
                await db.scalars(select(Instrument).where(Instrument.id.in_({f[0] for f in flips})))
            ).all()
        }
        states = {
            (s.instrument_id, s.timeframe): s
            for s in (
                await db.scalars(
                    select(StockSignalState).where(
                        StockSignalState.instrument_id.in_({f[0] for f in flips})
                    )
                )
            ).all()
        }
        rows = []
        for iid, tf, prev, new in flips:
            inst, st = insts[iid], states.get((iid, tf))
            sym = inst.trading_symbol or inst.symbol
            kind = "holding_reversal" if new.endswith("_CONFIRMED") else "portfolio_trend_change"
            word = "bearish" if _group(new) == "bear" else "bullish"
            title = (
                f"{sym} (held): reversal to {word} ({tf})"
                if kind == "holding_reversal"
                else f"{sym} (held): trend turned {word} ({tf})"
            )
            body = (
                f"{sym} moved from {prev} to {new} on the {tf} candle that closed "
                f"{st.candle_close_at:%d %b %H:%M} UTC (close {_s(st.last_price)}, EMA 8 "
                f"{_s(st.ema_fast)}, EMA 32 {_s(st.ema_slow)}, VWAP {_s(st.vwap) or 'n/a'}). "
                "Advisory only."
                if st is not None and st.candle_close_at is not None
                else f"{sym} moved from {prev} to {new} ({tf}). Advisory only."
            )
            payload = {
                "kind": kind,
                "signal_id": st.event_id if st else None,
                "instrument": {
                    "id": inst.id,
                    "symbol": sym,
                    "name": inst.display_name or inst.name,
                    "exchange": inst.exchange,
                    "segment": inst.segment,
                },
                "timeframe": tf,
                "previous_state": prev,
                "state": new,
                "prices": {
                    "last_price": _s(st.last_price) if st else None,
                    "ema_fast": _s(st.ema_fast) if st else None,
                    "ema_slow": _s(st.ema_slow) if st else None,
                    "vwap": _s(st.vwap) if st else None,
                },
                "data_as_of": st.candle_close_at.isoformat() if st and st.candle_close_at else None,
                "deep_link": deep_link(st.event_id if st else None, iid),
            }
            for uid in hold[iid]:
                rows.append(
                    {
                        "user_id": uid,
                        "kind": kind,
                        "event_id": st.event_id if st else None,
                        "instrument_id": iid,
                        "timeframe": tf,
                        "title": title[:200],
                        "body": body,
                        "payload": payload,
                        "dedup_key": f"{kind}:{iid}:{tf}:{run_id}"[:160],
                    }
                )
        return await insert_many(db, rows)


async def notify_candidates(
    maker: async_sessionmaker[AsyncSession], settings: Settings, tf: str, week: str
) -> int:
    """The weekly review: alternatives passing the filters, once per user per week."""
    async with maker() as db, db.begin():
        accounts = await _dhan_accounts(db, None)
        owners: dict[uuid.UUID, list[uuid.UUID]] = {}
        for a in accounts:
            owners.setdefault(a.owner_user_id, []).append(a.id)
        rows = []
        for uid, acct_ids in owners.items():
            held = {h.instrument_id for h in await _latest_holdings(db, acct_ids)}
            if not held:
                continue
            for c in (await candidates(db, settings, tf, held))[: settings.PORTFOLIO_CANDIDATES]:
                rows.append(
                    {
                        "user_id": uid,
                        "kind": "replacement_candidate",
                        "event_id": None,
                        "instrument_id": c.instrument_id,
                        "timeframe": tf,
                        "title": f"Candidate to review: {c.symbol} ({c.state}, score {c.score})",
                        "body": (
                            f"{c.symbol} passes the liquidity and trend filters ({c.state}, "
                            f"trend score {c.score}, last {c.price}). A candidate to compare "
                            "with your holdings in the rebalance preview, not a recommendation "
                            "to switch."
                        ),
                        "payload": {
                            "kind": "replacement_candidate",
                            "instrument": {"id": c.instrument_id, "symbol": c.symbol},
                            "timeframe": tf,
                            "state": c.state,
                            "trend_score": _s(c.score),
                            "prices": {"last_price": _s(c.price)},
                            "week": week,
                            "deep_link": deep_link(None, c.instrument_id),
                        },
                        "dedup_key": f"replacement_candidate:{c.instrument_id}:{tf}:{week}",
                    }
                )
        return await insert_many(db, rows)


async def premarket_review(
    maker: async_sessionmaker[AsyncSession], settings: Settings, tf: str
) -> dict[str, Any]:
    """08:45 IST: the previous session's signal states for every held instrument."""
    async with maker() as db:
        hold = await holders(db)
        rows = (
            await db.execute(
                select(StockSignalState.state, func.count())
                .where(
                    StockSignalState.instrument_id.in_(list(hold)),
                    StockSignalState.timeframe == tf,
                )
                .group_by(StockSignalState.state)
            )
        ).all()
    return {"held_instruments": len(hold), "states": {s: int(n) for s, n in rows}}
