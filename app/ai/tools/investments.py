"""Investment tools. Reads come from the database only (snapshots, screener runs, the Aureon
MT5 bot's own tables): no broker call per question. The one write tool, propose_trade, files
an approval after the hard risk limits in app/ai/risk.py pass; an approved proposal becomes
a PAPER order intent (app/ai/approvals.py), never a live order.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from app.ai import risk
from app.ai.budget import IST
from app.ai.tools.registry import Draft, Tool, ToolContext, ToolRefused, obj
from app.aureon import config as aureon_config
from app.aureon import db as aureon_db
from app.models.ai import Approval
from app.models.trading import (
    BrokerAccount,
    Instrument,
    PositionSnapshot,
    ScreenerResult,
    ScreenerRun,
)
from app.services import mt5_records

AGENTS = frozenset({"investments", "report"})
MAX_ROWS = 25


def _n(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, Decimal):
        return f"{v.normalize():f}"
    return str(v)


async def _accounts(ctx: ToolContext) -> list[BrokerAccount]:
    q = select(BrokerAccount).where(BrokerAccount.is_active.is_(True))
    if not ctx.is_admin:
        q = q.where(BrokerAccount.owner_user_id == ctx.user_id)
    async with ctx.maker() as db:
        return list((await db.scalars(q.order_by(BrokerAccount.broker))).all())


async def _latest_snapshots(ctx: ToolContext, account: BrokerAccount) -> list[PositionSnapshot]:
    async with ctx.maker() as db:
        last = await db.scalar(
            select(func.max(PositionSnapshot.as_of)).where(
                PositionSnapshot.broker_account_id == account.id
            )
        )
        if last is None:
            return []
        return list(
            (
                await db.scalars(
                    select(PositionSnapshot)
                    .where(
                        PositionSnapshot.broker_account_id == account.id,
                        PositionSnapshot.as_of == last,
                        PositionSnapshot.quantity != 0,
                    )
                    .order_by(PositionSnapshot.symbol)
                )
            ).all()
        )


async def get_portfolio_summary(ctx: ToolContext, _: dict[str, Any]) -> str:
    accounts = await _accounts(ctx)
    if not accounts:
        return "No trading accounts are set up."
    lines = []
    for a in accounts:
        snaps = await _latest_snapshots(ctx, a)
        upnl = sum((s.unrealized_pnl or Decimal(0) for s in snaps), Decimal(0))
        rpnl = sum((s.realized_pnl or Decimal(0) for s in snaps), Decimal(0))
        exposure = sum((abs(s.quantity) * (s.last_price or s.avg_price) for s in snaps), Decimal(0))
        as_of = max((s.as_of for s in snaps), default=None)
        lines.append(
            f"- {a.broker} account ({a.label or 'unlabelled'}), mode {a.mode}, "
            f"{len(snaps)} open positions, exposure {_n(exposure.quantize(Decimal('0.01')))} "
            f"{a.base_currency}, unrealized P&L {_n(upnl.quantize(Decimal('0.01')))}, "
            f"realized {_n(rpnl.quantize(Decimal('0.01')))}"
            + (f", as of {as_of:%Y-%m-%d %H:%M} UTC" if as_of else ", no snapshot yet")
        )
    return "Portfolio by account:\n" + "\n".join(lines)


async def get_positions(ctx: ToolContext, args: dict[str, Any]) -> str:
    want = (args.get("broker") or "").lower()
    rows: list[str] = []
    for a in await _accounts(ctx):
        if want and a.broker != want:
            continue
        for s in await _latest_snapshots(ctx, a):
            rows.append(
                f"- {s.symbol} ({a.broker}, {a.mode}): qty {_n(s.quantity)}, avg {_n(s.avg_price)}"
                f", last {_n(s.last_price)}, unrealized {_n(s.unrealized_pnl)} {s.currency}"
            )
    if not rows:
        return "No open positions in the latest snapshots."
    more = f"\n…and {len(rows) - MAX_ROWS} more" if len(rows) > MAX_ROWS else ""
    return f"Open positions ({len(rows)}):\n" + "\n".join(rows[:MAX_ROWS]) + more


async def get_dhan_screener_results(ctx: ToolContext, args: dict[str, Any]) -> str:
    limit = int(args.get("limit") or 15)
    async with ctx.maker() as db:
        q = select(ScreenerRun).where(ScreenerRun.status == "succeeded")
        if not ctx.is_admin:
            q = q.where(ScreenerRun.owner_user_id == ctx.user_id)
        run = await db.scalar(q.order_by(ScreenerRun.created_at.desc()).limit(1))
        if run is None:
            return "No finished screener run yet. Run one from the Screener page."
        rows = (
            await db.execute(
                select(Instrument.trading_symbol, Instrument.symbol, ScreenerResult.metrics)
                .join(Instrument, Instrument.id == ScreenerResult.instrument_id)
                .where(ScreenerResult.run_id == run.id, ScreenerResult.outcome == "match")
                .order_by(Instrument.symbol)
                .limit(limit)
            )
        ).all()
    head = (
        f"Latest screener run {run.name or ''} ({run.timeframe}) at "
        f"{run.created_at:%Y-%m-%d %H:%M} UTC: {run.matched} matches of {run.scanned} scanned"
        + (f", data as of {run.data_as_of:%Y-%m-%d %H:%M} UTC" if run.data_as_of else "")
    )
    lines = []
    for tsym, sym, metrics in rows:
        m = metrics or {}
        keys = [k for k in ("close", "change_pct", "volume") if k in m]
        extra = ", ".join(f"{k} {m[k]}" for k in keys)
        lines.append(f"- {tsym or sym}" + (f": {extra}" if extra else ""))
    return head + ("\n" + "\n".join(lines) if lines else "\nNo matches.")


async def get_mt5_bot_status(ctx: ToolContext, _: dict[str, Any]) -> str:
    async with ctx.maker() as db:
        state = await aureon_db.get_state(db)
        beats = await aureon_db.q(
            db, "select service,state,last_beat from aureon.service_heartbeats order by service"
        )
        trades = await aureon_db.q(
            db,
            """select dir,lots,entry_price,sl,status from aureon.trades
               where status in ('open','closing','unreconciled') order by id limit 10""",
        )
        risk_row = await aureon_db.q(
            db,
            """select state,drawdown from aureon.risk_state
               order by broker_day desc, updated_at desc limit 1""",
            one=True,
        )
    hb = ", ".join(
        f"{b['service']} {b['state']} (last {b['last_beat']:%Y-%m-%d %H:%M} UTC)"
        if b.get("last_beat")
        else f"{b['service']} {b['state']}"
        for b in beats
    )
    tr = "; ".join(
        f"{t['dir']} {t['lots']} lots at {t['entry_price']} SL {t['sl']} ({t['status']})"
        for t in trades
    )
    rk = f"{risk_row['state']}, drawdown {risk_row['drawdown']}" if risk_row else "unknown"
    return (
        f"Aureon MT5 bot ({aureon_config.SYMBOL}, rules {aureon_config.RULES_VERSION}): "
        f"state {state}; risk {rk}.\nServices: {hb or 'none reporting'}.\n"
        f"Open trades: {tr or 'none'}."
    )


async def get_mt5_recent_signals(ctx: ToolContext, args: dict[str, Any]) -> str:
    days = int(args.get("days") or 7)
    since = datetime.now(UTC) - timedelta(days=days)
    async with ctx.maker() as db:
        rows = await aureon_db.q(
            db,
            """select s.time_utc,s.kind,s.dir,s.price,s.suggested_sl,
               (select tag from aureon.decisions d where d.signal_id=s.id
                order by tagged_at desc limit 1) tag, t.status trade_status, t.pnl_usd
               from aureon.signals s left join aureon.trades t on t.signal_id=s.id
               where s.source='live' and s.time_utc>=:since
               order by s.time_utc desc limit :lim""",
            {"since": since, "lim": MAX_ROWS},
        )
    if not rows:
        return f"No live Aureon signals in the last {days} days."
    lines = [
        f"- {r['time_utc']:%Y-%m-%d %H:%M} UTC {r['kind']} {r['dir']} at {r['price']}, "
        f"SL {r['suggested_sl']}, decision {r['tag'] or 'none'}"
        + (f", trade {r['trade_status']} P&L {r['pnl_usd']} USD" if r["trade_status"] else "")
        for r in rows
    ]
    return f"Live signals, last {days} days (newest first):\n" + "\n".join(lines)


def _when(t: datetime | None) -> str:
    return f"{t:%Y-%m-%d %H:%M} UTC" if t else "never"


async def mt5_records_summary(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Aggregates over the 1.7.x bot's records (mt5.records) and bars (mt5.bars), never rows."""
    days = int(args.get("days") or 7)
    async with ctx.maker() as db:
        symbol = await mt5_records.resolve_symbol(db, str(args.get("symbol") or ""))
        s = await mt5_records.summary(db, symbol, days)
    if not s["records_total"] and not s["bars_newest"]:
        return f"No MT5 bot records or bars stored for {symbol} yet."
    side = s["crosses_by_side"]
    tag = s["crosses_by_tag"]
    crosses = sum(side.values())
    labels = ", ".join(f"{lb} {n}" for lb, n in s["labels"][:12]) or "none"
    closed = s["positions_closed"]
    win = f"{s['positions_won']} won" + (
        f" ({100 * s['positions_won'] / closed:.0f}%)" if closed else ""
    )
    ds = s["dataset_rows"]
    widened = (
        f"{s['dataset_widened']} of {ds} completed crosses widened "
        f"({100 * s['dataset_widened'] / ds:.0f}%)"
        if ds
        else "no completed cross in the period"
    )
    lag = (
        f", {int((s['now'] - s['newest_utc_time']).total_seconds() // 60)} min ago"
        if s["newest_utc_time"]
        else ""
    )
    # Account logins go through the redactor: Claude sees [ACCOUNT_ID], never the number.
    accounts = ", ".join(ctx.redactor.text(f"login {a}") for a in s["accounts"])
    text = (
        f"MT5 bot records for {symbol}, last {days} days (since {_when(s['since'])}):\n"
        f"- crosses: {crosses} (long {side.get('long', 0)}, short {side.get('short', 0)}"
        + (f", unknown {side['unknown']}" if side.get("unknown") else "")
        + f"); above 200 {tag.get('above 200', 0)}, below 200 {tag.get('below 200', 0)}\n"
        f"- labels: {labels}\n"
        f"- positions: {s['positions_opened']} opened, {closed} closed, {win}, "
        f"total result {_n(s['result_total'])}\n"
        f"- SL changes: {s['sl_changes']}\n"
        f"- cross dataset ({mt5_records.DEFAULT_TIMEFRAME}, y_widened): {widened}\n"
        f"- freshness: newest record {_when(s['newest_utc_time'])}{lag}, received "
        f"{_when(s['newest_received_at'])}; newest {mt5_records.DEFAULT_TIMEFRAME} bar "
        f"{_when(s['bars_newest'])}" + (f"\n- accounts: {accounts}" if accounts else "")
    )
    return ctx.redactor.text(text)


async def _proposals_today(ctx: ToolContext) -> int:
    local = datetime.now(UTC).astimezone(IST)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    async with ctx.maker() as db:
        n = await db.scalar(
            select(func.count())
            .select_from(Approval)
            .where(
                Approval.action == "paper_trade",
                Approval.created_at >= start,
                Approval.status.not_in(("rejected", "expired")),
            )
        )
    return int(n or 0)


async def propose_trade(ctx: ToolContext, args: dict[str, Any]) -> Draft:
    try:
        p = risk.parse(args)
        risk.check(ctx.settings, p, await _proposals_today(ctx))
    except risk.RiskLimitBreached as exc:
        raise ToolRefused(f"rejected by the risk limits: {exc}") from None
    async with ctx.maker() as db:
        inst = await db.scalar(
            select(Instrument)
            .where(
                Instrument.is_active.is_(True),
                func.upper(Instrument.trading_symbol) == p.symbol,
                Instrument.broker == "dhan",
                Instrument.segment.in_(("NSE_EQ", "BSE_EQ")),
                Instrument.instrument_type == "EQUITY",
            )
            .order_by(Instrument.segment.desc())  # NSE_EQ before BSE_EQ
            .limit(1)
        )
    if inst is None:
        raise ToolRefused(f"{p.symbol} is not an active NSE/BSE equity in the instrument list")
    payload = {
        "symbol": p.symbol,
        "instrument_id": inst.id,
        "segment": inst.segment,
        "side": p.side,
        "qty": str(p.quantity),
        "entry": str(p.entry),
        "stop": str(p.stop),
        "target": str(p.target),
        "risk_inr": str(p.risk_inr),
        "notional_inr": str(p.notional_inr),
        "reason": p.reason[:500],
    }
    rr = (p.reward_inr / p.risk_inr).quantize(Decimal("0.01")) if p.risk_inr else Decimal(0)
    summary = (
        f"PAPER {p.side} {p.quantity} {p.symbol} at {p.entry} (stop {p.stop}, target "
        f"{p.target}); risk {p.risk_inr:.2f} INR, size {p.notional_inr:.2f} INR, R:R {rr}"
    )
    return Draft("paper_trade", payload, summary)


TOOLS = [
    Tool(
        "get_portfolio_summary",
        "Totals per trading account (mode, open positions, exposure, unrealized and realized "
        "P&L) from the latest position snapshots. No input.",
        obj({}),
        get_portfolio_summary,
        "read",
        AGENTS,
    ),
    Tool(
        "get_positions",
        "Open positions from the latest snapshots (symbol, qty, avg, last, unrealized P&L). "
        "Optionally one broker.",
        obj({"broker": {"type": "string", "enum": ["dhan", "mt5"]}}),
        get_positions,
        "read",
        AGENTS,
    ),
    Tool(
        "get_dhan_screener_results",
        "Matches from the latest finished Dhan screener run (symbol, close, change %, volume).",
        obj({"limit": {"type": "integer", "minimum": 1, "maximum": 25}}),
        get_dhan_screener_results,
        "read",
        AGENTS,
    ),
    Tool(
        "get_mt5_bot_status",
        "The Aureon MT5 bot: state, risk state, service heartbeats and open trades (read-only).",
        obj({}),
        get_mt5_bot_status,
        "read",
        AGENTS,
    ),
    Tool(
        "get_mt5_recent_signals",
        "The Aureon MT5 bot's recent live signals with the decision taken and trade result.",
        obj({"days": {"type": "integer", "minimum": 1, "maximum": 30}}),
        get_mt5_recent_signals,
        "read",
        AGENTS,
    ),
    Tool(
        "mt5_records_summary",
        "Aggregates from the MT5 bot's own records and bars for one symbol: crosses by side "
        "and by above/below EMA 200, counts per label, positions opened/closed with wins and "
        "total result, SL changes, the share of completed EMA 8/32 crosses that widened, and "
        "data freshness. Read-only, never raw rows.",
        obj(
            {
                "symbol": {"type": "string", "minLength": 1, "maxLength": 64},
                "days": {"type": "integer", "minimum": 1, "maximum": 90},
            },
            ["symbol"],
        ),
        mt5_records_summary,
        "read",
        AGENTS,
    ),
    Tool(
        "propose_trade",
        "Propose a PAPER trade in an NSE/BSE equity for the user to approve. It is never "
        "executed by you: it is checked against hard risk limits, then waits for approval. "
        "qty is a whole number of shares; prices in INR.",
        obj(
            {
                "symbol": {"type": "string", "minLength": 1, "maxLength": 40},
                "side": {"type": "string", "enum": ["BUY", "SELL"]},
                "qty": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
                "entry": {"type": "number", "exclusiveMinimum": 0},
                "stop": {"type": "number", "exclusiveMinimum": 0},
                "target": {"type": "number", "exclusiveMinimum": 0},
                "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            },
            ["symbol", "side", "qty", "entry", "stop", "target", "reason"],
        ),
        propose_trade,
        "write",
        frozenset({"investments"}),
    ),
]
