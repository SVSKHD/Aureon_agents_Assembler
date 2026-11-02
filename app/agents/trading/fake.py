"""Deterministic in-memory adapter for tests and the adapter contract suite.

It is never registered in production (app.state.trading_adapters starts empty).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

from app.agents.trading.base import BrokerAdapter, BrokerError
from app.agents.trading.types import (
    AccountInfo,
    AdapterHealth,
    BrokerOrder,
    Candle,
    Funds,
    InstrumentInfo,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
    Tick,
    Timeframe,
)

STEP = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
}
T0 = datetime(2026, 11, 2, 3, 45, tzinfo=UTC)


class FakeBrokerAdapter(BrokerAdapter):
    broker: ClassVar[str] = "dhan"

    def __init__(
        self,
        account_id: str = "FAKE-001",
        *,
        healthy: bool = True,
        fail: bool = False,
        broker: str | None = None,
    ) -> None:
        self.account_id = account_id
        self.healthy = healthy
        self.fail = fail
        if broker:
            self.broker = broker  # type: ignore[misc]

    def _maybe_fail(self) -> None:
        if self.fail:
            raise BrokerError("simulated broker outage")

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            broker=self.broker,
            ok=self.healthy,
            checked_at=T0,
            detail=None if self.healthy else "token expired",
            credentials_expire_at=T0 + timedelta(hours=20),
        )

    async def accounts(self) -> list[AccountInfo]:
        self._maybe_fail()
        return [AccountInfo(self.broker, self.account_id, "Fake account", "INR")]

    async def funds(self, account_id: str) -> Funds:
        self._maybe_fail()
        return Funds(
            account_id=account_id,
            currency="INR",
            available=Decimal("100000.0000"),
            used_margin=Decimal("2500.5050"),
            equity=Decimal("102345.6789"),
            as_of=T0,
        )

    async def positions(self, account_id: str) -> list[Position]:
        self._maybe_fail()
        return [
            Position(
                account_id,
                "NIFTY 25NOV 24500 CE",
                "52175",
                Decimal("75"),
                Decimal("123.45"),
                "INR",
                last_price=Decimal("130.05"),
                unrealized_pnl=Decimal("495.00"),
                product="INTRADAY",
            ),
            Position(account_id, "RELIANCE", "2885", Decimal("-10"), Decimal("2890.10"), "INR"),
        ]

    async def orders(self, account_id: str) -> list[BrokerOrder]:
        self._maybe_fail()
        return [
            BrokerOrder(
                account_id,
                "ORD-1",
                "RELIANCE",
                Side.SELL,
                OrderType.LIMIT,
                Decimal("10"),
                Decimal("10"),
                OrderStatus.FILLED,
                "TRADED",
                T0,
                price=Decimal("2890.10"),
                avg_fill_price=Decimal("2890.10"),
                correlation_id="corr-1",
            )
        ]

    async def instruments(self) -> list[InstrumentInfo]:
        self._maybe_fail()
        return [
            InstrumentInfo(
                self.broker,
                "2885",
                "RELIANCE",
                "EQUITY",
                "INR",
                Decimal("1"),
                Decimal("0.05"),
                exchange="NSE",
                segment="NSE_EQ",
            )
        ]

    async def quotes(self, symbols: Sequence[str]) -> list[Quote]:
        self._maybe_fail()
        return [
            Quote(s, T0, last=Decimal("2890.10"), bid=Decimal("2890.05"), ask=Decimal("2890.15"))
            for s in symbols
        ]

    async def candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        self._maybe_fail()
        step = timedelta(seconds=STEP[timeframe])
        out: list[Candle] = []
        ts, price = start, Decimal("2890.00")
        while ts < end and len(out) < 500:
            out.append(
                Candle(
                    ts,
                    price,
                    price + Decimal("1.5"),
                    price - Decimal("0.75"),
                    price + Decimal("0.25"),
                    Decimal("1000"),
                )
            )
            ts, price = ts + step, price + Decimal("0.25")
        return out

    async def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        self._maybe_fail()
        n = 0
        while True:
            for s in symbols:
                yield Tick(
                    s, T0 + timedelta(seconds=n), Decimal("2890.10") + n, quantity=Decimal("1")
                )
            n += 1
            await asyncio.sleep(0)


class NullAdapter(BrokerAdapter):
    """Supports nothing but health; exercises the NotSupported paths."""

    broker: ClassVar[str] = "mt5"

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            broker=self.broker,
            ok=False,
            checked_at=datetime.now(UTC),
            detail="no MT5 bridge configured",
        )
