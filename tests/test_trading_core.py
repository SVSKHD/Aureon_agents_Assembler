"""Trading types, session tags, Decimal enforcement, adapter contract (no DB)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.agents.trading.base import BrokerError, NotSupported
from app.agents.trading.fake import FakeBrokerAdapter, NullAdapter
from app.agents.trading.types import (
    Candle,
    Funds,
    Position,
    Session,
    Timeframe,
    session_for,
)
from tests.trading_contract import check_adapter

T = datetime(2026, 11, 2, tzinfo=UTC)


@pytest.mark.parametrize(
    ("hhmm", "expected"),
    [
        ("00:00", Session.ASIA),
        ("06:59", Session.ASIA),
        ("07:00", Session.LONDON),
        ("12:59", Session.LONDON),
        ("13:00", Session.NY),
        ("21:59", Session.NY),
        ("22:00", Session.ASIA),
        ("23:59", Session.ASIA),
    ],
)
def test_session_partition_covers_the_day(hhmm: str, expected: Session) -> None:
    h, m = map(int, hhmm.split(":"))
    assert session_for(T.replace(hour=h, minute=m)) is expected


def test_session_uses_utc_not_local_time() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    # 12:30 IST = 07:00 UTC → London
    assert session_for(datetime(2026, 11, 2, 12, 30, tzinfo=ist)) is Session.LONDON
    with pytest.raises(ValueError, match="timezone-aware"):
        session_for(datetime(2026, 11, 2, 12, 30))


def test_dtos_reject_floats_and_naive_datetimes() -> None:
    with pytest.raises(TypeError, match="Decimal, not float"):
        Funds(
            "A",
            "INR",
            available=100.1,  # type: ignore[arg-type]
            used_margin=Decimal(0),
            equity=None,
            as_of=T,
        )
    with pytest.raises(TypeError):
        Position("A", "X", None, Decimal("1"), 2890.1, "INR")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        Funds("A", "INR", Decimal(1), Decimal(0), None, as_of=datetime(2026, 1, 1))


def test_decimal_precision_is_kept_exactly() -> None:
    c = Candle(
        T,
        Decimal("0.00000001"),
        Decimal("123456789.12345678"),
        Decimal("0.00000001"),
        Decimal("0.1") + Decimal("0.2"),
        Decimal("1"),
    )
    assert c.close == Decimal("0.3")  # 0.1 + 0.2 is exactly 0.3 with Decimal
    assert c.session is Session.ASIA


async def test_base_adapter_raises_not_supported_by_default() -> None:
    a = NullAdapter()
    for coro in (
        a.accounts(),
        a.funds("x"),
        a.positions("x"),
        a.orders("x"),
        a.instruments(),
        a.quotes(["x"]),
        a.candles("x", Timeframe.M1, T, T + timedelta(minutes=5)),
    ):
        with pytest.raises(NotSupported) as exc:
            await coro
        assert exc.value.broker == "mt5"
    with pytest.raises(NotSupported):
        a.subscribe_ticks(["x"])
    assert (await a.health()).ok is False


async def test_contract_fake_adapter_supports_everything() -> None:
    supported = await check_adapter(FakeBrokerAdapter(), "FAKE-001", "RELIANCE")
    assert all(supported.values()), supported


async def test_contract_null_adapter_supports_nothing_cleanly() -> None:
    supported = await check_adapter(NullAdapter(), "1", "XAUUSD")
    assert not any(supported.values()), supported


async def test_broker_errors_are_distinct_from_not_supported() -> None:
    with pytest.raises(BrokerError):
        await FakeBrokerAdapter(fail=True).funds("x")
