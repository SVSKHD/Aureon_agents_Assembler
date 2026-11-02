"""Paper accounting fixtures: fill prices, Indian charges, MT5 lots, average-cost P&L."""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from app.agents.trading.paper import accounting as acc

NIFTY_OPT = acc.Spec("dhan", "options", D(75), D(1), "INR", D("0.05"))
EQUITY = acc.Spec("dhan", "equity", D(1), D(1), "INR", D("0.05"))
XAUUSD = acc.Spec("mt5", "cfd", D("0.01"), D(100), "USD", D("0.01"), digits=2)
EURUSD = acc.Spec("mt5", "cfd", D("0.01"), D(100000), "USD", D("0.00001"), digits=5)


def test_quantity_must_be_a_lot_multiple() -> None:
    acc.check_quantity(NIFTY_OPT, D(150))
    acc.check_quantity(XAUUSD, D("0.03"))
    with pytest.raises(acc.AccountingError, match="multiple of 75 units"):
        acc.check_quantity(NIFTY_OPT, D(100))
    with pytest.raises(acc.AccountingError, match=r"multiple of 0\.01 lots"):
        acc.check_quantity(XAUUSD, D("0.015"))
    with pytest.raises(acc.AccountingError, match="positive"):
        acc.check_quantity(EQUITY, D(0))


def test_kinds() -> None:
    assert acc.dhan_kind("NSE_EQ", "EQUITY") == "equity"
    assert acc.dhan_kind("NSE_FNO", "OPTIDX") == "options"
    assert acc.dhan_kind("NSE_FNO", "FUTSTK") == "futures"
    for seg, t in (("IDX_I", "INDEX"), ("MCX_COMM", "FUTCOM"), ("NSE_CURRENCY", "FUTCUR")):
        with pytest.raises(acc.AccountingError):
            acc.dhan_kind(seg, t)


def test_fill_price_is_adverse_and_on_tick() -> None:
    # 1000 ± (2/2 + 1) bps = ±0.2 → BUY 1000.20, SELL 999.80 (on the 0.05 tick)
    assert acc.fill_price(D(1000), "BUY", D(2), D(1), D("0.05")) == D("1000.20")
    assert acc.fill_price(D(1000), "SELL", D(2), D(1), D("0.05")) == D("999.80")
    # Off-tick values round against the trader.
    assert acc.fill_price(D("101.01"), "BUY", D(0), D(0), D("0.05")) == D("101.05")
    assert acc.fill_price(D("101.04"), "SELL", D(0), D(0), D("0.05")) == D("101.00")


def test_dhan_option_charges_fixture() -> None:
    # Sell 75 NIFTY CE at 100: turnover 7,500
    c = acc.dhan_charges("options", "INTRADAY", "SELL", D(7500), acc.DEFAULT_DHAN_CHARGES)
    assert c.brokerage == D("2.25")  # min(20, 0.03% of 7500 = 2.25)
    assert c.stt == D("7.50")  # 0.1% of premium on sell
    assert c.exchange == D("2.63")  # 0.03503% → 2.627
    assert c.sebi == D("0.01")
    assert c.stamp == D("0")  # buyer only
    assert c.gst == D("0.88")  # 18% of (2.25 + 2.627 + 0.0075)
    assert c.total == D("13.27")


def test_dhan_equity_delivery_and_intraday() -> None:
    buy = acc.dhan_charges("equity", "CNC", "BUY", D(100000), acc.DEFAULT_DHAN_CHARGES)
    assert buy.brokerage == 0 and buy.stt == D("100.00") and buy.stamp == D("15.00")
    intraday = acc.dhan_charges("equity", "INTRADAY", "BUY", D(100000), acc.DEFAULT_DHAN_CHARGES)
    assert intraday.brokerage == D("20") and intraday.stt == 0 and intraday.stamp == D("3.00")


def test_mt5_commission_and_pip_value() -> None:
    assert acc.mt5_charges(D("0.50"), D(7)).total == D("3.50")
    assert acc.pip_value(EURUSD) == D("10.00000")  # 0.0001 x 100,000
    assert acc.pip_value(XAUUSD, D("0.10")) == D("0.100")  # 0.01 x 100 x 0.1
    assert acc.pip_value(EQUITY) is None


def test_average_cost_realized_and_flip() -> None:
    book = acc.Book()
    f = lambda side, q, p: acc.Fill(1, side, D(q), D(p), D(0), D(1))  # noqa: E731
    assert book.apply(f("BUY", 10, 100)) == 0
    assert book.apply(f("BUY", 10, 110)) == 0
    pos = book.positions[1]
    assert (pos.quantity, pos.avg_price) == (D(20), D(105))
    assert book.apply(f("SELL", 5, 120)) == D("75.00")  # (120-105)*5
    assert book.apply(f("SELL", 25, 100)) == D("-75.00")  # closes 15 at -5, flips short 10
    assert (pos.quantity, pos.avg_price) == (D(-10), D(100))
    assert pos.unrealized(D(90)) == D("100.00")  # short 10 from 100 to 90
    assert book.apply(f("BUY", 10, 95)) == D("50.00")
    assert pos.quantity == 0 and book.realized == D("50.00")


def test_mt5_pnl_uses_contract_size() -> None:
    book = acc.Book()
    book.apply(acc.Fill(7, "BUY", D("0.10"), D("2650.00"), D("0.70"), D(100)))
    r = book.apply(acc.Fill(7, "SELL", D("0.10"), D("2655.50"), D("0.70"), D(100)))
    assert r == D("55.00")  # 5.5 x 0.1 lot x 100 oz
    assert book.fees == D("1.40")


def test_replay_matches_incremental() -> None:
    fills = [
        acc.Fill(1, "BUY", D(75), D("100"), D("5"), D(1)),
        acc.Fill(2, "SELL", D(1), D("2000"), D("1"), D(1)),
        acc.Fill(1, "SELL", D(150), D("110"), D("8"), D(1)),
    ]
    book = acc.replay(fills)
    assert book.positions[1].quantity == D(-75) and book.realized == D("750.00")
    assert book.fees == D("14.00")
