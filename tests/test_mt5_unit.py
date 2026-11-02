"""MT5Adapter over the bot's status export (fixture files; no MT5 terminal, no DB)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.agents.trading.base import BrokerError, NotSupported
from app.agents.trading.mt5 import build_mt5_adapter
from app.agents.trading.mt5.adapter import MT5Adapter
from app.agents.trading.mt5.status import StatusInvalid, StatusSource, parse_status
from app.agents.trading.types import Timeframe
from app.core.config import Settings
from app.services.mt5_sync import event_key, performance
from app.workers.scheduler import default_jobs
from tests.trading_contract import check_adapter

FIX = Path(__file__).parent / "fixtures" / "mt5" / "status.json"
LOGIN = "51234567"


def doc(**changes: Any) -> dict[str, Any]:
    d: dict[str, Any] = json.loads(FIX.read_text())
    d["generated_at"] = datetime.now(UTC).isoformat()
    d.update(changes)
    return d


def write(tmp_path: Path, d: dict[str, Any]) -> Path:
    p = tmp_path / "status.json"
    p.write_text(json.dumps(d))
    return p


def adapter_for(path: Path, stale: float = 90) -> MT5Adapter:
    return MT5Adapter(
        StatusSource(path=str(path)), account_id=LOGIN, currency="USD", stale_after=stale
    )


def test_parse_keeps_decimals_and_rejects_other_schemas() -> None:
    st = parse_status(FIX.read_bytes())
    pos = st.agents["XAUUSD"].positions[0]
    assert pos.price_open == Decimal("2650.55") and isinstance(pos.volume, Decimal)
    assert st.timeframe("XAUUSD") is Timeframe.M5
    # Bar times are broker-server clock; offset_h 2 → UTC is 2 h earlier.
    assert st.server_to_utc(1793620500, "XAUUSD") == datetime.fromtimestamp(1793620500 - 7200, UTC)
    with pytest.raises(StatusInvalid, match=r"not aureon\.mt5\.status/v1"):
        parse_status(json.dumps({**doc(), "schema": "other/v9"}).encode())
    with pytest.raises(StatusInvalid, match="not valid JSON"):
        parse_status(b"{nope")
    bad = doc()
    del bad["bot"]
    with pytest.raises(StatusInvalid, match="bot"):
        parse_status(json.dumps(bad).encode())


async def test_positions_are_signed_lots(tmp_path: Path) -> None:
    a = adapter_for(write(tmp_path, doc()))
    pos = {p.symbol: p for p in await a.positions(LOGIN)}
    assert pos["XAUUSD"].quantity == Decimal("0.10") and pos["XAGUSD"].quantity == Decimal("-0.5")
    assert pos["XAUUSD"].last_price == Decimal("2655.25") and pos["XAUUSD"].unrealized_pnl is None
    assert pos["XAUUSD"].raw["ticket"] == 101
    with pytest.raises(ValueError, match="different account"):
        await a.positions("999")


async def test_contract_and_not_supported(tmp_path: Path) -> None:
    a = adapter_for(write(tmp_path, doc()))
    supported = await check_adapter(a, LOGIN, "XAUUSD")
    assert supported["accounts"] and supported["positions"]
    for op in ("funds", "orders", "instruments", "quotes", "candles"):
        assert supported[op] is False, op
    with pytest.raises(NotSupported):
        await a.trades(LOGIN)


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        ({"generated_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()}, "stale"),
        ({"bot": {**doc()["bot"], "dry": True}}, "--dry"),
        ({"agents": {}}, "no symbol agents"),
    ],
)
async def test_health_reasons(tmp_path: Path, changes: dict[str, Any], detail: str) -> None:
    d = doc()
    d.update(changes)
    h = await adapter_for(write(tmp_path, d)).health()
    assert not h.ok and detail in (h.detail or "")


async def test_health_disconnected_and_halt(tmp_path: Path) -> None:
    d = doc()
    d["agents"]["XAGUSD"]["mt5"] = "DISCONNECTED"
    h = await adapter_for(write(tmp_path, d)).health()
    assert not h.ok and h.detail == "MT5 not connected for XAGUSD"
    h = await adapter_for(write(tmp_path, doc(halt={"on": True, "reason": "news"}))).health()
    assert h.ok and h.detail == "bot halted: news"


async def test_missing_file_is_unhealthy_not_an_exception(tmp_path: Path) -> None:
    a = adapter_for(tmp_path / "absent.json")
    h = await a.health()
    assert not h.ok and "not found" in (h.detail or "")
    with pytest.raises(BrokerError):
        await a.positions(LOGIN)


async def test_stale_positions_are_refused(tmp_path: Path) -> None:
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    with pytest.raises(BrokerError, match="stale"):
        await adapter_for(write(tmp_path, doc(generated_at=old))).positions(LOGIN)


@respx.mock
async def test_http_source_sends_token_and_maps_errors() -> None:
    route = respx.get("http://bot.test:8788/status").mock(
        return_value=httpx.Response(200, json=doc())
    )
    src = StatusSource(url="http://bot.test:8788/status", token="s" * 40)
    st = await src.read()
    assert st.bot.mode == "ema5080"
    assert route.calls[0].request.headers["authorization"] == "Bearer " + "s" * 40
    route.mock(return_value=httpx.Response(401))
    with pytest.raises(BrokerError, match="HTTP 401"):
        await src.read()
    route.mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(BrokerError, match="unreachable"):
        await src.read()
    await src.aclose()


def test_build_and_jobs_only_when_configured(settings: Settings) -> None:
    assert build_mt5_adapter(settings) is None
    assert "mt5-sync-status" not in [j[0] for j in default_jobs(settings)]
    s = Settings.model_validate(
        {**settings.model_dump(), "MT5_STATUS_URL": "http://bot.test:8788/status"}
    )
    a = build_mt5_adapter(s)
    assert a is not None and a.source.kind == "http"
    assert ("mt5-sync-status", "mt5_sync_status", "* * * * *") in default_jobs(s)


def test_performance_is_calculated_from_exits() -> None:
    perf = performance(parse_status(FIX.read_bytes()))
    x = perf["exits_by_symbol"]["XAUUSD"]
    assert (x["exits"], x["wins"], x["losses"], x["net_points"]) == (2, 1, 1, "8.5")
    assert perf["source"] == "calculated" and perf["ema921e"] == {"trades": 2, "net_usd": "9.2"}


def test_event_key_is_stable_across_float_text() -> None:
    a = json.loads('{"t": 1, "price": 2650.10}', parse_float=Decimal)
    b = json.loads('{"t": 1, "price": 2650.1}', parse_float=Decimal)
    assert event_key(a) == event_key(b)
