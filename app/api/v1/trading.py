"""Trading Phase A endpoints: read-only, broker-agnostic, all authenticated.

- **Stored data** (instruments, candles, strategies, signals, executions) comes from the DB.
  It returns empty pages until later phases fill those tables.
- **Live broker reads** (health, funds, positions, orders, quotes) go through a BrokerAdapter.
  With no adapter configured (Phase A), they return 501 `not_supported` and never invent data.
- **Account changes** (create, mode, risk policy) are admin-only, need CSRF, and are audited.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.agents.registry import effective_state
from app.agents.trading.base import BrokerAdapter
from app.agents.trading.mt5.adapter import MT5Adapter
from app.agents.trading.types import Broker, SignalSide, Timeframe
from app.auth.deps import AdminRequired, Current, DbSession, SettingsDep
from app.models.agents import Agent, AgentLog
from app.models.brokers import Mt5Symbol
from app.models.trading import (
    BrokerAccount,
    Execution,
    Instrument,
    MarketCandle,
    RiskPolicy,
    Strategy,
    StrategyVersion,
    TradingSignal,
)
from app.schemas.agents import LogOut
from app.schemas.common import Page
from app.schemas.trading import (
    AccountCreate,
    AccountOut,
    CandleOut,
    ExecutionOut,
    FundsOut,
    HealthOut,
    HoldingOut,
    InstrumentOut,
    LiveToken,
    ModeChange,
    Mt5InstrumentIn,
    Mt5StatusOut,
    OptionChainOut,
    OrderOut,
    PositionOut,
    QuoteOut,
    RiskPolicyIn,
    RiskPolicyOut,
    SignalOut,
    StrategyOut,
    StrategyVersionOut,
    TradeOut,
    TradingAgentStatus,
)
from app.services import audit, mt5_sync
from app.services.audit import ClientInfo
from app.services.trading import (
    ModeChangeDenied,
    adapter_for,
    call_adapter,
    check_mode_change,
    count,
    get_visible_account,
    issue_live_token,
    risk_policy,
    visible_account_ids,
    visible_accounts,
)

router = APIRouter(prefix="/trading", tags=["trading"])

Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]
TRADING_AGENT_KIND = "trading"


def _adapters(request: Request) -> dict[str, BrokerAdapter]:
    adapters: dict[str, BrokerAdapter] = request.app.state.trading_adapters
    return adapters


def _page[T](items: list[T], limit: int, offset: int) -> Page[T]:
    return Page[T](
        items=items[offset : offset + limit], total=len(items), limit=limit, offset=offset
    )


# --- accounts -----------------------------------------------------------------------------


@router.get("/accounts", response_model=Page[AccountOut])
async def list_accounts(
    db: DbSession, current: Current, limit: Limit = 50, offset: Offset = 0
) -> Page[AccountOut]:
    q = visible_accounts(current)
    rows = (
        await db.scalars(q.order_by(BrokerAccount.created_at).limit(limit).offset(offset))
    ).all()
    return Page[AccountOut](
        items=[AccountOut.model_validate(r) for r in rows],
        total=await count(db, q),
        limit=limit,
        offset=offset,
    )


@router.post("/accounts", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreate, request: Request, db: DbSession, admin: AdminRequired
) -> AccountOut:
    exists = await db.scalar(
        select(BrokerAccount.id).where(
            BrokerAccount.broker == body.broker.value, BrokerAccount.account_id == body.account_id
        )
    )
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "account already registered")
    acct = BrokerAccount(
        broker=body.broker.value,
        account_id=body.account_id,
        label=body.label,
        base_currency=body.base_currency,
        owner_user_id=body.owner_user_id or admin.user.id,
    )
    db.add(acct)
    await db.flush()
    audit.record(
        db,
        "trading_account_created",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        target_user_id=acct.owner_user_id,
        details={"account": str(acct.id), "broker": acct.broker, "mode": "OBSERVE"},
    )
    await db.commit()
    await db.refresh(acct)
    return AccountOut.model_validate(acct)


@router.get("/accounts/{account_id}", response_model=AccountOut)
async def get_account(account_id: uuid.UUID, db: DbSession, current: Current) -> AccountOut:
    return AccountOut.model_validate(await get_visible_account(db, current, account_id))


@router.post("/accounts/{account_id}/mode/live-token", response_model=LiveToken)
async def live_token(
    account_id: uuid.UUID,
    request: Request,
    db: DbSession,
    admin: AdminRequired,
    settings: SettingsDep,
) -> LiveToken:
    acct = await get_visible_account(db, admin, account_id)
    client = ClientInfo.from_request(request)
    try:
        token = await issue_live_token(request.app.state.redis, settings, acct.id, admin.user.id)
    except ModeChangeDenied as exc:
        audit.record(
            db,
            "trading_mode_change_denied",
            client,
            actor_user_id=admin.user.id,
            details={"account": str(acct.id), "to": "LIVE", "code": exc.code},
        )
        await db.commit()
        raise HTTPException(exc.http_status, {"code": exc.code, "detail": exc.message}) from exc
    audit.record(
        db,
        "trading_live_token_issued",
        client,
        actor_user_id=admin.user.id,
        details={"account": str(acct.id)},
    )
    await db.commit()
    return LiveToken(token=token, expires_in=settings.TRADING_LIVE_TOKEN_TTL_SECONDS)


@router.post("/accounts/{account_id}/mode", response_model=AccountOut)
async def change_mode(
    account_id: uuid.UUID,
    body: ModeChange,
    request: Request,
    db: DbSession,
    admin: AdminRequired,
    settings: SettingsDep,
) -> AccountOut:
    acct = await get_visible_account(db, admin, account_id)
    client = ClientInfo.from_request(request)
    old = acct.mode
    try:
        await check_mode_change(
            request.app.state.redis,
            settings,
            acct,
            body.mode,
            admin.user.id,
            body.confirmation_token,
        )
    except ModeChangeDenied as exc:
        audit.record(
            db,
            "trading_mode_change_denied",
            client,
            actor_user_id=admin.user.id,
            details={"account": str(acct.id), "from": old, "to": body.mode.value, "code": exc.code},
        )
        await db.commit()
        detail: dict[str, object] = {"code": exc.code, "detail": exc.message}
        if exc.unmet:
            detail["unmet"] = exc.unmet
        raise HTTPException(exc.http_status, detail) from exc
    if body.mode.value != old:
        acct.mode = body.mode.value
        acct.mode_changed_at = datetime.now(UTC)
        acct.mode_changed_by = admin.user.id
        acct.updated_at = datetime.now(UTC)
        audit.record(
            db,
            "trading_mode_changed",
            client,
            actor_user_id=admin.user.id,
            details={"account": str(acct.id), "from": old, "to": body.mode.value},
        )
        await db.commit()
        await db.refresh(acct)
    return AccountOut.model_validate(acct)


@router.get("/accounts/{account_id}/risk-policy", response_model=RiskPolicyOut)
async def get_risk_policy(account_id: uuid.UUID, db: DbSession, current: Current) -> RiskPolicyOut:
    acct = await get_visible_account(db, current, account_id)
    policy = await risk_policy(db, acct.id)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no risk policy for this account")
    return RiskPolicyOut.model_validate(policy)


@router.put("/accounts/{account_id}/risk-policy", response_model=RiskPolicyOut)
async def put_risk_policy(
    account_id: uuid.UUID, body: RiskPolicyIn, request: Request, db: DbSession, admin: AdminRequired
) -> RiskPolicyOut:
    acct = await get_visible_account(db, admin, account_id)
    policy = await risk_policy(db, acct.id)
    values = body.model_dump()
    values["allowed_sessions"] = [s.value for s in body.allowed_sessions]
    before = RiskPolicyOut.model_validate(policy).model_dump(mode="json") if policy else None
    if policy is None:
        policy = RiskPolicy(broker_account_id=acct.id, **values)
        db.add(policy)
    else:
        for k, v in values.items():
            setattr(policy, k, v)
        policy.updated_at = datetime.now(UTC)
    policy.updated_by = admin.user.id
    await db.flush()
    await db.refresh(policy)
    after = RiskPolicyOut.model_validate(policy).model_dump(mode="json")
    audit.record(
        db,
        "risk_policy_updated",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"account": str(acct.id), "before": before, "after": after},
    )
    await db.commit()
    return RiskPolicyOut.model_validate(policy)


# --- live broker reads --------------------------------------------------------------------


@router.get("/accounts/{account_id}/health", response_model=HealthOut)
async def account_health(
    account_id: uuid.UUID, request: Request, db: DbSession, current: Current, settings: SettingsDep
) -> HealthOut:
    acct = await get_visible_account(db, current, account_id)
    adapter = _adapters(request).get(acct.broker)
    if adapter is None:
        return HealthOut(
            broker=acct.broker,
            supported=False,
            ok=False,
            detail="no adapter configured yet (Trading Phase B: Dhan, C: MT5)",
        )
    h = await call_adapter(adapter.health(), settings)
    return HealthOut(
        broker=h.broker,
        supported=True,
        ok=h.ok,
        checked_at=h.checked_at,
        detail=h.detail,
        credentials_expire_at=h.credentials_expire_at,
    )


@router.get("/accounts/{account_id}/funds", response_model=FundsOut)
async def account_funds(
    account_id: uuid.UUID, request: Request, db: DbSession, current: Current, settings: SettingsDep
) -> FundsOut:
    acct = await get_visible_account(db, current, account_id)
    adapter = adapter_for(_adapters(request), acct.broker, "funds")
    return FundsOut.model_validate(await call_adapter(adapter.funds(acct.account_id), settings))


@router.get("/accounts/{account_id}/positions", response_model=Page[PositionOut])
async def account_positions(
    account_id: uuid.UUID,
    request: Request,
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    limit: Limit = 100,
    offset: Offset = 0,
) -> Page[PositionOut]:
    acct = await get_visible_account(db, current, account_id)
    adapter = adapter_for(_adapters(request), acct.broker, "positions")
    rows = await call_adapter(adapter.positions(acct.account_id), settings)
    return _page([PositionOut.model_validate(p) for p in rows], limit, offset)


@router.get("/accounts/{account_id}/orders", response_model=Page[OrderOut])
async def account_orders(
    account_id: uuid.UUID,
    request: Request,
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    limit: Limit = 100,
    offset: Offset = 0,
) -> Page[OrderOut]:
    acct = await get_visible_account(db, current, account_id)
    adapter = adapter_for(_adapters(request), acct.broker, "orders")
    rows = await call_adapter(adapter.orders(acct.account_id), settings)
    return _page([OrderOut.model_validate(o) for o in rows], limit, offset)


@router.get("/accounts/{account_id}/holdings", response_model=Page[HoldingOut])
async def account_holdings(
    account_id: uuid.UUID,
    request: Request,
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    limit: Limit = 100,
    offset: Offset = 0,
) -> Page[HoldingOut]:
    acct = await get_visible_account(db, current, account_id)
    adapter = adapter_for(_adapters(request), acct.broker, "holdings")
    rows = await call_adapter(adapter.holdings(acct.account_id), settings)
    return _page([HoldingOut.model_validate(h) for h in rows], limit, offset)


@router.get("/accounts/{account_id}/trades", response_model=Page[TradeOut])
async def account_trades(
    account_id: uuid.UUID,
    request: Request,
    db: DbSession,
    current: Current,
    settings: SettingsDep,
    limit: Limit = 100,
    offset: Offset = 0,
) -> Page[TradeOut]:
    acct = await get_visible_account(db, current, account_id)
    adapter = adapter_for(_adapters(request), acct.broker, "trades")
    rows = await call_adapter(adapter.trades(acct.account_id), settings)
    return _page([TradeOut.model_validate(t) for t in rows], limit, offset)


@router.get("/option-chain/expiries", response_model=list[date])
async def option_expiries(
    request: Request,
    _: Current,
    settings: SettingsDep,
    underlying: Annotated[str, Query(max_length=40)],
    broker: Broker = Broker.DHAN,
) -> list[date]:
    adapter = adapter_for(_adapters(request), broker.value, "option_expiries")
    return await call_adapter(adapter.option_expiries(underlying), settings)


@router.get("/option-chain", response_model=OptionChainOut)
async def option_chain(
    request: Request,
    _: Current,
    settings: SettingsDep,
    underlying: Annotated[str, Query(max_length=40, description="e.g. IDX_I:13 (NIFTY)")],
    expiry: date,
    broker: Broker = Broker.DHAN,
) -> OptionChainOut:
    adapter = adapter_for(_adapters(request), broker.value, "option_chain")
    chain = await call_adapter(adapter.option_chain(underlying, expiry), settings)
    return OptionChainOut.model_validate(chain)


@router.get("/quotes", response_model=list[QuoteOut])
async def quotes(
    request: Request,
    _: Current,
    settings: SettingsDep,
    broker: Broker,
    symbols: Annotated[
        str, Query(min_length=1, max_length=2000, description="comma-separated, max 50")
    ],
) -> list[QuoteOut]:
    wanted = list(dict.fromkeys(s.strip() for s in symbols.split(",") if s.strip()))
    if not wanted or len(wanted) > 50:
        raise HTTPException(422, "between 1 and 50 symbols")
    adapter = adapter_for(_adapters(request), broker.value, "quotes")
    rows = await call_adapter(adapter.quotes(wanted), settings)
    return [QuoteOut.model_validate(q) for q in rows if q.symbol in wanted]


# --- stored data --------------------------------------------------------------------------


@router.get("/instruments", response_model=Page[InstrumentOut])
async def instruments(
    db: DbSession,
    _: Current,
    broker: Broker | None = None,
    q: Annotated[str | None, Query(max_length=64)] = None,
    instrument_type: Annotated[str | None, Query(max_length=32)] = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[InstrumentOut]:
    stmt = select(Instrument).where(Instrument.is_active.is_(True))
    if broker:
        stmt = stmt.where(Instrument.broker == broker.value)
    if q:
        stmt = stmt.where(Instrument.symbol.ilike(f"%{q.replace('%', '').replace('_', '')}%"))
    if instrument_type:
        stmt = stmt.where(Instrument.instrument_type == instrument_type)
    rows = (
        await db.scalars(
            stmt.order_by(Instrument.symbol, Instrument.id).limit(limit).offset(offset)
        )
    ).all()
    return Page[InstrumentOut](
        items=[InstrumentOut.model_validate(r) for r in rows],
        total=await count(db, stmt),
        limit=limit,
        offset=offset,
    )


@router.get("/candles", response_model=list[CandleOut])
async def candles(
    db: DbSession,
    _: Current,
    instrument_id: int,
    timeframe: Timeframe,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> list[CandleOut]:
    for name, value in (("start", start), ("end", end)):
        if value is not None and value.tzinfo is None:
            raise HTTPException(422, f"{name} must include a timezone (e.g. Z)")
    stmt = select(MarketCandle).where(
        MarketCandle.instrument_id == instrument_id, MarketCandle.timeframe == timeframe.value
    )
    if start:
        stmt = stmt.where(MarketCandle.ts >= start)
    if end:
        stmt = stmt.where(MarketCandle.ts < end)
    # Latest `limit` candles in the range, returned oldest → newest.
    rows = (await db.scalars(stmt.order_by(MarketCandle.ts.desc()).limit(limit))).all()
    return [CandleOut.model_validate(r) for r in reversed(rows)]


@router.get("/strategies", response_model=list[StrategyOut])
async def strategies(db: DbSession, _: Current) -> list[StrategyOut]:
    strats = (await db.scalars(select(Strategy).order_by(Strategy.name))).all()
    versions = (
        await db.scalars(
            select(StrategyVersion).order_by(
                StrategyVersion.strategy_id, StrategyVersion.version.desc()
            )
        )
    ).all()
    by_strategy: dict[uuid.UUID, list[StrategyVersionOut]] = {}
    for v in versions:
        by_strategy.setdefault(v.strategy_id, []).append(StrategyVersionOut.model_validate(v))
    return [
        StrategyOut.model_validate(s).model_copy(update={"versions": by_strategy.get(s.id, [])})
        for s in strats
    ]


@router.get("/signals", response_model=Page[SignalOut])
async def signals(
    db: DbSession,
    _: Current,
    strategy_version_id: uuid.UUID | None = None,
    instrument_id: int | None = None,
    side: SignalSide | None = None,
    since: datetime | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[SignalOut]:
    stmt = select(TradingSignal)
    if strategy_version_id:
        stmt = stmt.where(TradingSignal.strategy_version_id == strategy_version_id)
    if instrument_id is not None:
        stmt = stmt.where(TradingSignal.instrument_id == instrument_id)
    if side:
        stmt = stmt.where(TradingSignal.side == side.value)
    if since:
        stmt = stmt.where(TradingSignal.candle_time >= since)
    rows = (
        await db.scalars(
            stmt.order_by(TradingSignal.candle_time.desc(), TradingSignal.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page[SignalOut](
        items=[SignalOut.model_validate(r) for r in rows],
        total=await count(db, stmt),
        limit=limit,
        offset=offset,
    )


@router.get("/executions", response_model=Page[ExecutionOut])
async def executions(
    db: DbSession,
    current: Current,
    account_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
) -> Page[ExecutionOut]:
    allowed = await visible_account_ids(db, current)
    if account_id is not None:
        if account_id not in allowed:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
        allowed = [account_id]
    stmt = select(Execution).where(Execution.broker_account_id.in_(allowed))
    if since:
        stmt = stmt.where(Execution.executed_at >= since)
    if until:
        stmt = stmt.where(Execution.executed_at < until)
    rows = (
        await db.scalars(
            stmt.order_by(Execution.executed_at.desc(), Execution.id).limit(limit).offset(offset)
        )
    ).all()
    items = [
        ExecutionOut.model_validate(
            {
                **{c: getattr(r, c) for c in ExecutionOut.model_fields if c != "source"},
                "source": "simulated" if r.is_simulated else "broker",
            }
        )
        for r in rows
    ]
    return Page[ExecutionOut](items=items, total=await count(db, stmt), limit=limit, offset=offset)


# --- trading agents -----------------------------------------------------------------------


def _agent_status(a: Agent, stale_after: float) -> TradingAgentStatus:
    return TradingAgentStatus(
        id=a.id,
        name=a.name,
        state=effective_state(a, stale_after),
        paused=a.paused,
        last_heartbeat_at=a.last_heartbeat_at,
        details=a.details,
    )


async def _trading_agent(db: DbSession, agent_id: str) -> Agent:
    agent = await db.get(Agent, agent_id)
    if agent is None or agent.kind != TRADING_AGENT_KIND:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "trading agent not found")
    return agent


@router.get("/agents", response_model=list[TradingAgentStatus])
async def trading_agents(
    db: DbSession, _: Current, settings: SettingsDep
) -> list[TradingAgentStatus]:
    rows = (
        await db.scalars(select(Agent).where(Agent.kind == TRADING_AGENT_KIND).order_by(Agent.id))
    ).all()
    return [_agent_status(a, settings.AGENT_STALE_SECONDS) for a in rows]


@router.get("/agents/{agent_id}/status", response_model=TradingAgentStatus)
async def trading_agent_status(
    agent_id: str, db: DbSession, _: Current, settings: SettingsDep
) -> TradingAgentStatus:
    return _agent_status(await _trading_agent(db, agent_id), settings.AGENT_STALE_SECONDS)


@router.get("/agents/{agent_id}/logs", response_model=Page[LogOut])
async def trading_agent_logs(
    agent_id: str, db: DbSession, _: AdminRequired, limit: Limit = 100, offset: Offset = 0
) -> Page[LogOut]:
    await _trading_agent(db, agent_id)
    stmt = select(AgentLog).where(AgentLog.agent_id == agent_id)
    rows = (
        await db.scalars(
            stmt.order_by(AgentLog.created_at.desc(), AgentLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return Page[LogOut](
        items=[LogOut.model_validate(r, from_attributes=True) for r in rows],
        total=await count(db, stmt),
        limit=limit,
        offset=offset,
    )


# --- MT5 bot (Trading Phase C) ------------------------------------------------------------


def _mt5(request: Request) -> MT5Adapter:
    adapter = adapter_for(_adapters(request), Broker.MT5.value, "mt5_status")
    assert isinstance(adapter, MT5Adapter)
    return adapter


def _jsonable_dict(d: dict[str, object]) -> dict[str, object]:
    out = mt5_sync._jsonable(d)
    assert isinstance(out, dict)
    return out


@router.get("/mt5/status", response_model=Mt5StatusOut)
async def mt5_status(request: Request, _: Current, settings: SettingsDep) -> Mt5StatusOut:
    """Live read of the bot's export: health, agents, positions, backtests and performance
    calculated from its journal (labelled `calculated`). Nothing is sent to the bot."""
    adapter = _mt5(request)
    status_ = await call_adapter(adapter.status(), settings)
    ok, why = adapter.assess(status_)
    agents = {
        sym: _jsonable_dict(
            a.model_dump(
                mode="python",
                include={
                    "strategy",
                    "exec_tf",
                    "alive",
                    "snapshot_at",
                    "market_open",
                    "connected",
                    "mt5",
                    "tick_age",
                    "positions",
                    "today",
                    "offset_h",
                    "latest_signal",
                },
            )
        )
        for sym, a in status_.agents.items()
    }
    return Mt5StatusOut(
        ok=ok,
        detail=why,
        source="file" if adapter.source.kind == "file" else "http",
        generated_at=status_.generated_at,
        age_s=int(status_.age().total_seconds()),
        bot=_jsonable_dict(status_.bot.model_dump(mode="python")),
        agents=agents,
        halt=_jsonable_dict(status_.halt) if status_.halt else None,
        backtests=[_jsonable_dict(b) for b in status_.backtests],
        performance=mt5_sync.performance(status_),
        export_errors=status_.errors,
    )


@router.put("/mt5/instruments/{symbol}", response_model=InstrumentOut)
async def put_mt5_instrument(
    symbol: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._#-]+$")],
    body: Mt5InstrumentIn,
    request: Request,
    db: DbSession,
    admin: AdminRequired,
    settings: SettingsDep,
) -> InstrumentOut:
    """Register an MT5 symbol's contract spec so the bot's signals and positions can be
    linked to it. Admin-only, CSRF-protected, audited."""
    sym = symbol.upper()
    ins = insert(Instrument).values(
        broker=Broker.MT5.value,
        broker_instrument_id=sym,
        symbol=sym,
        instrument_type=body.instrument_type,
        currency=body.currency,
        lot_size=body.lot_size,
        tick_size=body.tick_size,
    )
    inst_id = await db.scalar(
        ins.on_conflict_do_update(
            constraint="uq_instruments_broker_broker_instrument_id",
            set_={
                k: ins.excluded[k] for k in ("instrument_type", "currency", "lot_size", "tick_size")
            },
        ).returning(Instrument.id)
    )
    if settings.MT5_SERVER:
        spec = {
            "digits": body.digits,
            "point": body.tick_size,
            "contract_size": body.contract_size,
            "volume_min": body.volume_min,
            "volume_step": body.volume_step,
            "volume_max": body.volume_max,
            "currency_profit": body.currency,
            "trading_instrument_id": inst_id,
            "raw": {"source": "admin"},
        }
        s_ins = insert(Mt5Symbol).values(server=settings.MT5_SERVER, symbol=sym, **spec)
        await db.execute(
            s_ins.on_conflict_do_update(
                constraint="pk_symbols", set_={k: s_ins.excluded[k] for k in spec}
            )
        )
    audit.record(
        db,
        "trading_mt5_instrument_set",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={
            "symbol": sym,
            "lot_size": str(body.lot_size),
            "tick_size": str(body.tick_size),
            "currency": body.currency,
        },
    )
    await db.commit()
    row = await db.get(Instrument, inst_id)
    assert row is not None
    await db.refresh(row)
    return InstrumentOut.model_validate(row)
