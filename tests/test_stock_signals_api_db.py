"""Stock Signals API contract and Claude's tools (docs/37 §7, §9): sign-in on every route,
filters, paging and sorting on the server, static paths never parsed as an id, rankings
with coverage, and read-only tools that report the stored numbers."""

from __future__ import annotations

import numpy as np
import pytest

from app.ai.tools import stock_signals as tools
from app.ai.tools.registry import ToolContext
from tests.test_market_db import Env, EnvFactory, make_env  # noqa: F401
from tests.test_stock_signals_db import (  # noqa: F401
    DAYS,
    SIGNALS,
    clean_runs,
    ist,
    pending_then_confirmed,
    put,
    run_at,
    session,
)

pytestmark = pytest.mark.db
BASE = "/api/trading/stock-signals"


async def seeded(env: Env) -> dict[str, int]:
    """A pending-then-confirmed cross (SIGNAL), a steady riser and a faller, evaluated at
    Thursday 10:15 IST."""
    ids = {"SIGNAL": await env.instrument("SIGNAL")}
    await pending_then_confirmed(env, ids["SIGNAL"])
    times = session(DAYS[0]) + session(DAYS[1]) + session(DAYS[2]) + session(DAYS[3])[:4]
    n = len(times)
    for sym, closes in (("RISER", np.linspace(100, 160, n)), ("FALLER", np.linspace(160, 100, n))):
        ids[sym] = await env.instrument(sym)
        await put(env, ids[sym], times, [float(c) for c in closes], [100.0] * n)
    await run_at(env, ist(DAYS[3], 10, 0))
    await run_at(env, ist(DAYS[3], 10, 15))
    return ids


async def test_every_route_needs_sign_in(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS) as env:
        for path in ("", "/states", "/market-movers", "/trending", "/portfolio-map", "/1",
                     "/notifications", "/notifications/preferences"):  # fmt: skip
            assert (await env.c["anon"].get(BASE + path)).status_code == 401, path
        assert (await env.c["anon"].post(BASE + "/rebalance-preview", json={})).status_code in (
            401,
            403,
        )


async def test_signals_list_detail_and_rankings(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS, STOCK_SIGNALS_MIN_DAILY_TURNOVER=0) as env:
        ids = await seeded(env)
        u = env.c["user"]

        page = (await u.get(BASE, params={"timeframe": "15m"})).json()
        assert page["total"] == 1 and page["items"][0]["symbol"] == "SIGNAL"
        ev = page["items"][0]
        assert ev["state"] == "BULLISH_CONFIRMED" and ev["direction"] == "bullish"
        assert ev["crossed_at"] < ev["confirmed_at"]
        assert (await u.get(BASE, params={"confirmation": "pending"})).json()["total"] == 0
        assert (await u.get(BASE, params={"symbol": "sig"})).json()["total"] == 1
        assert (await u.get(BASE, params={"direction": "bearish"})).json()["total"] == 0
        assert (await u.get(BASE, params={"since": "2026-11-12T00:00:00"})).status_code == 422
        assert (await u.get(BASE, params={"exchange": "LSE"})).status_code == 422

        detail = (await u.get(f"{BASE}/{ev['id']}")).json()
        assert detail["event"]["id"] == ev["id"]
        assert detail["current"]["instrument_id"] == ids["SIGNAL"]
        assert (await u.get(f"{BASE}/999999")).status_code == 404

        # Static paths are routes, not ids.
        movers = await u.get(f"{BASE}/market-movers", params={"kind": "gainers"})
        assert movers.status_code == 200
        m = movers.json()
        assert m["coverage"]["universe_size"] == 3 and m["coverage"]["data_as_of"]
        assert [r["symbol"] for r in m["items"]][:1] in (["RISER"], ["SIGNAL"])
        losers = (await u.get(f"{BASE}/market-movers", params={"kind": "losers"})).json()
        assert [r["symbol"] for r in losers["items"]] == ["FALLER"]
        bull = (await u.get(f"{BASE}/trending", params={"direction": "bullish"})).json()
        assert "RISER" in {r["symbol"] for r in bull["items"]}
        assert "not a probability" in bull["note"]
        states = (await u.get(f"{BASE}/states", params={"state": ["BEARISH_TREND"]})).json()
        assert [r["symbol"] for r in states["items"]] == ["FALLER"]
        two = (await u.get(f"{BASE}/states", params={"limit": 2})).json()
        assert two["total"] == 3 and len(two["items"]) == 2


async def test_claude_tools_report_stored_numbers(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS, STOCK_SIGNALS_MIN_DAILY_TURNOVER=0) as env:
        await seeded(env)
        ctx = ToolContext(settings=env.settings, maker=env.maker, user_id=env.ids["user"],
                          is_admin=False, agent="investments")  # fmt: skip
        sig = await tools.get_stock_signals(ctx, {"hours": 168})
        assert "SIGNAL (NSE): bullish BULLISH_CONFIRMED" in sig
        rank = await tools.get_market_rankings(ctx, {"kind": "losers"})
        assert "FALLER" in rank and "not a probability of profit" in rank
        health = await tools.get_portfolio_signal_health(ctx, {})
        assert "No Dhan holdings snapshot" in health
        preview = await tools.get_rebalance_preview(ctx, {})
        assert "PREVIEW" in preview and "no order is placed" in preview
        assert {t.kind for t in tools.TOOLS} == {"read"}
