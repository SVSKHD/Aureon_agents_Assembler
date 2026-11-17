"""Portfolio signal map against Postgres (docs/37 §6): the latest holdings snapshot only,
owners see their own accounts, prices and classes from stored signals, cash from the latest
funds snapshot, holder notifications, and a rebalance preview that places nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.brokers import DhanFundSnapshot
from app.models.stock_signals import HoldingSnapshot, StockSignalNotification, StockSignalState
from app.models.trading import BrokerAccount, OrderIntent
from app.services import portfolio_map
from app.services.stock_signals import RunResult
from tests.test_market_db import Env, EnvFactory, make_env  # noqa: F401
from tests.test_stock_signals_db import SIGNALS, clean_runs  # noqa: F401

pytestmark = pytest.mark.db
NOW = datetime.now(UTC).replace(microsecond=0)


async def account(env: Env, who: str) -> BrokerAccount:
    async with env.maker() as db, db.begin():
        a = BrokerAccount(broker="dhan", account_id=f"11000{who}", base_currency="INR",
                          owner_user_id=env.ids[who], mode="OBSERVE")  # fmt: skip
        db.add(a)
        await db.flush()
        return a


async def hold(env: Env, acct: BrokerAccount, iid: int | None, sym: str, qty: str, avg: str,
               ltp: str, as_of: datetime) -> None:  # fmt: skip
    async with env.maker() as db, db.begin():
        db.add(HoldingSnapshot(broker_account_id=acct.id, instrument_id=iid, symbol=sym,
                               isin=f"INE{sym}", quantity=Decimal(qty),
                               available_quantity=Decimal(qty), avg_cost=Decimal(avg),
                               last_price=Decimal(ltp), as_of=as_of))  # fmt: skip


async def state(env: Env, iid: int, st: str, close: str, score: str | None, at: datetime) -> None:
    async with env.maker() as db, db.begin():
        db.add(StockSignalState(instrument_id=iid, timeframe="15m", state=st,
                                last_price=Decimal(close), ema_fast=Decimal("101"),
                                ema_slow=Decimal("100"), vwap=Decimal("99"),
                                trend_score=Decimal(score) if score else None,
                                candle_close_at=at, evaluated_at=at))  # fmt: skip


async def test_map_values_classes_cash_and_visibility(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS) as env:
        acct = await account(env, "user")
        good = await env.instrument("GOOD")
        weak = await env.instrument("WEAK")
        # An older snapshot that must not count.
        await hold(env, acct, good, "GOOD", "999", "1", "1", NOW - timedelta(days=1))
        await hold(env, acct, good, "GOOD", "10", "100", "110", NOW - timedelta(minutes=10))
        await hold(env, acct, weak, "WEAK", "1", "200", "180", NOW - timedelta(minutes=10))
        # GOOD's candle is newer than the snapshot: its close is the price used.
        await state(env, good, "BULLISH_TREND", "120", "80", NOW - timedelta(minutes=2))
        await state(env, weak, "BEARISH_CONFIRMED", "170", "20", NOW - timedelta(hours=1))
        async with env.maker() as db, db.begin():
            db.add(DhanFundSnapshot(broker_account_id=acct.id, available_balance=Decimal("500"),
                                    as_of=NOW))  # fmt: skip

        r = await env.c["user"].get("/api/trading/stock-signals/portfolio-map")
        assert r.status_code == 200, r.text
        m = r.json()
        rows = {h["symbol"]: h for h in m["holdings"]}
        assert set(rows) == {"GOOD", "WEAK"} and rows["GOOD"]["quantity"] == "10.000000"
        g, w = rows["GOOD"], rows["WEAK"]
        assert (g["last_price"], g["price_source"]) == ("120.00000000", "candle_close")
        assert (w["last_price"], w["price_source"]) == ("180.00000000", "dhan_holdings")
        assert g["current_value"] == "1200.00" and g["unrealized_pnl"] == "200.00"
        assert g["allocation_pct"] == "86.96" and w["allocation_pct"] == "13.04"
        # GOOD is bullish but 87% of value (> 20%): hold, not add. WEAK is bearish: review.
        assert g["classification"] == "HOLD_CANDIDATE" and w["classification"] == "REVIEW_RISK"
        assert g["ema_state"] == "above" and g["vwap_state"] == "above"
        assert m["available_cash"] == "500.0000" and m["sector_exposure"]["available"] is False
        assert m["totals"]["current"] == "1380.00" and m["accounts"] == 1

        other = (await env.c["other"].get("/api/trading/stock-signals/portfolio-map")).json()
        assert other["holdings"] == [] and other["accounts"] == 0
        admin = (await env.c["admin"].get("/api/trading/stock-signals/portfolio-map")).json()
        assert {h["symbol"] for h in admin["holdings"]} == {"GOOD", "WEAK"}

        # Rebalance preview: CSRF required. Portfolio 1,880 → cap 376: GOOD (1,200) trims 6
        # shares; WEAK (180) is under the cap, so it is flagged for review, not sold.
        url = "/api/trading/stock-signals/rebalance-preview"
        assert (await env.c["user"].post(url, json={})).status_code == 403
        r = await env.send("user", "POST", url, json={"cash_buffer_pct": "0"})
        assert r.status_code == 200, r.text
        p = r.json()
        sells = [t for t in p["trades"] if t["side"] == "SELL"]
        assert [(t["symbol"], t["quantity"]) for t in sells] == [("GOOD", "6")]
        assert Decimal(sells[0]["charges"]) > 0
        assert any(x.startswith("WEAK:") for x in p["review"])
        assert any("no order is placed" in n for n in p["notes"])
        async with env.maker() as db:
            assert await db.scalar(select(func.count()).select_from(OrderIntent)) == 0


async def test_holders_get_reversal_notifications_once(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS) as env:
        acct = await account(env, "user")
        iid = await env.instrument("HELD")
        other = await env.instrument("NOTHELD")
        await hold(env, acct, iid, "HELD", "10", "100", "100", NOW)
        await state(env, iid, "BEARISH_CONFIRMED", "95", None, NOW)
        await state(env, other, "BEARISH_CONFIRMED", "95", None, NOW)
        async with env.maker() as db:
            assert await portfolio_map.holders(db) == {iid: {env.ids["user"]}}
        result = RunResult(
            "done",
            changes=[
                (iid, "15m", "BULLISH_TREND", "BEARISH_CONFIRMED"),  # reversal
                (other, "15m", "BULLISH_TREND", "BEARISH_CONFIRMED"),  # not held
                (iid, "15m", "MIXED", "BEARISH_CONFIRMED"),  # not a flip
            ],
        )
        run = None
        assert await portfolio_map.notify_holdings(env.maker, result, run) == 1
        assert await portfolio_map.notify_holdings(env.maker, result, run) == 0
        async with env.maker() as db:
            rows = (await db.scalars(select(StockSignalNotification))).all()
            st = await db.get(StockSignalState, (iid, "15m"))
            st2 = await db.get(StockSignalState, (other, "15m"))
        assert [(n.kind, n.user_id) for n in rows] == [("holding_reversal", env.ids["user"])]
        assert rows[0].payload["previous_state"] == "BULLISH_TREND"
        assert st is not None and st.in_holdings and st2 is not None and not st2.in_holdings
