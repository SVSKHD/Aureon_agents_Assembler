"""MT5Adapter: a read-only BrokerAdapter over the Aureon MT5 bot's status export.

It never talks to MetaTrader 5 itself and never sends anything to the bot. It only exposes
what the bot already computes:

- health: export freshness + the bot's own MT5 link state
- accounts: the configured MT5 login (MT5_ACCOUNT_ID)
- positions: the bot's cached positions (signed lots, Decimal)

Funds, orders, trades, quotes, candles and instruments raise NotSupported: the bot does not
export them, and inventing them (or adding MT5 calls) is out of scope.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.trading.base import BrokerAdapter, BrokerError, NotSupported
from app.agents.trading.mt5.status import BotStatus, StatusSource
from app.agents.trading.types import AccountInfo, AdapterHealth, Position


class MT5Adapter(BrokerAdapter):
    broker = "mt5"

    def __init__(
        self,
        source: StatusSource,
        *,
        account_id: str | None,
        currency: str,
        stale_after: float,
    ) -> None:
        self.source = source
        self.account_id = account_id
        self.currency = currency
        self.stale_after = timedelta(seconds=stale_after)

    async def status(self) -> BotStatus:
        return await self.source.read()

    def assess(self, status: BotStatus, now: datetime | None = None) -> tuple[bool, str | None]:
        """(ok, why-not). ok = fresh export, not a dry run, and every agent connected to MT5."""
        now = now or datetime.now(UTC)
        age = status.age(now)
        if age > self.stale_after:
            return False, f"bot export is stale ({int(age.total_seconds())} s old)"
        if status.bot.dry:
            return False, "bot is running --dry (no MT5 terminal)"
        if not status.agents:
            return False, "bot reports no symbol agents"
        down = sorted(s for s, a in status.agents.items() if a.mt5 != "CONNECTED")
        if down:
            return False, "MT5 not connected for " + ", ".join(down)
        dead = sorted(s for s, a in status.agents.items() if a.alive is False)
        if dead:
            return False, "agent thread not alive for " + ", ".join(dead)
        return True, None

    async def health(self) -> AdapterHealth:
        now = datetime.now(UTC)
        try:
            status = await self.source.read()
        except BrokerError as exc:
            return AdapterHealth(broker=self.broker, ok=False, checked_at=now, detail=str(exc))
        ok, why = self.assess(status, now)
        detail = why
        if ok and status.halt:
            detail = f"bot halted: {status.halt.get('reason') or 'manual'}"
        return AdapterHealth(broker=self.broker, ok=ok, checked_at=now, detail=detail)

    async def accounts(self) -> list[AccountInfo]:
        if not self.account_id:
            raise NotSupported(self.broker, "accounts", "MT5_ACCOUNT_ID is not set")
        return [
            AccountInfo(
                broker=self.broker,
                account_id=self.account_id,
                label="Aureon MT5 bot",
                currency=self.currency,
            )
        ]

    def _check_account(self, account_id: str, operation: str) -> None:
        if not self.account_id:
            raise NotSupported(self.broker, operation, "MT5_ACCOUNT_ID is not set")
        if account_id != self.account_id:
            raise ValueError("this MT5 bot trades a different account")

    async def positions(self, account_id: str) -> list[Position]:
        self._check_account(account_id, "positions")
        status = await self.source.read()
        ok, why = self.assess(status)
        if not ok and status.age() > self.stale_after:
            raise BrokerError(why or "bot export is stale")
        return positions_from(status, account_id, self.currency)

    async def aclose(self) -> None:
        await self.source.aclose()


def positions_from(status: BotStatus, account_id: str, currency: str) -> list[Position]:
    out: list[Position] = []
    for sym, agent in sorted(status.agents.items()):
        for p in agent.positions:
            sign = 1 if p.direction.lower() == "long" else -1
            out.append(
                Position(
                    account_id=account_id,
                    symbol=p.symbol or sym,
                    broker_instrument_id=p.symbol or sym,
                    quantity=p.volume * sign,
                    avg_price=p.price_open,
                    currency=currency,
                    last_price=p.current,
                    # The bot exports price points, not account-currency P/L: left empty.
                    unrealized_pnl=None,
                    raw={
                        "ticket": p.ticket,
                        "sl": str(p.sl) if p.sl is not None else None,
                        "tp": str(p.tp) if p.tp is not None else None,
                        "points": str(p.points) if p.points is not None else None,
                        "server_time": p.time,
                        "strategy": agent.strategy,
                    },
                )
            )
    return out
