"""Stock Signals tools for Claude (docs/37 §9). All read-only.

They hand Claude the numbers the server computed (EMA 8/32 + session VWAP signals, rankings,
the portfolio signal map, the rebalance preview), each with its data time, so Claude can
explain them. Claude never computes, changes or invents prices, scores or allocations here,
cannot change risk limits, and nothing here places, drafts or approves an order (the only
order path stays propose_trade → a person's approval → a PAPER intent).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.ai.tools.registry import Tool, ToolContext, obj
from app.services import portfolio_map, stock_rankings

AGENTS = frozenset({"investments", "report"})
TF = {"type": "string", "enum": ["5m", "15m", "1h", "1d"]}
MAX_ROWS = 15


def _n(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    return str(v)


def _t(v: datetime | None) -> str:
    return "n/a" if v is None else f"{v:%Y-%m-%d %H:%M} UTC"


def _viewer(ctx: ToolContext) -> portfolio_map.Viewer:
    return portfolio_map.Viewer(ctx.user_id, ctx.is_admin)


async def get_stock_signals(ctx: ToolContext, args: dict[str, Any]) -> str:
    tf = args.get("timeframe") or "15m"
    hours = int(args.get("hours") or 24)
    limit = min(int(args.get("limit") or 10), MAX_ROWS)
    async with ctx.maker() as db:
        rows, total = await stock_rankings.list_events(
            db,
            timeframe=tf,
            direction=args.get("direction"),
            since=datetime.now(UTC) - timedelta(hours=hours),
            limit=limit,
        )
    if not rows:
        return f"No EMA 8/32 crossovers on {tf} in the last {hours} h."
    lines = [
        f"- {r['symbol']} ({r['exchange']}): {r['direction']} {r['state']}, crossed "
        f"{_t(r['crossed_at'])}, confirmed {_t(r['confirmed_at'])}; EMA8 {_n(r['ema_fast'])}, "
        f"EMA32 {_n(r['ema_slow'])}, VWAP {_n(r['vwap'])}, close {_n(r['last_price'])}, "
        f"score {_n(r['trend_score'])}{' [held]' if r['in_holdings'] else ''}"
        for r in rows
    ]
    return f"{total} crossover(s) on {tf} in the last {hours} h (newest first):\n" + "\n".join(
        lines
    )


async def get_market_rankings(ctx: ToolContext, args: dict[str, Any]) -> str:
    kind = args.get("kind") or "gainers"
    tf = args.get("timeframe") or "15m"
    limit = min(int(args.get("limit") or 10), MAX_ROWS)
    async with ctx.maker() as db:
        if kind == "gainers":
            rows = await stock_rankings.movers(db, tf, "gainers", limit=limit)
        elif kind == "losers":
            rows = await stock_rankings.movers(db, tf, "losers", limit=limit)
        elif kind == "unusual_volume":
            rows = await stock_rankings.unusual_volume(
                db, tf, ctx.settings.STOCK_SIGNALS_UNUSUAL_RVOL, limit=limit
            )
        else:
            rows = await stock_rankings.trending(
                db, tf, "bearish" if kind == "trending_bearish" else "bullish", limit=limit
            )
        cov = await stock_rankings.coverage(db, tf)
    head = (
        f"{kind} on {tf}, data as of {_t(cov.get('data_as_of'))} "
        f"({cov.get('evaluated', 0)} evaluated of {cov.get('universe_size', 0)}, "
        f"{cov.get('unavailable', 0)} without usable data, {cov.get('illiquid', 0)} illiquid). "
        "The trend score ranks trend strength; it is not a probability of profit."
    )
    if cov.get("run_id") is None:
        return "No finished Stock Signals run yet for " + tf + "."
    if not rows:
        return head + "\nNothing qualifies."
    lines = [
        f"- {r['symbol']}: change {_n(r['change_pct'])}%, score {_n(r['trend_score'])}, "
        f"state {r['state']}, rvol {_n(r['rvol'])}, RSI {_n(r['rsi'])}, ADX {_n(r['adx'])}, "
        f"last {_n(r['last_price'])}"
        for r in rows
    ]
    return head + "\n" + "\n".join(lines)


async def get_portfolio_signal_health(ctx: ToolContext, args: dict[str, Any]) -> str:
    tf = args.get("timeframe") or "15m"
    async with ctx.maker() as db:
        m = await portfolio_map.build(db, _viewer(ctx), ctx.settings, tf)
    if not m["holdings"]:
        return "No Dhan holdings snapshot for your accounts yet (the reconcile job writes it)."
    t, f = m["totals"], m["freshness"]
    head = (
        f"{len(m['holdings'])} holdings: invested {t['invested']}, current {t['current']}, "
        f"unrealized P&L {t['unrealized_pnl']}; cash {m['available_cash'] or 'n/a'}. "
        f"Holdings as of {_t(f['holdings_as_of'])}, signals as of {_t(f['signals_as_of'])}. "
        f"Classes: {m['classification_counts']}. {m['disclaimer']}"
    )
    lines = [
        f"- {h['symbol']}: {h['classification']} ({'; '.join(h['reasons'])}); "
        f"alloc {h['allocation_pct'] or 'n/a'}%, P&L {h['unrealized_pnl'] or 'n/a'} "
        f"({h['unrealized_pnl_pct'] or 'n/a'}%), state {h['signal_state'] or 'n/a'}"
        for h in m["holdings"][:MAX_ROWS]
    ]
    return head + "\n" + "\n".join(lines)


async def get_portfolio_candidates(ctx: ToolContext, args: dict[str, Any]) -> str:
    tf = args.get("timeframe") or "15m"
    async with ctx.maker() as db:
        m = await portfolio_map.build(db, _viewer(ctx), ctx.settings, tf)
        held = {h["instrument_id"] for h in m["holdings"]}
        rows = await portfolio_map.candidates(db, ctx.settings, tf, held)
    if not rows:
        return "No liquid bullish instrument outside your holdings passes the filters now."
    lines = [
        f"- {c.symbol}: {c.state}, trend score {_n(c.score)}, last {_n(c.price)}"
        for c in rows[:MAX_ROWS]
    ]
    return (
        "Candidates to compare with your holdings (filters: liquid, bullish state with agreeing "
        f"EMAs, score >= {ctx.settings.PORTFOLIO_ADD_MIN_SCORE}). A candidate is not a "
        "recommendation to switch; a crossover alone never justifies replacing a holding.\n"
        + "\n".join(lines)
    )


async def get_rebalance_preview(ctx: ToolContext, args: dict[str, Any]) -> str:
    s = ctx.settings
    tf = args.get("timeframe") or "15m"
    max_w = Decimal(str(args.get("max_weight_pct") or s.PORTFOLIO_MAX_WEIGHT_PCT))
    async with ctx.maker() as db:
        p = await portfolio_map.rebalance_preview(
            db,
            _viewer(ctx),
            s,
            tf=tf,
            max_weight_pct=max_w,
            cash_buffer_pct=Decimal(str(s.PORTFOLIO_CASH_BUFFER_PCT)),
            max_new=s.PORTFOLIO_CANDIDATES,
            min_trade_value=Decimal(str(s.PORTFOLIO_MIN_TRADE_VALUE)),
        )
    trades = [
        f"- {t['side']} {t['quantity']} {t['symbol']} @ {t['price']} = {t['value']} "
        f"(charges {t['charges']}): {t['reason']}"
        for t in p["trades"]
    ] or ["- no trades: nothing is overweight and no alternative fits"]
    return (
        f"Rebalance PREVIEW (deterministic, max weight {max_w}%): cash {p['cash_before']} → "
        f"{p['cash_after']}, cash required {p['cash_required']}, charges {p['total_charges']}.\n"
        + "\n".join(trades)
        + (f"\nFor review (not traded): {'; '.join(p['review'])}" if p["review"] else "")
        + f"\nAllocation after: {p['allocation_after']}\n"
        + " ".join(p["notes"])
    )


TOOLS = [
    Tool(
        "get_stock_signals",
        "Recent EMA 8/32 crossovers with session-VWAP confirmation (NSE/BSE equities): state, "
        "cross and confirmation times, EMA/VWAP/close values, trend score, whether held. "
        "Computed by the server on closed candles.",
        obj(
            {
                "timeframe": TF,
                "direction": {"type": "string", "enum": ["bullish", "bearish"]},
                "hours": {"type": "integer", "minimum": 1, "maximum": 168},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
            }
        ),
        get_stock_signals,
        "read",
        AGENTS,
    ),
    Tool(
        "get_market_rankings",
        "Market rankings from the latest Stock Signals run with coverage and data time: "
        "gainers/losers by real % change, trending bullish/bearish by the 0-100 trend score "
        "(not a probability), or unusual volume.",
        obj(
            {
                "kind": {
                    "type": "string",
                    "enum": [
                        "gainers",
                        "losers",
                        "trending_bullish",
                        "trending_bearish",
                        "unusual_volume",
                    ],
                },
                "timeframe": TF,
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
            }
        ),
        get_market_rankings,
        "read",
        AGENTS,
    ),
    Tool(
        "get_portfolio_signal_health",
        "The user's Dhan holdings with value, P&L, allocation, cash, EMA/VWAP state and the "
        "advisory class (HOLD_CANDIDATE, MONITOR, REVIEW_RISK, POTENTIAL_ADD) with its reasons.",
        obj({"timeframe": TF}),
        get_portfolio_signal_health,
        "read",
        AGENTS,
    ),
    Tool(
        "get_portfolio_candidates",
        "Liquid bullish instruments not already held that pass the portfolio filters: things "
        "to compare with the holdings, not recommendations to switch.",
        obj({"timeframe": TF}),
        get_portfolio_candidates,
        "read",
        AGENTS,
    ),
    Tool(
        "get_rebalance_preview",
        "The deterministic rebalance preview (trims of overweight holdings, alternatives, "
        "charges, cash required, allocation after) for Claude to explain. Places and drafts "
        "nothing.",
        obj(
            {
                "timeframe": TF,
                "max_weight_pct": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
            }
        ),
        get_rebalance_preview,
        "read",
        AGENTS,
    ),
]
