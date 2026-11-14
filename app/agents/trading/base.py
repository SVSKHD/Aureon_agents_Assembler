"""BrokerAdapter: the read-only interface every broker integration implements.

Rules for implementations (Dhan in Phase B, MT5 in Phase C):

- **Never fake data.** If the broker or account cannot provide something, raise
  NotSupported. The base class already does that for every method except `health()`.
- **Decimals only** for prices, quantities and money (the types reject floats), and
  timezone-aware UTC datetimes.
- **Read-only.** This interface has no order placement, modification or cancellation.
  Execution lives in a separate module behind the LIVE gate (Trading Phase G).
- Broker failures raise BrokerError (or a subclass) with a message that is safe to show:
  no tokens, no request bodies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime
from typing import ClassVar

from app.agents.trading.types import (
    AccountInfo,
    AdapterHealth,
    BrokerOrder,
    BrokerTrade,
    Candle,
    Funds,
    Holding,
    InstrumentInfo,
    OptionChain,
    Position,
    Quote,
    Tick,
    Timeframe,
)


class NotSupported(Exception):
    """The adapter (or broker) cannot provide this; never substitute made-up data."""

    def __init__(self, broker: str, operation: str, detail: str | None = None) -> None:
        super().__init__(
            f"{broker}: {operation} is not supported" + (f" ({detail})" if detail else "")
        )
        self.broker = broker
        self.operation = operation
        self.detail = detail


class BrokerError(Exception):
    """The broker call failed (network, auth, rate limit, bad response)."""


class CredentialsExpired(BrokerError):
    pass


class BrokerRejected(BrokerError):
    """The broker refused the request itself (bad parameters, or no data for it): sending
    the same request again cannot succeed, so it is never retried."""


class BrokerAdapter(ABC):
    broker: ClassVar[str]

    def _unsupported(self, operation: str) -> NotSupported:
        return NotSupported(self.broker, operation)

    @abstractmethod
    async def health(self) -> AdapterHealth:
        """Connectivity + credential status. Must not raise for a down broker: return
        ok=False with a safe `detail` instead."""

    async def accounts(self) -> list[AccountInfo]:
        """Accounts these credentials can see."""
        raise self._unsupported("accounts")

    async def funds(self, account_id: str) -> Funds:
        """Balance / margin for one account."""
        raise self._unsupported("funds")

    async def positions(self, account_id: str) -> list[Position]:
        """Open positions as the broker reports them."""
        raise self._unsupported("positions")

    async def orders(self, account_id: str) -> list[BrokerOrder]:
        """Today's / open orders as the broker reports them."""
        raise self._unsupported("orders")

    async def holdings(self, account_id: str) -> list[Holding]:
        """Delivery (demat) holdings."""
        raise self._unsupported("holdings")

    async def trades(self, account_id: str) -> list[BrokerTrade]:
        """Today's trade book (fills)."""
        raise self._unsupported("trades")

    async def option_expiries(self, underlying: str) -> list[date]:
        """Listed expiries for an option underlying."""
        raise self._unsupported("option_expiries")

    async def option_chain(self, underlying: str, expiry: date) -> OptionChain:
        """Full option chain (OI, IV, greeks when the broker provides them)."""
        raise self._unsupported("option_chain")

    async def instruments(self) -> list[InstrumentInfo]:
        """Full tradable instrument master."""
        raise self._unsupported("instruments")

    async def quotes(self, symbols: Sequence[str]) -> list[Quote]:
        """Latest quotes; only for the symbols asked for, in any order."""
        raise self._unsupported("quotes")

    async def candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        """Historical candles with start <= ts < end, ascending by ts."""
        raise self._unsupported("candles")

    def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        """Live ticks as an async iterator (implement as an `async def ... yield` generator).
        The caller cancels by closing the iterator."""
        raise self._unsupported("subscribe_ticks")
