"""Trading Phase F: PAPER mode end to end. No broker order call exists anywhere in here.

    order intent ─▶ risk decision ─▶ admin approval ─▶ Execution Agent ─▶ simulated fill
    (PAPER only)    (re-run at        (POST …/approve)   (approved PAPER     executions,
                     approval)                            intents only)      positions_snapshots,
                                                                             paper_ledger,
                                                                             order_events

- **Idempotent:** an intent's `correlation_id` is unique. Re-sending the same request returns
  the existing intent; a different request with the same id is refused.
- **Lifecycle:** every step is an `order_events` row (CREATED, RISK_PASSED / RISK_REJECTED,
  APPROVED, SUBMITTED, FILLED, CANCELLED, EXPIRED), with the correlation id.
- **LIVE is unreachable:** an intent is only accepted on a PAPER account, the Execution Agent
  refuses anything that is not PAPER on both the intent and the account, and the
  BrokerAdapter interface has no order methods at all.
- **AI cannot create or approve:** intents come only from the authenticated API (source
  `manual`, or `strategy` when they reference a signal); approval is admin-only. The AI
  research agent has no code path here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.trading.base import BrokerAdapter, BrokerError
from app.agents.trading.mt5.adapter import MT5Adapter
from app.agents.trading.paper import accounting as acc
from app.agents.trading.types import session_for
from app.core.config import Settings
from app.models.brokers import Mt5Symbol
from app.models.trading import (
    BrokerAccount,
    Execution,
    Instrument,
    MarketCandle,
    OrderEvent,
    OrderIntent,
    PaperLedger,
    PositionSnapshot,
    Reconciliation,
    RiskDecision,
    RiskPolicy,
    TradingSignal,
)
from app.services.research import TF_SECONDS, Checks, _num, _policy_snapshot, tf_delta

IST = ZoneInfo("Asia/Kolkata")
OPEN_STATUSES = ("PENDING_RISK", "APPROVED", "SUBMITTED")


class IntentRefused(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class LiveUnreachable(IntentRefused):
    def __init__(self) -> None:
        super().__init__(
            "live_unreachable",
            "LIVE execution does not exist in this build (Phase G); nothing was sent",
        )


@dataclass(frozen=True, slots=True)
class PaperConfig:
    capital: Decimal
    price_max_age: float
    costs: dict[str, dict[str, Decimal]]
    dhan_charges: dict[str, Any]
    mt5_commission: Decimal
    max_cost_bps: Decimal

    @classmethod
    def from_settings(cls, s: Settings) -> PaperConfig:
        costs = {b: {k: Decimal(str(v)) for k, v in c.items()} for b, c in s.PAPER_COSTS.items()}
        return cls(
            capital=s.PAPER_STARTING_CAPITAL,
            price_max_age=s.PAPER_PRICE_MAX_AGE_SECONDS,
            costs=costs,
            dhan_charges={**acc.DEFAULT_DHAN_CHARGES, **s.PAPER_DHAN_CHARGES},
            mt5_commission=s.PAPER_MT5_COMMISSION_PER_LOT,
            max_cost_bps=s.RISK_MAX_COST_BPS,
        )

    def spread(self, broker: str) -> tuple[Decimal, Decimal]:
        c = self.costs.get(broker, {})
        return c.get("spread_bps", Decimal(0)), c.get("slippage_bps", Decimal(0))


@dataclass(frozen=True, slots=True)
class IntentRequest:
    account_id: uuid.UUID
    instrument_id: int
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None
    product: str | None
    signal_id: uuid.UUID | None
    correlation_id: str | None


def _now() -> datetime:
    return datetime.now(UTC)


def _event(db: AsyncSession, intent: OrderIntent, kind: str, **payload: Any) -> None:
    db.add(
        OrderEvent(
            order_intent_id=intent.id,
            event_type=kind,
            correlation_id=intent.correlation_id,
            payload={k: (_num(v) if isinstance(v, Decimal) else v) for k, v in payload.items()},
            occurred_at=_now(),
        )
    )


# --- instrument spec + reference price -------------------------------------------------------


async def instrument_spec(db: AsyncSession, inst: Instrument) -> acc.Spec:
    if inst.broker == "dhan":
        return acc.Spec(
            "dhan",
            acc.dhan_kind(inst.segment, inst.instrument_type),
            inst.lot_size,
            Decimal(1),
            inst.currency,
            inst.tick_size,
        )
    sym = await db.scalar(
        select(Mt5Symbol).where(Mt5Symbol.trading_instrument_id == inst.id).limit(1)
    )
    if sym is None or sym.contract_size is None:
        raise acc.AccountingError(
            f"{inst.symbol}: contract size unknown; register it with PUT "
            "/api/trading/mt5/instruments/{symbol} (contract_size) first"
        )
    return acc.Spec(
        "mt5",
        "cfd",
        sym.volume_step or inst.lot_size,
        sym.contract_size,
        (sym.currency_profit or inst.currency)[:3],
        sym.point or inst.tick_size,
        sym.digits,
    )


@dataclass(frozen=True, slots=True)
class RefPrice:
    price: Decimal
    at: datetime
    source: str


async def reference_price(
    db: AsyncSession,
    redis: Redis | None,
    adapters: dict[str, BrokerAdapter],
    inst: Instrument,
    max_age: float,
    now: datetime | None = None,
) -> RefPrice | None:
    """The freshest price we have, never older than max_age. None means: do not fill."""
    now = now or _now()
    limit = timedelta(seconds=max_age)
    if inst.broker == "dhan" and redis is not None:
        try:
            h = await redis.hgetall(f"dhan:tick:{inst.broker_instrument_id}")
        except Exception:
            h = {}
        data = {
            (k.decode() if isinstance(k, bytes) else str(k)): (
                v.decode() if isinstance(v, bytes) else str(v)
            )
            for k, v in (h or {}).items()
        }
        if data.get("ltp") and data.get("received_at") and data.get("stale") != "1":
            at = datetime.fromisoformat(data["received_at"])
            if now - at <= limit:
                return RefPrice(Decimal(data["ltp"]), at, "dhan feed (last traded price)")
    if inst.broker == "mt5":
        adapter = adapters.get("mt5")
        if isinstance(adapter, MT5Adapter):
            try:
                st = await adapter.status()
            except BrokerError:
                st = None
            agent = st.agents.get(inst.broker_instrument_id) if st else None
            close = agent.health.get("close") if agent else None
            if (
                agent
                and close is not None
                and agent.snapshot_at
                and now - agent.snapshot_at <= limit
            ):
                return RefPrice(Decimal(str(close)), agent.snapshot_at, "MT5 bot (last close)")
    # Any timeframe's newest closed real candle, if it closed recently enough.
    rows = (
        await db.execute(
            select(MarketCandle.timeframe, MarketCandle.ts, MarketCandle.close)
            .where(MarketCandle.instrument_id == inst.id, MarketCandle.is_gap.is_(False))
            .order_by(MarketCandle.ts.desc())
            .limit(20)
        )
    ).all()
    best: RefPrice | None = None
    for tf, ts, close in rows:
        if tf not in {t.value for t in TF_SECONDS}:
            continue
        closed = ts + tf_delta(tf)
        if closed <= now and now - closed <= limit and (best is None or closed > best.at):
            best = RefPrice(close, closed, f"{tf} candle close")
    return best


# --- the paper book ------------------------------------------------------------------------------


async def _fills(db: AsyncSession, account_id: uuid.UUID) -> list[tuple[Execution, Decimal]]:
    rows = (
        await db.scalars(
            select(Execution)
            .where(Execution.broker_account_id == account_id, Execution.is_simulated.is_(True))
            .order_by(Execution.executed_at, Execution.id)
        )
    ).all()
    out = []
    for e in rows:
        inst = await db.get(Instrument, e.instrument_id)
        assert inst is not None
        out.append((e, (await instrument_spec(db, inst)).multiplier))
    return out


async def book_for(db: AsyncSession, account_id: uuid.UUID) -> acc.Book:
    return acc.replay(
        [
            acc.Fill(e.instrument_id, e.side, e.quantity, e.price, e.fees, mult, e.executed_at)
            for e, mult in await _fills(db, account_id)
        ]
    )


async def ledger_totals(
    db: AsyncSession, account_id: uuid.UUID, since: datetime | None = None
) -> dict[str, Decimal]:
    q = select(PaperLedger.kind, func.coalesce(func.sum(PaperLedger.amount), 0)).where(
        PaperLedger.broker_account_id == account_id
    )
    if since is not None:
        q = q.where(PaperLedger.created_at >= since)
    rows: dict[str, Decimal] = {
        k: v for k, v in (await db.execute(q.group_by(PaperLedger.kind))).all()
    }
    return {"realized": Decimal(rows.get("realized", 0)), "fees": Decimal(rows.get("fees", 0))}


async def equity_curve(db: AsyncSession, account_id: uuid.UUID, capital: Decimal) -> list[Decimal]:
    amounts = (
        await db.scalars(
            select(PaperLedger.amount)
            .where(PaperLedger.broker_account_id == account_id)
            .order_by(PaperLedger.created_at, PaperLedger.id)
        )
    ).all()
    curve, eq = [capital], capital
    for a in amounts:
        eq += a
        curve.append(eq)
    return curve


def drawdown_pct(curve: list[Decimal]) -> Decimal:
    peak, worst = curve[0], Decimal(0)
    for e in curve:
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, (peak - e) / peak * 100)
    return worst.quantize(Decimal("0.001"))


def market_day(broker: str, at: datetime) -> date:
    return at.astimezone(IST).date() if broker == "dhan" else at.astimezone(UTC).date()


# --- risk for an intent ---------------------------------------------------------------------------


async def assess_intent(
    db: AsyncSession,
    redis: Redis | None,
    adapters: dict[str, BrokerAdapter],
    settings: Settings,
    cfg: PaperConfig,
    intent: OrderIntent,
    now: datetime | None = None,
) -> RiskDecision:
    now = now or _now()
    account = await db.get(BrokerAccount, intent.broker_account_id)
    inst = await db.get(Instrument, intent.instrument_id)
    assert account and inst
    c = Checks()
    policy = await db.scalar(select(RiskPolicy).where(RiskPolicy.broker_account_id == account.id))
    c.add("policy_present", policy is not None, "risk policy set" if policy else "none")
    c.add(
        "mode_paper", account.mode == "PAPER" and intent.mode == "PAPER", f"account {account.mode}"
    )
    inputs: dict[str, Any] = {
        "intent": {
            "id": str(intent.id),
            "side": intent.side,
            "order_type": intent.order_type,
            "quantity": _num(intent.quantity),
            "limit_price": _num(intent.limit_price) if intent.limit_price else None,
        },
        "instrument": {"id": inst.id, "symbol": inst.symbol, "broker": inst.broker},
        "evaluated_at": now.isoformat(),
    }
    try:
        spec = await instrument_spec(db, inst)
        acc.check_quantity(spec, intent.quantity)
        c.add("quantity_valid", True, f"multiple of {_num(spec.lot_step)}")
    except acc.AccountingError as exc:
        spec = None
        c.add("quantity_valid", False, str(exc))
    if spec and spec.currency != account.base_currency:
        c.add(
            "currency",
            False,
            f"{inst.symbol} settles in {spec.currency}, account is {account.base_currency}: "
            "conversion is not modelled",
        )
    ref = await reference_price(db, redis, adapters, inst, cfg.price_max_age, now)
    c.add("price_fresh", ref is not None, ref.source if ref else "no price within the age limit")
    book = await book_for(db, account.id)
    if policy is not None:
        c.add("kill_switch_off", not policy.kill_switch, f"kill_switch={policy.kill_switch}")
        sess = session_for(now).value
        c.add("session_allowed", sess in policy.allowed_sessions, f"now={sess}")
        current = book.positions.get(inst.id)
        cur_q = current.quantity if current else Decimal(0)
        signed = intent.quantity if intent.side == "BUY" else -intent.quantity
        after = abs(cur_q + signed)
        c.add(
            "position_size",
            after <= policy.max_position_size,
            f"position after fill {_num(after)} (max {_num(policy.max_position_size)})",
        )
        if spec and ref:
            gross = sum(
                (p.notional(ref.price if p.instrument_id == inst.id else None)
                 for p in book.positions.values()),
                Decimal(0),
            )  # fmt: skip
            new = intent.quantity * ref.price * spec.multiplier
            c.add(
                "exposure",
                gross + new <= policy.max_exposure,
                f"gross {_num(acc.money(gross))} + new {_num(acc.money(new))} "
                f"(max {_num(policy.max_exposure)})",
            )
            spread, slip = cfg.spread(inst.broker)
            charges = _charges(cfg, spec, intent, ref.price)
            cost_bps = (
                (charges.total + new * (spread / 2 + slip) / acc.BPS) / new * acc.BPS
                if new
                else Decimal(0)
            ).quantize(Decimal("0.01"))
            inputs["estimate"] = {
                "reference_price": _num(ref.price),
                "price_source": ref.source,
                "notional": _num(acc.money(new)),
                "charges": charges.as_dict(),
                "cost_bps": _num(cost_bps),
            }
            c.add(
                "costs",
                cost_bps <= cfg.max_cost_bps,
                f"{_num(cost_bps)} bps of notional (max {_num(cfg.max_cost_bps)})",
            )
        start = datetime.combine(
            market_day(inst.broker, now), datetime.min.time(), IST if inst.broker == "dhan" else UTC
        )
        today = await ledger_totals(db, account.id, since=start)
        day_pnl = today["realized"] + today["fees"]
        c.add(
            "daily_loss",
            day_pnl >= -policy.max_daily_loss,
            f"today {_num(day_pnl)} (limit -{_num(policy.max_daily_loss)})",
        )
        dd = drawdown_pct(await equity_curve(db, account.id, cfg.capital))
        c.add(
            "drawdown",
            dd <= policy.max_drawdown_pct,
            f"{_num(dd)}% from peak (max {_num(policy.max_drawdown_pct)}%)",
        )
    approved = not c.failed
    decision = RiskDecision(
        broker_account_id=account.id,
        order_intent_id=intent.id,
        signal_id=intent.signal_id,
        policy_id=policy.id if policy else None,
        approved=approved,
        reason="all checks passed" if approved else "rejected: " + ", ".join(c.failed),
        policy_snapshot=_policy_snapshot(policy) if policy else {},
        inputs=inputs,
        checks=c.items,
    )
    db.add(decision)
    await db.flush()
    return decision


def _charges(cfg: PaperConfig, spec: acc.Spec, intent: OrderIntent, price: Decimal) -> acc.Charges:
    turnover = price * intent.quantity * spec.multiplier
    if spec.broker == "dhan":
        return acc.dhan_charges(spec.kind, intent.product, intent.side, turnover, cfg.dhan_charges)
    return acc.mt5_charges(intent.quantity, cfg.mt5_commission)


# --- intents --------------------------------------------------------------------------------------


def _same(intent: OrderIntent, req: IntentRequest) -> bool:
    return (
        intent.broker_account_id == req.account_id
        and intent.instrument_id == req.instrument_id
        and intent.side == req.side
        and intent.order_type == req.order_type
        and intent.quantity == req.quantity
        and intent.limit_price == req.limit_price
    )


async def create_intent(
    db: AsyncSession,
    redis: Redis | None,
    adapters: dict[str, BrokerAdapter],
    settings: Settings,
    account: BrokerAccount,
    req: IntentRequest,
    user_id: uuid.UUID,
) -> tuple[OrderIntent, bool]:
    """(intent, created). Runs the risk check immediately; a failed check rejects it."""
    if req.correlation_id:
        existing = await db.scalar(
            select(OrderIntent).where(OrderIntent.correlation_id == req.correlation_id)
        )
        if existing is not None:
            if not _same(existing, req):
                raise IntentRefused(
                    "correlation_conflict", "this correlation_id belongs to a different intent"
                )
            return existing, False
    if account.mode == "LIVE":
        raise LiveUnreachable()
    if account.mode != "PAPER":
        raise IntentRefused(
            "account_not_paper", f"account is {account.mode}; switch it to PAPER to paper-trade"
        )
    inst = await db.get(Instrument, req.instrument_id)
    if inst is None or not inst.is_active:
        raise IntentRefused("instrument_not_found", "instrument not found", 404)
    if inst.broker != account.broker:
        raise IntentRefused(
            "broker_mismatch", f"{inst.symbol} is not a {account.broker} instrument"
        )
    if req.order_type == "LIMIT" and (req.limit_price is None or req.limit_price <= 0):
        raise IntentRefused(
            "limit_price_required", "a LIMIT intent needs a positive limit_price", 422
        )
    if req.order_type == "MARKET" and req.limit_price is not None:
        raise IntentRefused("limit_price_unexpected", "a MARKET intent has no limit_price", 422)
    try:
        acc.check_quantity(await instrument_spec(db, inst), req.quantity)
    except acc.AccountingError as exc:
        raise IntentRefused("bad_quantity", str(exc), 422) from exc
    if req.signal_id and await db.get(TradingSignal, req.signal_id) is None:
        raise IntentRefused("signal_not_found", "signal not found", 404)
    intent = OrderIntent(
        broker_account_id=account.id,
        instrument_id=inst.id,
        signal_id=req.signal_id,
        side=req.side,
        order_type=req.order_type,
        quantity=req.quantity,
        limit_price=req.limit_price,
        product=req.product,
        mode="PAPER",
        status="PENDING_RISK",
        source="strategy" if req.signal_id else "manual",
        created_by=user_id,
        correlation_id=req.correlation_id or f"aureon-{uuid.uuid4().hex}",
    )
    db.add(intent)
    await db.flush()
    _event(db, intent, "CREATED", by=str(user_id), source=intent.source)
    decision = await assess_intent(
        db, redis, adapters, settings, PaperConfig.from_settings(settings), intent
    )
    if decision.approved:
        _event(db, intent, "RISK_PASSED", risk_decision_id=decision.id)
    else:
        intent.status = "REJECTED"
        intent.updated_at = _now()
        _event(db, intent, "RISK_REJECTED", risk_decision_id=decision.id, reason=decision.reason)
    return intent, True


async def approve_and_execute(
    db: AsyncSession,
    redis: Redis | None,
    adapters: dict[str, BrokerAdapter],
    settings: Settings,
    intent: OrderIntent,
    admin_id: uuid.UUID,
) -> OrderIntent:
    """Admin approval: risk is re-checked now (prices and limits may have moved), then the
    Execution Agent fills it."""
    account = await db.get(BrokerAccount, intent.broker_account_id)
    if intent.mode == "LIVE" or account is None or account.mode == "LIVE":
        raise LiveUnreachable()
    if intent.status != "PENDING_RISK":
        raise IntentRefused("not_pending", f"intent is {intent.status}")
    decision = await assess_intent(
        db, redis, adapters, settings, PaperConfig.from_settings(settings), intent
    )
    if not decision.approved:
        intent.status = "REJECTED"
        intent.updated_at = _now()
        _event(db, intent, "RISK_REJECTED", risk_decision_id=decision.id, reason=decision.reason)
        return intent
    intent.status = "APPROVED"
    intent.updated_at = _now()
    _event(db, intent, "APPROVED", by=str(admin_id), risk_decision_id=decision.id)
    await execute(db, redis, adapters, settings, intent)
    return intent


async def execute(
    db: AsyncSession,
    redis: Redis | None,
    adapters: dict[str, BrokerAdapter],
    settings: Settings,
    intent: OrderIntent,
    now: datetime | None = None,
) -> Execution | None:
    """The Execution Agent. Accepts only an APPROVED (or resting SUBMITTED) PAPER intent on a
    PAPER account whose latest risk decision approved it. Never calls a broker."""
    now = now or _now()
    account = await db.get(BrokerAccount, intent.broker_account_id)
    assert account is not None
    if intent.mode == "LIVE" or account.mode == "LIVE":
        raise LiveUnreachable()
    if intent.mode != "PAPER" or account.mode != "PAPER":
        raise IntentRefused("not_paper", "only PAPER intents on PAPER accounts are executed")
    if intent.status not in ("APPROVED", "SUBMITTED"):
        raise IntentRefused("not_approved", f"intent is {intent.status}, not approved")
    latest = await db.scalar(
        select(RiskDecision)
        .where(RiskDecision.order_intent_id == intent.id)
        .order_by(RiskDecision.id.desc())
        .limit(1)
    )
    if latest is None or not latest.approved:
        raise IntentRefused("risk_not_approved", "no approving risk decision for this intent")
    cfg = PaperConfig.from_settings(settings)
    inst = await db.get(Instrument, intent.instrument_id)
    assert inst is not None
    spec = await instrument_spec(db, inst)
    ref = await reference_price(db, redis, adapters, inst, cfg.price_max_age, now)
    if ref is None:
        if intent.status == "APPROVED":
            intent.status = "SUBMITTED"
            _event(db, intent, "SUBMITTED", note="waiting for a fresh price")
        return None
    spread, slip = cfg.spread(inst.broker)
    price = acc.fill_price(ref.price, intent.side, spread, slip, spec.tick_size)
    if intent.order_type == "LIMIT":
        assert intent.limit_price is not None
        marketable = (
            price <= intent.limit_price if intent.side == "BUY" else price >= intent.limit_price
        )
        if not marketable:
            if intent.status == "APPROVED":
                intent.status = "SUBMITTED"
                intent.updated_at = now
                _event(db, intent, "SUBMITTED", limit_price=intent.limit_price, market=price)
            return None
    if intent.status == "APPROVED":
        _event(db, intent, "SUBMITTED", simulated=True)
    charges = _charges(cfg, spec, intent, price)
    execution = Execution(
        broker_account_id=account.id,
        order_intent_id=intent.id,
        instrument_id=inst.id,
        broker_trade_id=f"paper-{intent.id.hex}",
        side=intent.side,
        quantity=intent.quantity,
        price=price,
        fees=charges.total,
        currency=account.base_currency,
        executed_at=now,
        session=session_for(now).value,
        is_simulated=True,
    )
    db.add(execution)
    await db.flush()
    book = await book_for(db, account.id)  # includes the new execution (flushed)
    pos = book.positions.get(inst.id)
    realized = await _realized_of_last(db, account.id, inst.id)
    db.add(
        PaperLedger(
            broker_account_id=account.id,
            execution_id=execution.id,
            order_intent_id=intent.id,
            instrument_id=inst.id,
            kind="fees",
            amount=-charges.total,
            currency=account.base_currency,
            details=charges.as_dict(),
        )
    )
    if realized != 0:
        db.add(
            PaperLedger(
                broker_account_id=account.id,
                execution_id=execution.id,
                order_intent_id=intent.id,
                instrument_id=inst.id,
                kind="realized",
                amount=realized,
                currency=account.base_currency,
            )
        )
    await _snapshot(db, account, book, {inst.id: ref.price}, now)
    intent.status = "FILLED"
    intent.updated_at = now
    _event(
        db,
        intent,
        "FILLED",
        execution_id=str(execution.id),
        price=price,
        reference_price=ref.price,
        price_source=ref.source,
        quantity=intent.quantity,
        charges=charges.as_dict(),
        realized=realized,
        position=_num(pos.quantity) if pos else "0",
        pip_value=_num(pv) if (pv := acc.pip_value(spec, intent.quantity)) is not None else None,
    )
    return execution


async def _realized_of_last(db: AsyncSession, account_id: uuid.UUID, instrument_id: int) -> Decimal:
    fills = [
        acc.Fill(e.instrument_id, e.side, e.quantity, e.price, e.fees, m)
        for e, m in await _fills(db, account_id)
        if e.instrument_id == instrument_id
    ]
    book = acc.replay(fills[:-1])
    return book.apply(fills[-1]) if fills else Decimal(0)


async def _snapshot(
    db: AsyncSession,
    account: BrokerAccount,
    book: acc.Book,
    prices: dict[int, Decimal],
    now: datetime,
) -> None:
    """All paper positions as of `now` (source=simulated). Closed positions stay at quantity 0,
    so a flat book is visible too. Instruments without a new price keep their last one."""
    previous = await db.scalar(
        select(func.max(PositionSnapshot.as_of)).where(
            PositionSnapshot.broker_account_id == account.id,
            PositionSnapshot.source == "simulated",
        )
    )
    if previous is not None:
        for iid, lp in (
            await db.execute(
                select(PositionSnapshot.instrument_id, PositionSnapshot.last_price).where(
                    PositionSnapshot.broker_account_id == account.id,
                    PositionSnapshot.source == "simulated",
                    PositionSnapshot.as_of == previous,
                )
            )
        ).all():
            if iid is not None and lp is not None:
                prices.setdefault(iid, lp)
    for p in book.positions.values():
        inst = await db.get(Instrument, p.instrument_id)
        assert inst is not None
        last = prices.get(p.instrument_id)
        db.add(
            PositionSnapshot(
                broker_account_id=account.id,
                instrument_id=inst.id,
                symbol=inst.symbol,
                quantity=p.quantity,
                avg_price=p.avg_price,
                last_price=last,
                unrealized_pnl=p.unrealized(last),
                realized_pnl=p.realized,
                currency=account.base_currency,
                source="simulated",
                as_of=now,
            )
        )


async def cancel(db: AsyncSession, intent: OrderIntent, user_id: uuid.UUID) -> OrderIntent:
    if intent.status not in OPEN_STATUSES:
        raise IntentRefused("not_open", f"intent is {intent.status}")
    intent.status = "CANCELLED"
    intent.updated_at = _now()
    _event(db, intent, "CANCELLED", by=str(user_id))
    return intent


async def process_open(
    db: AsyncSession,
    redis: Redis | None,
    adapters: dict[str, BrokerAdapter],
    settings: Settings,
    now: datetime | None = None,
) -> dict[str, int]:
    """Scheduler: retry resting (SUBMITTED) intents and expire DAY intents from a past market
    day. Never fills anything that is not already approved."""
    now = now or _now()
    out = {"filled": 0, "expired": 0, "waiting": 0}
    rows = (
        await db.scalars(
            select(OrderIntent).where(
                OrderIntent.status.in_(OPEN_STATUSES), OrderIntent.mode == "PAPER"
            )
        )
    ).all()
    for intent in rows:
        inst = await db.get(Instrument, intent.instrument_id)
        assert inst is not None
        if intent.validity == "DAY" and market_day(inst.broker, intent.created_at) < market_day(
            inst.broker, now
        ):
            intent.status = "EXPIRED"
            intent.updated_at = now
            _event(db, intent, "EXPIRED", reason="DAY validity ended")
            out["expired"] += 1
            continue
        if intent.status == "SUBMITTED":
            try:
                ex = await execute(db, redis, adapters, settings, intent, now)
            except IntentRefused:
                ex = None
            out["filled" if ex else "waiting"] += 1
    return out


# --- reconciliation -------------------------------------------------------------------------


async def reconcile(db: AsyncSession, account: BrokerAccount) -> Reconciliation:
    """Paper positions (latest simulated snapshot) vs the simulated ledger (replayed
    executions), and the money ledger vs replayed P&L and charges. Any difference is drift."""
    book = await book_for(db, account.id)
    last = await db.scalar(
        select(func.max(PositionSnapshot.as_of)).where(
            PositionSnapshot.broker_account_id == account.id,
            PositionSnapshot.source == "simulated",
        )
    )
    snaps: dict[int, PositionSnapshot] = {}
    if last is not None:
        snaps = {
            s.instrument_id: s
            for s in (
                await db.scalars(
                    select(PositionSnapshot).where(
                        PositionSnapshot.broker_account_id == account.id,
                        PositionSnapshot.source == "simulated",
                        PositionSnapshot.as_of == last,
                    )
                )
            ).all()
            if s.instrument_id is not None
        }
    drift: list[dict[str, Any]] = []
    for iid in set(book.positions) | set(snaps):
        p = book.positions.get(iid)
        s = snaps.get(iid)
        want_q = p.quantity if p else Decimal(0)
        have_q = s.quantity if s else Decimal(0)
        if want_q != have_q:
            drift.append(
                {
                    "instrument_id": iid,
                    "field": "quantity",
                    "ledger": _num(want_q),
                    "snapshot": _num(have_q),
                }
            )
        if p and s and p.quantity != 0 and p.avg_price != s.avg_price:
            drift.append(
                {
                    "instrument_id": iid,
                    "field": "avg_price",
                    "ledger": _num(p.avg_price),
                    "snapshot": _num(s.avg_price),
                }
            )
    totals = await ledger_totals(db, account.id)
    if totals["realized"] != book.realized:
        drift.append(
            {"field": "realized", "ledger": _num(book.realized), "booked": _num(totals["realized"])}
        )
    if -totals["fees"] != book.fees:
        drift.append({"field": "fees", "ledger": _num(book.fees), "booked": _num(-totals["fees"])})
    row = Reconciliation(
        broker_account_id=account.id,
        ok=not drift,
        drift=drift,
        summary={
            "positions": len([p for p in book.positions.values() if p.quantity != 0]),
            "realized": _num(book.realized),
            "fees": _num(book.fees),
            "snapshot_as_of": last.isoformat() if last else None,
        },
    )
    db.add(row)
    await db.flush()
    return row


async def summary(db: AsyncSession, settings: Settings, account: BrokerAccount) -> dict[str, Any]:
    cfg = PaperConfig.from_settings(settings)
    totals = await ledger_totals(db, account.id)
    curve = await equity_curve(db, account.id, cfg.capital)
    last_snap = await db.scalar(
        select(func.max(PositionSnapshot.as_of)).where(
            PositionSnapshot.broker_account_id == account.id, PositionSnapshot.source == "simulated"
        )
    )
    unrealized = Decimal(0)
    positions = []
    if last_snap is not None:
        for s in (
            await db.scalars(
                select(PositionSnapshot).where(
                    PositionSnapshot.broker_account_id == account.id,
                    PositionSnapshot.source == "simulated",
                    PositionSnapshot.as_of == last_snap,
                    PositionSnapshot.quantity != 0,
                )
            )
        ).all():
            unrealized += s.unrealized_pnl or 0
            positions.append(
                {
                    "instrument_id": s.instrument_id,
                    "symbol": s.symbol,
                    "quantity": _num(s.quantity),
                    "avg_price": _num(s.avg_price),
                    "last_price": _num(s.last_price) if s.last_price is not None else None,
                    "unrealized_pnl": _num(s.unrealized_pnl)
                    if s.unrealized_pnl is not None
                    else None,
                    "realized_pnl": _num(s.realized_pnl) if s.realized_pnl is not None else None,
                }
            )
    rec = await db.scalar(
        select(Reconciliation)
        .where(Reconciliation.broker_account_id == account.id)
        .order_by(Reconciliation.id.desc())
        .limit(1)
    )
    equity = curve[-1] + unrealized
    return {
        "account_id": str(account.id),
        "mode": account.mode,
        "currency": account.base_currency,
        "starting_capital": _num(cfg.capital),
        "realized_pnl": _num(totals["realized"]),
        "fees": _num(-totals["fees"]),
        "unrealized_pnl": _num(acc.money(unrealized)),
        "equity": _num(acc.money(equity)),
        "max_drawdown_pct": _num(drawdown_pct(curve)),
        "positions": positions,
        "positions_as_of": last_snap.isoformat() if last_snap else None,
        "reconciliation": {
            "ok": rec.ok,
            "drift": rec.drift,
            "at": rec.created_at.isoformat(),
        }
        if rec
        else None,
        "source": "simulated",
    }
