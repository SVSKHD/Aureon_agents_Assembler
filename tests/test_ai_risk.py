"""Hard limits on propose_trade (app/ai/risk.py): Python, not the prompt."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.ai import risk
from app.core.config import Settings

OK: dict[str, Any] = {
    "symbol": "reliance",
    "side": "BUY",
    "qty": 10,
    "entry": 2850,
    "stop": 2800,
    "target": 2950,
    "reason": "breakout",
}


def test_valid_proposal_passes(settings: Settings) -> None:
    p = risk.parse(OK)
    assert (p.symbol, p.side, p.quantity) == ("RELIANCE", "BUY", Decimal(10))
    assert p.risk_inr == Decimal(500) and p.notional_inr == Decimal(28500)
    risk.check(settings, p, proposals_today=0)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"side": "HOLD"}, "BUY or SELL"),
        ({"qty": 0}, "positive whole"),
        ({"qty": 1.5}, "positive whole"),
        ({"stop": 2900}, "stop < entry < target"),
        ({"target": 2800}, "stop < entry < target"),
        ({"side": "SELL"}, "target < entry < stop"),
        ({"entry": -1}, "positive prices"),
        ({"entry": "abc"}, "not a number"),
        ({"symbol": " "}, "symbol is required"),
    ],
)
def test_malformed_proposals_are_rejected(change: dict[str, Any], message: str) -> None:
    with pytest.raises(risk.RiskLimitBreached, match=message):
        risk.parse({**OK, **change})


def test_max_loss_per_trade(settings: Settings) -> None:
    # 100 shares x 50 INR to the stop = 5,000 INR > the 2,000 INR default.
    p = risk.parse({**OK, "qty": 100})
    with pytest.raises(risk.RiskLimitBreached, match="max loss per trade"):
        risk.check(settings, p, 0)


def test_max_order_size(settings: Settings) -> None:
    # A tight stop keeps the loss small, but 40 x 2,850 = 114,000 INR > 100,000.
    p = risk.parse({**OK, "qty": 40, "stop": 2849})
    with pytest.raises(risk.RiskLimitBreached, match="max order size"):
        risk.check(settings, p, 0)


def test_max_trades_per_day(settings: Settings) -> None:
    p = risk.parse(OK)
    risk.check(settings, p, settings.AI_TRADE_MAX_PER_DAY - 1)
    with pytest.raises(risk.RiskLimitBreached, match="max trades per day"):
        risk.check(settings, p, settings.AI_TRADE_MAX_PER_DAY)


def test_limits_come_from_settings(settings: Settings) -> None:
    s = settings.model_copy(update={"AI_TRADE_MAX_LOSS_INR": Decimal("10000")})
    risk.check(s, risk.parse({**OK, "qty": 30}), 0)  # 1,500 risk, 85,500 size
