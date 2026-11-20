"""Live market API (app/api/v1/live.py, the live basis of /stock-signals, docs/38).

No database or real Redis: sign-in is a dependency override (REST) or a patched
`authenticate` (WebSocket), instrument lookups are patched, Redis is tests/fake_redis.py.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

from app.api.v1 import live as live_api
from app.auth.deps import CurrentSession, get_current
from app.core.config import Settings
from app.main import create_app
from app.services import market_live as ml
from tests.fake_redis import FakeRedis

USER = uuid.uuid4()
REF = "NSE_EQ:2885"


def fake_current() -> CurrentSession:
    return CurrentSession(
        user=SimpleNamespace(id=USER, role="user", is_active=True),  # type: ignore[arg-type]
        session=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        token="t",
    )


def inst(iid: int = 7, ref: str = REF, active: bool = True) -> Any:
    return SimpleNamespace(
        id=iid,
        broker="dhan",
        broker_instrument_id=ref,
        symbol="RELIANCE",
        trading_symbol="RELIANCE",
        display_name="Reliance",
        name="RELIANCE INDUSTRIES",
        exchange="NSE",
        segment="NSE_EQ",
        is_active=active,
    )


@pytest.fixture
def s(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "MARKET_LIVE_UNIVERSES": ["fno_stocks"],
            "MARKET_LIVE_SCREENER_TIMEFRAMES": ["15m"],
            "MARKET_WS_MAX_TOPICS": 3,
            "MARKET_WS_MAX_CONNECTIONS_PER_USER": 2,
            "MARKET_WS_MESSAGES_PER_MINUTE": 5,
            "MARKET_LIVE_MIN_AVG_TURNOVER": 0.0,
            "CORS_ORIGINS": ["https://app.example"],
        }
    )


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    async def by_id(db: Any, ids: Any) -> dict[int, Any]:
        return {i: inst(i) for i in ids if i == 7}

    async def no_states(db: Any, ids: Any, tf: str) -> dict[int, Any]:
        return {}

    monkeypatch.setattr(ml, "instruments_by_id", by_id)
    monkeypatch.setattr(ml, "confirmed_states", no_states)


@pytest.fixture
async def app(s: Settings, redis: FakeRedis, patched: None) -> AsyncIterator[FastAPI]:
    a = create_app(s)
    async with LifespanManager(a):
        a.state.redis = redis
        yield a


@pytest.fixture
async def anon(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


@pytest.fixture
async def user(app: FastAPI) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_current] = fake_current
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c
    app.dependency_overrides.clear()


# --- REST -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/trading/live/quotes?instrument_id=7",
        "/api/trading/live/status",
        "/api/trading/live/protocol",
        "/api/trading/chart?instrument_id=7",
        "/api/trading/stock-signals/market-movers?basis=live",
        "/api/trading/stock-signals/trending?basis=live",
    ],
)
async def test_rest_routes_need_a_session(anon: AsyncClient, path: str) -> None:
    assert (await anon.get(path)).status_code == 401


async def test_live_quotes(user: AsyncClient, redis: FakeRedis) -> None:
    now = datetime.now(UTC)
    await redis.set(
        ml.QUOTE_KEY.format(ref=REF),
        json.dumps(
            {
                "key": REF,
                "ltp": "2510.5",
                "ltt": now.isoformat(),
                "session_date": "2026-11-02",
                "prev_close": "2500",
                "prev_close_source": "feed",
                "change": "10.5",
                "change_pct": "0.4200",
                "status": "live",
                "age_seconds": 0.1,
                "received_at": now.isoformat(),
                "source": "dhan_feed",
            }
        ),
    )
    r = await user.get("/api/trading/live/quotes?instrument_id=7&instrument_id=99")
    assert r.status_code == 200, r.text
    body = r.json()
    (q,) = body["items"]
    assert q["instrument_id"] == 7 and q["symbol"] == "RELIANCE" and q["ref"] == REF
    assert q["prev_close_source"] == "feed" and Decimal(q["change_pct"]) == Decimal("0.42")
    assert q["status"] in ("live", "stale", "closed")  # re-judged at read time
    assert body["missing"] == [99] and "open" in body["market"]


async def test_protocol_describes_the_socket(user: AsyncClient, s: Settings) -> None:
    body = (await user.get("/api/trading/live/protocol")).json()
    assert body["url"] == "/api/trading/live/ws"
    assert body["limits"]["max_topics_per_connection"] == s.MARKET_WS_MAX_TOPICS
    assert "4008" in body["close_codes"] and "4401" in body["close_codes"]
    assert body["universes"] == ["fno_stocks"]


async def test_chart_merges_stored_and_live(
    user: AsyncClient, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    step = timedelta(minutes=15)
    start = now - step * 60
    stored = [
        SimpleNamespace(
            ts=start + step * i,
            open=Decimal(100 + i),
            high=Decimal(101 + i),
            low=Decimal(99 + i),
            close=Decimal(100 + i),
            volume=Decimal(1000),
            source="broker",
        )
        for i in range(40)
    ]

    async def fake_stored(db: Any, iid: int, tf: Any, *, limit: int, now: datetime) -> list[Any]:
        return stored

    monkeypatch.setattr(ml, "stored_candles", fake_stored)
    r = await user.get("/api/trading/chart?instrument_id=7&timeframe=15m&limit=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["candles"]) == 30 and body["ema_fast_period"] == 8
    assert all(c["source"] == "broker" for c in body["candles"])
    assert body["candles"][-1]["ema_slow"] is not None  # warm-up comes from before the window
    assert (await user.get("/api/trading/chart?instrument_id=8")).status_code == 404


def movers_snapshot() -> dict[str, Any]:
    def row(iid: int, chg: str, status: str = "live") -> dict[str, Any]:
        return {
            "instrument_id": iid,
            "key": f"NSE_EQ:{iid}",
            "symbol": f"S{iid}",
            "exchange": "NSE",
            "segment": "NSE_EQ",
            "ltp": "100",
            "change_pct": chg,
            "status": status,
            "avg_turnover": 1e9,
            "rvol": None,
            "prev_close_source": "daily_candle",
            "ltt": "2026-11-02T05:00:00+00:00",
        }

    return {
        "universe": "fno_stocks",
        "computed_at": "2026-11-02T05:00:01+00:00",
        "universe_size": 4,
        "universe_total": 4,
        "capped": False,
        "quotes_as_of": "2026-11-02T05:00:00+00:00",
        "market": {"open": True, "session": "open", "now": "2026-11-02T05:00:01+00:00"},
        "rows": [row(1, "3.5"), row(2, "-2.0"), row(3, "1.0"), row(4, "9.0", "stale")],
    }


async def test_live_market_movers(user: AsyncClient, redis: FakeRedis) -> None:
    url = "/api/trading/stock-signals/market-movers?basis=live&kind=gainers"
    assert (await user.get(url)).status_code == 503  # nothing published yet: said so, not empty
    await redis.set(ml.MOVERS_KEY.format(universe="fno_stocks"), json.dumps(movers_snapshot()))
    body = (await user.get(url)).json()
    assert body["basis"] == "live" and [i["instrument_id"] for i in body["items"]] == [1, 3]
    assert body["items"][0]["state"] == "UNAVAILABLE"  # no confirmed state: never invented
    assert body["items"][0]["quote_status"] == "live"
    assert body["scope"]["market_wide"] is False and body["coverage"]["stale"] == 1
    stale = (await user.get(url + "&include_stale=true")).json()
    assert stale["items"][0]["instrument_id"] == 4
    assert (await user.get(url + "&universe=nse_equities")).status_code == 422
    # The default stays the confirmed (closed-candle) contract.
    assert "basis=confirmed" not in url


async def test_live_trending(user: AsyncClient, redis: FakeRedis) -> None:
    rows = [
        {
            "instrument_id": 1,
            "ref": "NSE_EQ:1",
            "symbol": "A",
            "exchange": "NSE",
            "segment": "NSE_EQ",
            "timeframe": "15m",
            "basis": "live",
            "state": "BULLISH_TREND",
            "ema_fast": "101",
            "ema_slow": "100",
            "trend_score": 70.0,
            "last_price": "102",
            "candle_ts": "2026-11-02T04:45:00+00:00",
            "candle_closed": False,
            "partial_bars": 0,
        },
        {
            "instrument_id": 2,
            "ref": "NSE_EQ:2",
            "symbol": "B",
            "exchange": "NSE",
            "segment": "NSE_EQ",
            "timeframe": "15m",
            "basis": "live",
            "state": "BULLISH_PENDING",
            "ema_fast": "99",
            "ema_slow": "100",
            "trend_score": 80.0,
            "last_price": "98",
        },
        {"instrument_id": 3, "ref": "NSE_EQ:3", "state": "UNAVAILABLE", "timeframe": "15m"},
    ]
    await redis.set(
        ml.SCREENER_KEY.format(tf="15m"),
        json.dumps(
            {
                "timeframe": "15m",
                "basis": "live",
                "computed_at": "2026-11-02T05:00:00+00:00",
                "universes": ["fno_stocks"],
                "rows": rows,
                "market": {"open": True, "session": "open", "now": "2026-11-02T05:00:00+00:00"},
            }
        ),
    )
    body = (await user.get("/api/trading/stock-signals/trending?basis=live")).json()
    # B's EMAs disagree with its bullish state: excluded, as in the confirmed ranking.
    assert [i["instrument_id"] for i in body["items"]] == [1]
    assert body["items"][0]["candle_closed"] is False and body["coverage"]["unavailable"] == 1
    bad = await user.get("/api/trading/stock-signals/trending?basis=live&timeframe=1h")
    assert bad.status_code == 422


# --- WebSocket --------------------------------------------------------------------------------


@pytest.fixture
def ws_client(
    s: Settings, redis: FakeRedis, patched: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    async def authed(ws: Any) -> CurrentSession | None:
        return fake_current() if ws.cookies.get(s.session_cookie_name) == "good" else None

    async def lookup(ws: Any, ids: list[int]) -> dict[int, Any]:
        return {i: inst(i) for i in ids if i == 7}

    async def valid(ws: Any, current: Any) -> bool:
        return True

    monkeypatch.setattr(live_api, "authenticate", authed)
    monkeypatch.setattr(live_api, "lookup_instruments", lookup)
    monkeypatch.setattr(live_api, "session_still_valid", valid)
    a = create_app(s)
    with TestClient(a, base_url="https://test") as c:
        a.state.redis = redis
        yield c


def recv_until(ws: Any, kind: str, limit: int = 20) -> dict[str, Any]:
    for _ in range(limit):
        m = ws.receive_json()
        if m["type"] == kind:
            return m
    raise AssertionError(f"no {kind}")


def test_ws_refuses_without_a_session(ws_client: TestClient) -> None:
    with ws_client.websocket_connect("/api/trading/live/ws") as ws:
        assert ws.receive_json()["code"] == "unauthorized"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == live_api.CLOSE_UNAUTHORIZED


def test_ws_refuses_a_foreign_origin(ws_client: TestClient, s: Settings) -> None:
    ws_client.cookies.set(s.session_cookie_name, "good")
    with ws_client.websocket_connect(
        "/api/trading/live/ws", headers={"origin": "https://evil.example"}
    ) as ws:
        assert ws.receive_json()["code"] == "forbidden_origin"
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == live_api.CLOSE_FORBIDDEN_ORIGIN


def test_ws_subscribe_snapshot_update_and_errors(
    ws_client: TestClient, s: Settings, redis: FakeRedis
) -> None:
    ws_client.cookies.set(s.session_cookie_name, "good")
    with ws_client.websocket_connect(
        "/api/trading/live/ws", headers={"origin": "https://app.example"}
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["max_topics"] == 3 and hello["seq"] == 1
        ws.send_text(
            json.dumps(
                {
                    "op": "subscribe",
                    "id": "a",
                    "topics": [
                        {"kind": "quote", "instrument_id": 7},
                        {"kind": "quote", "instrument_id": 99},
                        {"kind": "rankings", "universe": "nope"},
                        {"kind": "candle", "instrument_id": 7, "timeframe": "5m"},
                    ],
                }
            )
        )
        ack = recv_until(ws, "ack")
        assert ack["id"] == "a" and ack["accepted"] == ["quote:7", "candle:7:5m"]
        assert {r["reason"] for r in ack["rejected"]} == {"unknown_instrument", "unknown_universe"}
        snaps = [recv_until(ws, "snapshot"), recv_until(ws, "snapshot")]
        assert {x["topic"] for x in snaps} == {"quote:7", "candle:7:5m"}
        # Browsers watching an instrument = demand for the one feed connection.
        assert REF.encode() in redis.zsets["dhan:feed:demand"]
        # An update published by the worker reaches the subscriber, tagged with its topic.
        hub = ws_client.app.state.live_hub  # type: ignore[attr-defined]
        hub.dispatch(ml.CH_QUOTES, json.dumps({"type": "quote", "key": REF, "ltp": "1"}))
        q = recv_until(ws, "quote")
        assert q["topic"] == "quote:7" and q["instrument_id"] == 7 and q["seq"] > ack["seq"]
        # Topic limit (3): two more → one accepted, one rejected.
        ws.send_text(
            json.dumps(
                {
                    "op": "subscribe",
                    "topics": [
                        {"kind": "screener", "timeframe": "15m"},
                        {"kind": "candle", "instrument_id": 7, "timeframe": "1m"},
                    ],
                }
            )
        )
        ack = recv_until(ws, "ack")
        assert ack["accepted"] == ["screener:15m"]
        assert ack["rejected"] == [{"topic": "candle:7:1m", "reason": "topic_limit"}]
        ws.send_text("{not json")
        assert recv_until(ws, "error")["code"] == "bad_message"
        ws.send_text(
            json.dumps({"op": "unsubscribe", "topics": [{"kind": "quote", "instrument_id": 7}]})
        )
        assert recv_until(ws, "ack")["accepted"] == ["quote:7"]


def test_ws_rate_limit_and_connection_limit(ws_client: TestClient, s: Settings) -> None:
    ws_client.cookies.set(s.session_cookie_name, "good")
    with (
        ws_client.websocket_connect("/api/trading/live/ws") as a,
        ws_client.websocket_connect("/api/trading/live/ws") as b,
    ):
        a.receive_json()
        b.receive_json()
        with ws_client.websocket_connect("/api/trading/live/ws") as c:
            assert c.receive_json()["code"] == "too_many_connections"
            with pytest.raises(WebSocketDisconnect) as exc:
                c.receive_json()
            assert exc.value.code == live_api.CLOSE_TOO_MANY
        for _ in range(6):  # MARKET_WS_MESSAGES_PER_MINUTE = 5
            a.send_text(json.dumps({"op": "ping"}))
        assert recv_until(a, "bye")["reason"] == "rate_limited"
        with pytest.raises(WebSocketDisconnect) as exc:
            for _ in range(10):
                a.receive_json()
        assert exc.value.code == live_api.CLOSE_POLICY


def test_origin_rules(s: Settings) -> None:
    assert live_api.origin_allowed(None, "api.example", s)  # not a browser page
    assert live_api.origin_allowed("https://app.example", "api.example", s)
    assert live_api.origin_allowed("https://api.example", "api.example", s)  # same host
    assert not live_api.origin_allowed("https://evil.example", "api.example", s)
