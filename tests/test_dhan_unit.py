"""Dhan parsing, client, adapter and feed against recorded fixtures (no network, no DB)."""

from __future__ import annotations

import asyncio
import json
import struct
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.agents.trading.base import BrokerError, CredentialsExpired, NotSupported
from app.agents.trading.dhan import parse
from app.agents.trading.dhan.adapter import DhanAdapter, StaticResolver
from app.agents.trading.dhan.client import DhanApiError, DhanClient
from app.agents.trading.dhan.feed import (
    DhanFeed,
    parse_frame,
    parse_instruments,
    subscribe_messages,
)
from app.agents.trading.types import OrderStatus, OrderType, Side, Timeframe
from tests.trading_contract import check_adapter

FIX = Path(__file__).parent / "fixtures" / "dhan"
BASE = "https://api.dhan.test/v2"
CSV_URL = "https://images.dhan.test/api-scrip-master-detailed.csv"
CLIENT = "1100003626"
TOKEN = "eyJ-secret-token-value"


def fx(name: str) -> Any:
    return json.loads((FIX / name).read_text())


class CountingLimiter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def acquire(self, bucket: str, max_wait: float = 30.0) -> None:
        self.calls.append(bucket)


def make_adapter(refs: dict[str, str] | None = None) -> tuple[DhanAdapter, CountingLimiter]:
    lim = CountingLimiter()
    client = DhanClient(CLIENT, TOKEN, base_url=BASE, limiter=lim)  # type: ignore[arg-type]
    resolver = StaticResolver(
        refs or {"NSE_EQ:3499": "EQUITY", "NSE_FNO:52175": "OPTIDX", "NSE_FNO:42528": "OPTIDX"}
    )
    return DhanAdapter(client, resolver, instrument_csv_url=CSV_URL), lim


# --- parsing ------------------------------------------------------------------------------


def test_funds_uses_dhans_spelling_and_exact_decimals() -> None:
    f = parse.funds(fx("fundlimit.json"), CLIENT, datetime.now(UTC))
    assert f.available == Decimal("98440.0") and f.used_margin == Decimal("15202.0")
    assert f.currency == "INR" and f.raw["receiveableAmount"] == 0.0


def test_positions_signed_and_closed_skipped() -> None:
    pos = parse.positions(fx("positions.json"), CLIENT)
    assert [(p.symbol, p.quantity, p.avg_price) for p in pos] == [
        ("TATASTEEL", Decimal("40"), Decimal("142.85")),
        ("NIFTY-Nov2026-24500-CE", Decimal("-75"), Decimal("130.05")),
    ]
    assert pos[1].broker_instrument_id == "NSE_FNO:52175"
    assert pos[0].unrealized_pnl == Decimal("120.4")


def test_holdings() -> None:
    (h,) = parse.holdings(fx("holdings.json"), CLIENT)
    assert (h.symbol, h.isin, h.quantity, h.avg_cost, h.last_price) == (
        "HDFC",
        "INE001A01036",
        Decimal(1000),
        Decimal("2655.0"),
        Decimal("2701.35"),
    )


def test_orders_status_type_and_ist_times() -> None:
    o1, o2 = (parse.order(o, CLIENT) for o in fx("orders.json"))
    assert (o1.status, o1.raw_status, o1.side, o1.order_type) == (
        OrderStatus.OPEN,
        "PENDING",
        Side.BUY,
        OrderType.LIMIT,
    )
    # 13:33:03 IST = 08:03:03 UTC
    assert o1.placed_at == datetime(2026, 11, 2, 8, 3, 3, tzinfo=UTC)
    assert o1.correlation_id == "aureon-corr-1"
    assert (o2.status, o2.order_type, o2.filled_quantity, o2.avg_fill_price) == (
        OrderStatus.FILLED,
        OrderType.STOP_LIMIT,
        Decimal(75),
        Decimal("130.05"),
    )
    assert o2.correlation_id is None  # "" means none
    unknown = parse.order({**fx("orders.json")[0], "orderStatus": "CLOSED"}, CLIENT)
    assert unknown.status is OrderStatus.UNKNOWN  # never guessed


def test_trades() -> None:
    (t,) = (parse.trade(x, CLIENT) for x in fx("trades.json"))
    assert (t.trade_id, t.order_id, t.side, t.quantity, t.price) == (
        "15112111182199001",
        "112111182199",
        Side.SELL,
        Decimal(75),
        Decimal("130.05"),
    )
    assert t.executed_at == datetime(2026, 11, 2, 3, 51, 40, tzinfo=UTC)


def test_quotes_depth_and_no_trade_time() -> None:
    now = datetime(2026, 11, 2, 8, 6, tzinfo=UTC)
    q = {x.symbol: x for x in parse.quotes(fx("quote.json"), now)}
    assert q["NSE_EQ:3499"].bid == Decimal("142.8") and q["NSE_EQ:3499"].ask == Decimal("142.85")
    assert q["NSE_EQ:3499"].ts == datetime(2026, 11, 2, 8, 5, 1, tzinfo=UTC)
    assert q["NSE_FNO:52175"].ts == now  # "01/01/1980" = no trade yet → fetch time


def test_candles_are_utc_epoch() -> None:
    c = parse.candles(fx("daily.json"))
    # 1326220200 = 2012-01-10 18:30 UTC = 2012-01-11 00:00 IST (daily candles at IST midnight)
    assert c[0].ts == datetime(2012, 1, 10, 18, 30, tzinfo=UTC)
    assert c[0].close == Decimal("3879.85") and c[1].volume == Decimal(1906106)
    with pytest.raises(ValueError, match="different lengths"):
        parse.candles({"timestamp": [1], "open": [], "high": [], "low": [], "close": []})


def test_option_chain_parsing() -> None:
    ch = parse.option_chain(
        fx("optionchain.json"), "IDX_I:13", date(2026, 11, 26), "NSE_FNO", datetime.now(UTC)
    )
    assert [s.strike for s in ch.strikes] == [Decimal("25600.000000"), Decimal("25650.000000")]
    s = ch.strikes[1]
    assert s.call and s.put
    assert s.call.delta == Decimal("0.53871") and s.call.oi == Decimal(3786445)
    assert s.call.broker_instrument_id == "NSE_FNO:42528"
    assert ch.strikes[0].put is None and ch.underlying_price == Decimal("25642.8")


def test_instrument_master_detailed_and_compact() -> None:
    import csv

    def rows(name: str) -> list[Any]:
        with (FIX / name).open(encoding="utf-8") as f:
            return [r for r in (parse.instrument_row(x) for x in csv.DictReader(f)) if r]

    detailed = rows("master_detailed.csv")
    infos = {i.broker_instrument_id: i for i, _ in detailed}
    assert set(infos) == {"NSE_EQ:3499", "NSE_FNO:52175", "IDX_I:13"}  # NSE/M skipped
    opt = infos["NSE_FNO:52175"]
    assert (
        opt.instrument_type,
        opt.lot_size,
        opt.strike,
        opt.option_type,
        opt.expiry,
        opt.underlying,
    ) == ("OPTIDX", Decimal("75.0"), Decimal("24500.000000"), "CE", date(2026, 11, 26), "NIFTY")
    eq = infos["NSE_EQ:3499"]
    assert eq.strike is None and eq.option_type is None and eq.expiry is None  # -0.01 / XX
    compact = {i.broker_instrument_id: i for i, _ in rows("master_compact.csv")}
    assert set(compact) == {"IDX_I:1", "BSE_CURRENCY:1026077"}
    assert compact["BSE_CURRENCY:1026077"].tick_size == Decimal("0.25")


def test_token_expiry_and_ref_parsing() -> None:
    assert parse.token_expiry({"tokenValidity": "30/03/2025 15:37"}) == datetime(
        2025, 3, 30, 10, 7, tzinfo=UTC
    )
    assert parse.split_ref("IDX_I:13") == ("IDX_I", "13")
    for bad in ("NSE:1", "NSE_EQ:", "NSE_EQ:12a", "13"):
        with pytest.raises(ValueError):
            parse.split_ref(bad)


# --- client -------------------------------------------------------------------------------


@respx.mock
async def test_client_headers_buckets_and_secret_hygiene() -> None:
    route = respx.get(f"{BASE}/fundlimit").mock(
        return_value=httpx.Response(200, json=fx("fundlimit.json"))
    )
    adapter, lim = make_adapter()
    await adapter.funds(CLIENT)
    req = route.calls[0].request
    assert req.headers["access-token"] == TOKEN and req.headers["client-id"] == CLIENT
    assert lim.calls == ["non_trading"]
    assert TOKEN not in repr(adapter.client)
    assert adapter.client.last_request_id and adapter.client.last_request_id.startswith("aureon-")


@respx.mock
async def test_client_error_mapping_and_retry() -> None:
    adapter, lim = make_adapter()
    respx.get(f"{BASE}/positions").mock(
        side_effect=[
            httpx.Response(
                429,
                json={
                    "errorType": "Rate_Limit",
                    "errorCode": "DH-904",
                    "errorMessage": "Too many requests",
                },
            ),
            httpx.Response(200, json=fx("positions.json")),
        ]
    )
    assert len(await adapter.positions(CLIENT)) == 2
    assert lim.calls.count("non_trading") == 2  # the retry also waited for the limiter

    respx.get(f"{BASE}/orders").mock(
        return_value=httpx.Response(
            400,
            json={
                "errorType": "Input_Exception",
                "errorCode": "DH-905",
                "errorMessage": "Missing required fields",
            },
        )
    )
    with pytest.raises(DhanApiError) as exc:
        await adapter.orders(CLIENT)
    assert exc.value.code == "DH-905" and TOKEN not in str(exc.value)

    respx.get(f"{BASE}/holdings").mock(
        return_value=httpx.Response(
            401,
            json={
                "errorType": "Invalid_Authentication",
                "errorCode": "DH-901",
                "errorMessage": "Client ID or user generated access token is invalid",
            },
        )
    )
    with pytest.raises(CredentialsExpired):
        await adapter.holdings(CLIENT)

    respx.get(f"{BASE}/trades").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(BrokerError, match="could not reach Dhan"):
        await adapter.trades(CLIENT)


async def test_client_rejects_path_injection() -> None:
    adapter, _ = make_adapter()
    with pytest.raises(ValueError):
        await adapter.client.order("../fundlimit")


# --- adapter ------------------------------------------------------------------------------


@respx.mock
async def test_health_ok_warn_expired_and_data_plan() -> None:
    adapter, _ = make_adapter()
    soon = (datetime.now(UTC) + timedelta(hours=1)).astimezone(parse.IST)
    route = respx.get(f"{BASE}/profile")
    route.mock(return_value=httpx.Response(200, json=fx("profile.json")))
    h = await adapter.health()
    assert (
        h.ok
        and h.detail is None
        and h.credentials_expire_at == datetime(2030, 3, 30, 10, 7, tzinfo=UTC)
    )
    route.mock(
        return_value=httpx.Response(
            200,
            json={
                **fx("profile.json"),
                "tokenValidity": soon.strftime("%d/%m/%Y %H:%M"),
                "dataPlan": "Deactive",
            },
        )
    )
    h = await adapter.health()
    assert h.ok and "expires in" in (h.detail or "") and "Data APIs plan" in (h.detail or "")
    route.mock(
        return_value=httpx.Response(
            200, json={**fx("profile.json"), "tokenValidity": "01/01/2020 10:00"}
        )
    )
    assert (await adapter.health()).ok is False
    route.mock(return_value=httpx.Response(401, json={"errorCode": "DH-901"}))
    h = await adapter.health()
    assert h.ok is False and "expired or invalid" in (h.detail or "")


async def test_other_account_ids_are_refused() -> None:
    adapter, _ = make_adapter()
    with pytest.raises(NotSupported, match="different client id"):
        await adapter.funds("9999999999")


@respx.mock
async def test_adapter_contract() -> None:
    adapter, _ = make_adapter()
    respx.get(f"{BASE}/profile").mock(return_value=httpx.Response(200, json=fx("profile.json")))
    respx.get(f"{BASE}/fundlimit").mock(return_value=httpx.Response(200, json=fx("fundlimit.json")))
    respx.get(f"{BASE}/positions").mock(return_value=httpx.Response(200, json=fx("positions.json")))
    respx.get(f"{BASE}/orders").mock(return_value=httpx.Response(200, json=fx("orders.json")))
    respx.post(f"{BASE}/marketfeed/quote").mock(
        return_value=httpx.Response(200, json=fx("quote.json"))
    )
    respx.get(CSV_URL).mock(
        return_value=httpx.Response(200, content=(FIX / "master_detailed.csv").read_bytes())
    )
    t0 = int(datetime(2026, 11, 2, 3, 45, tzinfo=UTC).timestamp())
    respx.post(f"{BASE}/charts/intraday").mock(
        return_value=httpx.Response(
            200,
            json={
                "open": [142, 143],
                "high": [143, 144],
                "low": [141, 142],
                "close": [142.5, 143.5],
                "volume": [10, 20],
                "timestamp": [t0, t0 + 900],
            },
        )
    )
    supported = await check_adapter(adapter, CLIENT, "NSE_EQ:3499")
    assert supported == {
        "accounts": True,
        "funds": True,
        "positions": True,
        "orders": True,
        "instruments": True,
        "quotes": True,
        "candles": True,
        "subscribe_ticks": False,
    }


@respx.mock
async def test_intraday_candles_chunked_to_90_days_and_filtered() -> None:
    adapter, lim = make_adapter()
    route = respx.post(f"{BASE}/charts/intraday").mock(
        return_value=httpx.Response(
            200,
            json={"open": [], "high": [], "low": [], "close": [], "volume": [], "timestamp": []},
        )
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    await adapter.candles("NSE_FNO:52175", Timeframe.M15, start, start + timedelta(days=200))
    bodies = [json.loads(c.request.content) for c in route.calls]
    assert len(bodies) == 3 and lim.calls.count("data") == 3
    assert bodies[0]["interval"] == 15 and bodies[0]["instrument"] == "OPTIDX"
    assert bodies[0]["oi"] is True and bodies[0]["fromDate"] == "2026-01-01 05:30:00"
    with pytest.raises(NotSupported, match="30m"):
        await adapter.candles("NSE_EQ:3499", Timeframe.M30, start, start + timedelta(days=1))
    with pytest.raises(BrokerError, match="unknown Dhan instrument"):
        await adapter.candles("NSE_EQ:1", Timeframe.M5, start, start + timedelta(days=1))


@respx.mock
async def test_daily_candles_use_exclusive_to_date() -> None:
    adapter, _ = make_adapter()
    route = respx.post(f"{BASE}/charts/historical").mock(
        return_value=httpx.Response(200, json=fx("daily.json"))
    )
    out = await adapter.candles(
        "NSE_EQ:3499",
        Timeframe.D1,
        datetime(2012, 1, 10, tzinfo=UTC),
        datetime(2012, 1, 12, tzinfo=UTC),
    )
    body = json.loads(route.calls[0].request.content)
    assert (body["fromDate"], body["toDate"]) == ("2012-01-10", "2012-01-13")
    assert "expiryCode" not in body  # equity: no expiry
    assert [c.ts.date() for c in out] == [date(2012, 1, 10), date(2012, 1, 11)]


@respx.mock
async def test_quotes_grouped_by_segment_and_option_chain() -> None:
    adapter, lim = make_adapter()
    q = respx.post(f"{BASE}/marketfeed/quote").mock(
        return_value=httpx.Response(200, json=fx("quote.json"))
    )
    out = await adapter.quotes(["NSE_EQ:3499", "NSE_FNO:52175"])
    assert json.loads(q.calls[0].request.content) == {"NSE_EQ": [3499], "NSE_FNO": [52175]}
    assert {x.symbol for x in out} == {"NSE_EQ:3499", "NSE_FNO:52175"}
    assert lim.calls == ["quote"]

    oc = respx.post(f"{BASE}/optionchain").mock(
        return_value=httpx.Response(200, json=fx("optionchain.json"))
    )
    chain = await adapter.option_chain("IDX_I:13", date(2026, 11, 26))
    assert json.loads(oc.calls[0].request.content) == {
        "UnderlyingScrip": 13,
        "UnderlyingSeg": "IDX_I",
        "Expiry": "2026-11-26",
    }
    # Leg segment for an index found by looking the leg id up in the master (NSE here).
    assert chain.strikes[1].call and chain.strikes[1].call.broker_instrument_id == "NSE_FNO:42528"
    assert lim.calls[-1] == "option_chain"
    respx.post(f"{BASE}/optionchain/expirylist").mock(
        return_value=httpx.Response(
            200, json={"data": ["2026-11-26", "2026-11-04"], "status": "success"}
        )
    )
    assert await adapter.option_expiries("IDX_I:13") == [date(2026, 11, 4), date(2026, 11, 26)]


# --- feed ---------------------------------------------------------------------------------


def hdr(code: int, length: int, seg: int, sid: int) -> bytes:
    return struct.pack("<BHBI", code, length, seg, sid)


def ticker(sid: int = 3499, ltp: float = 142.85, ltt: int = 1793350000) -> bytes:
    return hdr(2, 16, 1, sid) + struct.pack("<fI", ltp, ltt)


def test_parse_every_packet_type() -> None:
    quote = hdr(4, 50, 2, 52175) + struct.pack(
        "<fHIfIIIffff", 130.05, 75, 1793350000, 129.9, 1000, 400, 600, 128.0, 131.0, 132.5, 127.0
    )
    full = (
        hdr(8, 162, 2, 52175)
        + struct.pack(
            "<fHIfIIIIIIffff",
            130.05,
            75,
            1793350000,
            129.9,
            1000,
            400,
            600,
            3786445,
            3800000,
            3700000,
            128.0,
            131.0,
            132.5,
            127.0,
        )
        + b"".join(
            struct.pack("<IIHHff", 100 + k, 200 + k, 1, 2, 130.0 - k * 0.05, 130.1 + k * 0.05)
            for k in range(5)
        )
    )
    oi = hdr(5, 12, 2, 52175) + struct.pack("<I", 3786445)
    prev = hdr(6, 16, 1, 3499) + struct.pack("<fI", 142.35, 0)
    disc = hdr(50, 10, 0, 0) + struct.pack("<H", 807)
    assert len(full) == 162 and len(quote) == 50
    packets, skipped = parse_frame(ticker() + quote + full + oi + prev + disc)
    assert skipped == 0
    kinds = [p.kind for p in packets]
    assert kinds == ["ticker", "quote", "full", "oi", "prev_close", "disconnect"]
    t, q, f, o, pc, d = packets
    assert (t.key, t.ltp) == ("NSE_EQ:3499", Decimal("142.85"))
    assert t.ltt == datetime.fromtimestamp(1793350000, UTC)
    # Wire order is open, CLOSE, high, low.
    assert (q.open, q.close, q.high, q.low) == (
        Decimal(128),
        Decimal(131),
        Decimal("132.5"),
        Decimal(127),
    )
    assert (q.ltq, q.volume, q.total_sell_qty, q.total_buy_qty) == (75, 1000, 400, 600)
    assert f.oi == 3786445 and f.depth and f.depth[4]["bid_qty"] == 104
    assert f.depth[0]["bid"] == "130" and f.depth[0]["ask"] == "130.1"
    assert o.oi == 3786445 and pc.prev_close == Decimal("142.35") and d.reason == 807


def test_unknown_or_truncated_packets_are_skipped_not_guessed() -> None:
    packets, skipped = parse_frame(ticker() + hdr(7, 8, 1, 0) + ticker(sid=1))
    assert [p.kind for p in packets] == ["ticker"] and skipped == 1
    packets, skipped = parse_frame(ticker()[:12])
    assert packets == [] and skipped == 1


def test_subscribe_batching_and_validation() -> None:
    inst = [("NSE_EQ", str(i)) for i in range(251)]
    msgs = [json.loads(m) for m in subscribe_messages(inst, "quote")]
    assert [m["InstrumentCount"] for m in msgs] == [100, 100, 51]
    assert msgs[0]["RequestCode"] == 17
    assert msgs[0]["InstrumentList"][0] == {"ExchangeSegment": "NSE_EQ", "SecurityId": "0"}
    assert (
        json.loads(subscribe_messages(inst[:1], "full", unsubscribe=True)[0])["RequestCode"] == 22
    )
    assert parse_instruments(["IDX_I:13", "IDX_I:13", "NSE_EQ:1"]) == [
        ("IDX_I", "13"),
        ("NSE_EQ", "1"),
    ]
    with pytest.raises(ValueError):
        parse_instruments(["NSE:1"])
    with pytest.raises(ValueError, match="5000"):
        parse_instruments([f"NSE_EQ:{i}" for i in range(5001)])


class FakeWS:
    def __init__(self, frames: list[bytes | Exception]) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    async def recv(self) -> bytes:
        await asyncio.sleep(0)
        if not self.frames:
            await asyncio.sleep(3600)
        item = self.frames.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


def make_feed(sockets: list[FakeWS], queue_max: int = 100) -> tuple[DhanFeed, list[str]]:
    urls: list[str] = []
    it = iter(sockets)

    async def connector(url: str) -> FakeWS:
        urls.append(url)
        return next(it)

    feed = DhanFeed(
        url="wss://feed.test",
        client_id=CLIENT,
        access_token=TOKEN,
        instruments=[("NSE_EQ", "3499")],
        mode="ticker",
        queue_max=queue_max,
        max_backoff=0.05,
        connector=connector,
    )
    return feed, urls


async def test_feed_reconnects_after_drop_and_resubscribes() -> None:
    ws1 = FakeWS([ticker(), ConnectionError("reset")])
    ws2 = FakeWS([ticker(ltp=143.0)])
    feed, urls = make_feed([ws1, ws2])
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    for _ in range(200):
        if feed.stats.packets >= 2:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert feed.stats.connects == 2 and feed.stats.reconnects == 1
    assert json.loads(ws1.sent[0])["RequestCode"] == 15 and ws2.sent  # resubscribed
    assert "token=" in urls[0] and TOKEN not in repr(feed)
    assert json.loads(ws2.sent[-1]) == {"RequestCode": 12}  # clean disconnect on stop
    assert (await feed.queue.get()).ltp == Decimal("142.85")


async def test_feed_stops_for_good_on_auth_disconnect() -> None:
    disc = hdr(50, 10, 0, 0) + struct.pack("<H", 807)
    feed, urls = make_feed([FakeWS([disc]), FakeWS([])])
    await asyncio.wait_for(feed.run(asyncio.Event()), 2)
    assert feed.stats.auth_failed and feed.stats.last_disconnect_reason == 807
    assert len(urls) == 1  # no reconnect storm with a dead token


async def test_feed_queue_is_bounded_and_keeps_newest() -> None:
    frames: list[bytes | Exception] = [ticker(ltp=float(100 + i)) for i in range(10)]
    feed, _ = make_feed([FakeWS(frames)], queue_max=3)
    stop = asyncio.Event()
    task = asyncio.create_task(feed.run(stop))
    for _ in range(200):
        if feed.stats.packets >= 10:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert feed.queue.qsize() == 3 and feed.stats.dropped == 7
    assert (await feed.queue.get()).ltp == Decimal(107)
