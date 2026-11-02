"""Reusable BrokerAdapter contract. Phase B (Dhan) and C (MT5) run their adapters through
`check_adapter` with recorded fixtures; Phase A runs it against the fake and null adapters.

Each method must either return correctly-typed data (Decimals, tz-aware UTC timestamps) or
raise NotSupported. Returning None, floats, or invented placeholders fails the contract.
"""

from __future__ import annotations

import contextlib
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.agents.trading.base import BrokerAdapter, NotSupported
from app.agents.trading.types import (
    AccountInfo,
    AdapterHealth,
    BrokerOrder,
    Candle,
    Funds,
    InstrumentInfo,
    Position,
    Quote,
    Tick,
    Timeframe,
)


def _assert_clean(obj: Any) -> None:
    assert is_dataclass(obj), f"{obj!r} is not a DTO"
    for f in fields(obj):
        v = getattr(obj, f.name)
        assert not isinstance(v, float), f"{type(obj).__name__}.{f.name} is a float"
        if isinstance(v, datetime):
            assert v.utcoffset() == timedelta(0), f"{f.name} is not UTC"


async def _supported(coro: Any) -> Any:
    try:
        return await coro
    except NotSupported as exc:
        assert exc.operation and exc.broker
        return NotSupported


async def check_adapter(adapter: BrokerAdapter, account_id: str, symbol: str) -> dict[str, bool]:
    """Run the contract; returns {operation: supported?} for reporting."""
    supported: dict[str, bool] = {}

    health = await adapter.health()
    assert isinstance(health, AdapterHealth) and health.broker == adapter.broker
    _assert_clean(health)

    checks: list[tuple[str, Any, type]] = [
        ("accounts", adapter.accounts(), AccountInfo),
        ("funds", adapter.funds(account_id), Funds),
        ("positions", adapter.positions(account_id), Position),
        ("orders", adapter.orders(account_id), BrokerOrder),
        ("instruments", adapter.instruments(), InstrumentInfo),
        ("quotes", adapter.quotes([symbol]), Quote),
    ]
    for name, coro, dto in checks:
        out = await _supported(coro)
        supported[name] = out is not NotSupported
        if out is NotSupported:
            continue
        items = out if isinstance(out, list) else [out]
        for item in items:
            assert isinstance(item, dto), f"{name} returned {type(item).__name__}"
            _assert_clean(item)
        if name == "quotes":
            assert {q.symbol for q in items} <= {symbol}

    start = datetime(2026, 11, 2, 3, 45, tzinfo=UTC)
    end = start + timedelta(hours=2)
    out = await _supported(adapter.candles(symbol, Timeframe.M15, start, end))
    supported["candles"] = out is not NotSupported
    if out is not NotSupported:
        assert all(isinstance(c, Candle) for c in out)
        assert [c.ts for c in out] == sorted(c.ts for c in out), "candles not ascending"
        assert all(start <= c.ts < end for c in out), "candle outside requested range"
        assert all(c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close) for c in out)
        for c in out:
            _assert_clean(c)

    try:
        stream = adapter.subscribe_ticks([symbol])
    except NotSupported:
        supported["subscribe_ticks"] = False
    else:
        supported["subscribe_ticks"] = True
        tick = await anext(aiter(stream))
        assert isinstance(tick, Tick) and tick.symbol == symbol
        assert isinstance(tick.price, Decimal)
        with contextlib.suppress(AttributeError):
            await stream.aclose()  # type: ignore[attr-defined]
    return supported
