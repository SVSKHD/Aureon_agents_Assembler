"""DhanAdapter: the read-only BrokerAdapter for DhanHQ v2.

- **Instrument refs** are "SEGMENT:SECURITY_ID" (e.g. "NSE_EQ:2885", "IDX_I:13"). That is also
  `trading.instruments.broker_instrument_id` for Dhan.
- **Live ticks** are served by the separate dhan-feed worker, never from the API process, so
  `subscribe_ticks` raises NotSupported here.
- **Execution** is not possible through this class.
"""

from __future__ import annotations

import csv
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, ClassVar, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.trading.base import BrokerAdapter, BrokerError, CredentialsExpired, NotSupported
from app.agents.trading.dhan import parse
from app.agents.trading.dhan.client import DhanClient
from app.agents.trading.dhan.parse import IST, DhanRef
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
from app.models.brokers import DhanInstrument

INTRADAY_INTERVAL = {Timeframe.M1: 1, Timeframe.M5: 5, Timeframe.M15: 15, Timeframe.H1: 60}
INTRADAY_MAX_DAYS = 90  # documented: 90 days per intraday request
DAILY_CHUNK_DAYS = 365  # daily max range is undocumented; stay conservative
QUOTE_MAX_INSTRUMENTS = 1000  # documented per-request maximum
DERIVATIVES = ("FUT", "OPT")


class InstrumentResolver(Protocol):
    async def resolve(self, ref: str) -> DhanRef | None: ...


class DbInstrumentResolver:
    """Looks refs up in dhan.instruments (filled by the instrument refresh job)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self.maker = maker
        self._cache: dict[str, DhanRef] = {}

    async def resolve(self, ref: str) -> DhanRef | None:
        if ref in self._cache:
            return self._cache[ref]
        seg, sid = parse.split_ref(ref)
        async with self.maker() as db:
            inst = await db.scalar(
                select(DhanInstrument.instrument).where(
                    DhanInstrument.exchange_segment == seg, DhanInstrument.security_id == sid
                )
            )
        if inst is None:
            return None
        found = DhanRef(seg, sid, inst)
        self._cache[ref] = found
        return found


class StaticResolver:
    def __init__(self, refs: dict[str, str]) -> None:  # {"NSE_EQ:2885": "EQUITY"}
        self.refs = refs

    async def resolve(self, ref: str) -> DhanRef | None:
        seg, sid = parse.split_ref(ref)
        inst = self.refs.get(ref)
        return DhanRef(seg, sid, inst) if inst else None


class DhanAdapter(BrokerAdapter):
    broker: ClassVar[str] = "dhan"

    def __init__(
        self,
        client: DhanClient,
        resolver: InstrumentResolver,
        *,
        instrument_csv_url: str,
        token_warn_hours: float = 4.0,
        csv_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client = client
        self.resolver = resolver
        self.csv_url = instrument_csv_url
        self.token_warn = timedelta(hours=token_warn_hours)
        self._csv_transport = csv_transport

    async def aclose(self) -> None:
        await self.client.aclose()

    def _check_account(self, account_id: str) -> None:
        if account_id != self.client.client_id:
            raise NotSupported(
                self.broker,
                "account",
                "the configured Dhan credentials belong to a different client id",
            )

    # --- health / account ----------------------------------------------------------------

    async def health(self) -> AdapterHealth:
        now = datetime.now(UTC)
        try:
            profile = await self.client.profile()
        except CredentialsExpired:
            return AdapterHealth(
                self.broker,
                False,
                now,
                "access token expired or invalid; generate a new one on "
                "web.dhan.co and update DHAN_ACCESS_TOKEN",
            )
        except BrokerError as exc:
            return AdapterHealth(self.broker, False, now, str(exc))
        expires = parse.token_expiry(profile)
        notes: list[str] = []
        ok = True
        if expires is not None and expires <= now:
            ok = False
            notes.append("access token has expired")
        elif expires is not None and expires - now < self.token_warn:
            hours = (expires - now).total_seconds() / 3600
            notes.append(f"access token expires in {hours:.1f} h; renew it on web.dhan.co")
        if str(profile.get("dataPlan", "")).lower() != "active":
            notes.append("Dhan Data APIs plan is not active (quotes/charts/feed will fail)")
        return AdapterHealth(self.broker, ok, now, "; ".join(notes) or None, expires)

    async def accounts(self) -> list[AccountInfo]:
        return [parse.account(await self.client.profile())]

    async def funds(self, account_id: str) -> Funds:
        self._check_account(account_id)
        return parse.funds(await self.client.fund_limit(), account_id, datetime.now(UTC))

    async def positions(self, account_id: str) -> list[Position]:
        self._check_account(account_id)
        return parse.positions(await self.client.positions(), account_id)

    async def holdings(self, account_id: str) -> list[Holding]:
        self._check_account(account_id)
        return parse.holdings(await self.client.holdings(), account_id)

    async def orders(self, account_id: str) -> list[BrokerOrder]:
        self._check_account(account_id)
        return [parse.order(o, account_id) for o in await self.client.orders()]

    async def trades(self, account_id: str) -> list[BrokerTrade]:
        self._check_account(account_id)
        return [parse.trade(t, account_id) for t in await self.client.trades()]

    # --- market data ---------------------------------------------------------------------

    async def quotes(self, symbols: Sequence[str]) -> list[Quote]:
        refs = [parse.split_ref(s) for s in dict.fromkeys(symbols)]
        out: list[Quote] = []
        for i in range(0, len(refs), QUOTE_MAX_INSTRUMENTS):
            body: dict[str, list[int]] = {}
            for seg, sid in refs[i : i + QUOTE_MAX_INSTRUMENTS]:
                body.setdefault(seg, []).append(int(sid))
            out += parse.quotes(await self.client.market_quote(body), datetime.now(UTC))
        wanted = set(symbols)
        return [q for q in out if q.symbol in wanted]

    async def _ref(self, symbol: str) -> DhanRef:
        ref = await self.resolver.resolve(symbol)
        if ref is None:
            raise BrokerError(f"unknown Dhan instrument {symbol!r}; run the instrument refresh")
        return ref

    async def candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start/end must be timezone-aware")
        if timeframe is not Timeframe.D1 and timeframe not in INTRADAY_INTERVAL:
            raise NotSupported(
                self.broker, "candles", f"Dhan has no {timeframe.value} interval (1m 5m 15m 1h 1d)"
            )
        ref = await self._ref(symbol)
        base: dict[str, Any] = {
            "securityId": ref.security_id,
            "exchangeSegment": ref.segment,
            "instrument": ref.instrument,
        }
        if ref.instrument.startswith(DERIVATIVES):
            base["oi"] = True
        got: dict[datetime, Candle] = {}
        if timeframe is Timeframe.D1:
            d0 = start.astimezone(IST).date()
            d_end = end.astimezone(IST).date() + timedelta(days=1)  # toDate is exclusive
            if ref.instrument.startswith(DERIVATIVES):
                base["expiryCode"] = 0
            while d0 < d_end:
                d1 = min(d0 + timedelta(days=DAILY_CHUNK_DAYS), d_end)
                payload = await self.client.historical_daily(
                    {**base, "fromDate": d0.isoformat(), "toDate": d1.isoformat()}
                )
                for c in parse.candles(payload):
                    got[c.ts] = c
                d0 = d1
        else:
            t0 = start.astimezone(IST)
            t_end = end.astimezone(IST)
            while t0 < t_end:
                t1 = min(t0 + timedelta(days=INTRADAY_MAX_DAYS), t_end)
                payload = await self.client.intraday(
                    {
                        **base,
                        "interval": INTRADAY_INTERVAL[timeframe],
                        "fromDate": t0.strftime("%Y-%m-%d %H:%M:%S"),
                        "toDate": t1.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
                for c in parse.candles(payload):
                    got[c.ts] = c
                t0 = t1
        return [got[k] for k in sorted(got) if start <= k < end]

    async def option_expiries(self, underlying: str) -> list[date]:
        seg, sid = parse.split_ref(underlying)
        payload = await self.client.expiry_list(int(sid), seg)
        return sorted(date.fromisoformat(d) for d in payload.get("data") or [])

    async def option_chain(self, underlying: str, expiry: date) -> OptionChain:
        seg, sid = parse.split_ref(underlying)
        payload = await self.client.option_chain(int(sid), seg, expiry.isoformat())
        legs_segment = await self._legs_segment(seg, payload)
        return parse.option_chain(payload, underlying, expiry, legs_segment, datetime.now(UTC))

    async def _legs_segment(self, underlying_seg: str, payload: dict[str, Any]) -> str | None:
        direct = {"NSE_EQ": "NSE_FNO", "BSE_EQ": "BSE_FNO", "MCX_COMM": "MCX_COMM"}
        if underlying_seg in direct:
            return direct[underlying_seg]
        # Index options trade on NSE (NIFTY…) or BSE (SENSEX…): check one leg id in the master.
        for strike in (payload.get("data") or {}).get("oc", {}).values():
            for leg in (strike.get("ce"), strike.get("pe")):
                if leg and leg.get("security_id") is not None:
                    for seg in ("NSE_FNO", "BSE_FNO"):
                        if await self.resolver.resolve(f"{seg}:{leg['security_id']}"):
                            return seg
                    return None
        return None

    async def instruments(self) -> list[InstrumentInfo]:
        return [info async for info, _ in self.iter_master()]

    async def iter_master(self) -> AsyncIterator[tuple[InstrumentInfo, dict[str, Any]]]:
        """Stream Dhan's instrument master CSV (public file, no credentials)."""
        async with (
            httpx.AsyncClient(timeout=120, transport=self._csv_transport) as http,
            http.stream("GET", self.csv_url) as resp,
        ):
            if resp.status_code >= 400:
                raise BrokerError(f"instrument master download failed: HTTP {resp.status_code}")
            header: list[str] | None = None
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                cells = next(csv.reader([line]))
                if header is None:
                    header = [h.lstrip("﻿").strip() for h in cells]
                    continue
                parsed = parse.instrument_row(dict(zip(header, cells, strict=False)))
                if parsed is not None:
                    yield parsed

    def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        raise NotSupported(
            self.broker,
            "subscribe_ticks",
            "live ticks come from the dhan-feed worker (app/workers/dhan_feed.py)",
        )


def market_day_bounds(day: date) -> tuple[datetime, datetime]:
    """NSE cash session 09:15-15:30 IST for `day`, as UTC datetimes."""
    open_ = datetime.combine(day, time(9, 15), IST).astimezone(UTC)
    close = datetime.combine(day, time(15, 30), IST).astimezone(UTC)
    return open_, close
