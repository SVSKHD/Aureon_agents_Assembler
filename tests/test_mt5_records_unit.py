"""MT5 records API without a database (docs/36-mt5-records.md): row splitting (envelope vs
payload), kind validation, per-row rejection, the token (missing, wrong, reused, weak in
prod), size and content-type limits, the rate limit, cursor encoding, bars validation, the
Access exemption, the UI manifest and the purge job."""

from __future__ import annotations

import gzip
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.agents.trading.mt5.push import PushInvalid
from app.agents.trading.ratelimit import RateLimitExceeded
from app.api import ui
from app.api.v1 import mt5_records as route
from app.core.cf_access import CloudflareAccessMiddleware, is_machine_path, is_machine_request
from app.core.config import Environment, Settings
from app.core.prod_checks import (
    RECORDS_TOKEN_DISTINCT_FROM,
    production_problems,
    records_token_problems,
)
from app.main import create_app
from app.services import app_settings
from app.services import mt5_records as svc
from app.workers.scheduler import default_jobs
from tests.mt5_push_fixture import TOKEN as INGEST_TOKEN

TOKEN = "r" * 8 + "Mt5-Records_9fK2pQ7xL4vB8nZ1cW6"  # 39 chars, test only
RECORDS = "/api/trading/mt5/records"
BARS = "/api/trading/mt5/bars"
NOW = datetime(2026, 11, 13, 9, 30, tzinfo=UTC)


def row(**kw: Any) -> dict[str, Any]:
    """A row exactly as the bot's store.build() makes it."""
    base: dict[str, Any] = {
        "record_id": uuid.uuid4().hex,
        "kind": "event",
        "utc_time": "2026-11-13T09:30:03+00:00",
        "server_time": "2026-11-13 11:30:03",
        "version": "1.7.1",
        "account_login": 12345678,
        "symbol": "XAUUSD",
        "tf": "M30",
        "bar_time": "2026-11-13 09:00:00",
        "label": "CROSS",
        "side": "SHORT",
        "tag": "below 200",
        "price": 4038.1,
        "gap": 0.42,
        "atr": 6.1,
        "pair": None,
    }
    base.update(kw)
    return base


def batch(rows: list[Any], **kw: Any) -> dict[str, Any]:
    return {"schema": svc.RECORDS_SCHEMA, "batch_id": str(uuid.uuid4()), "rows": rows, **kw}


def bar(ts: str = "2026-11-13T09:00:00Z", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ts": ts,
        "server_ts": "2026-11-13 11:00:00",
        "open": 4040.5,
        "high": 4044.4,
        "low": 4037.9,
        "close": 4038.1,
        "tick_volume": 1830,
        "spread": 16,
        "ema8": 4041.2,
        "ema32": 4041.6,
        "ema200": 4090.3,
        "atr14": 6.1,
    }
    base.update(kw)
    return base


def bars_doc(bars: list[Any], **kw: Any) -> dict[str, Any]:
    return {
        "schema": svc.BARS_SCHEMA,
        "symbol": "XAUUSD",
        "timeframe": "M30",
        "source": "bot",
        "bars": bars,
        **kw,
    }


# --- fakes ----------------------------------------------------------------------------------


class FakeStore:
    def __init__(self) -> None:
        self.ids: set[uuid.UUID] = set()
        self.bars: dict[tuple[str, str, datetime], dict[str, Any]] = {}
        self.calls = 0

    async def store_records(self, rows: list[svc.RecordRow], batch_id: uuid.UUID) -> int:
        self.calls += 1
        new = [r for r in rows if r.record_id not in self.ids]
        self.ids.update(r.record_id for r in new)
        return len(new)

    async def store_bars(self, bars: list[dict[str, Any]]) -> svc.BarsResult:
        self.calls += 1
        ins = upd = 0
        for b in bars:
            key = (b["symbol"], b["timeframe"], b["ts"])
            old = self.bars.get(key)
            if old is None:
                ins += 1
            elif old != b:
                upd += 1
            self.bars[key] = b
        return svc.BarsResult(ins, upd, len(bars) - ins - upd)


class FakeLimiter:
    def __init__(self, allow: int = 1000) -> None:
        self.allow = allow

    async def acquire(self, bucket: str, max_wait: float = 30.0) -> None:
        self.allow -= 1
        if self.allow < 0:
            raise RateLimitExceeded(bucket, 7.0)


def _settings(settings: Settings, **kw: Any) -> Settings:
    return Settings.model_validate({**settings.model_dump(), **kw})


class Ctx:
    def __init__(self, app: FastAPI, client: AsyncClient, store: FakeStore) -> None:
        self.app, self.c, self.store = app, client, store


async def _ctx(s: Settings) -> AsyncIterator[Ctx]:
    app = create_app(s)
    async with LifespanManager(app) as mgr:
        store = FakeStore()
        app.state.mt5_records_store = store
        app.state.mt5_records_limiter = FakeLimiter()
        async with AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c:
            yield Ctx(app, c, store)


@pytest.fixture
async def ctx(settings: Settings) -> AsyncIterator[Ctx]:
    async for x in _ctx(_settings(settings, MT5_RECORDS_TOKEN=TOKEN)):
        yield x


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "content-type": "application/json"}


async def _post(c: AsyncClient, url: str, doc: Any, **headers: str) -> Any:
    body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    return await c.post(url, content=body, headers={**_auth(), **headers})


# --- row splitting --------------------------------------------------------------------------


def test_envelope_goes_to_columns_and_the_rest_to_payload() -> None:
    r = row()
    out = svc.split_row(r, NOW)
    assert out.record_id == uuid.UUID(r["record_id"])
    assert (out.kind, out.symbol, out.account_login, out.bot_version) == (
        "event",
        "XAUUSD",
        12345678,
        "1.7.1",
    )
    assert out.utc_time == datetime(2026, 11, 13, 9, 30, 3, tzinfo=UTC)
    assert out.server_time == datetime(2026, 11, 13, 11, 30, 3)  # broker clock, no zone
    assert set(out.payload) == set(r) - set(svc.ENVELOPE)
    assert out.payload["label"] == "CROSS" and out.payload["pair"] is None
    assert not set(svc.ENVELOPE) & set(out.payload)


def test_nullable_envelope_keys_and_lenient_forms() -> None:
    out = svc.split_row(
        row(server_time=None, account_login=None, symbol=None, utc_time="2026-11-13T09:30:03Z"),
        NOW,
    )
    assert out.server_time is None and out.account_login is None and out.symbol is None
    # dashed uuid, digit-string login, zone-less utc_time (taken as UTC)
    rid = uuid.uuid4()
    out = svc.split_row(
        row(record_id=str(rid), account_login="555", utc_time="2026-11-13T09:00:00"), NOW
    )
    assert out.record_id == rid and out.account_login == 555
    assert out.utc_time == datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    # payload may be empty: a row with only the envelope
    bare = {k: v for k, v in row().items() if k in svc.ENVELOPE}
    assert svc.split_row(bare, NOW).payload == {}


@pytest.mark.parametrize("kind", ["event", "alert", "sl_change", "weekly", "new_kind", "x"])
def test_known_and_unknown_kinds_are_accepted(kind: str) -> None:
    assert svc.split_row(row(kind=kind), NOW).kind == kind


@pytest.mark.parametrize("kind", ["", "Event", "sl-change", "a" * 33, "x1", None, 5, "ev ent"])
def test_bad_kinds_are_rejected(kind: Any) -> None:
    with pytest.raises(svc.RowInvalid, match="kind"):
        svc.split_row(row(kind=kind), NOW)


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({"record_id": None}, "record_id"),
        ({"record_id": "not-a-uuid"}, "record_id"),
        ({"utc_time": None}, "utc_time"),
        ({"utc_time": "yesterday"}, "utc_time"),
        ({"utc_time": "2026-11-14T09:30:04+00:00"}, "24 hours"),
        ({"server_time": "11:30 tomorrow"}, "server_time"),
        ({"version": None}, "version"),
        ({"version": ""}, "version"),
        ({"version": "v" * 65}, "version"),
        ({"account_login": True}, "account_login"),
        ({"account_login": -1}, "account_login"),
        ({"account_login": 2**63}, "account_login"),
        ({"account_login": "12ab"}, "account_login"),
        ({"symbol": 7}, "symbol"),
        ({"note": "a\x00b"}, "NUL"),
        ({"deep": json.loads("[" * 20 + "]" * 20)}, "deeply"),
    ],
)
def test_row_validation(bad: dict[str, Any], reason: str) -> None:
    with pytest.raises(svc.RowInvalid, match=reason):
        svc.split_row(row(**bad), NOW)


def test_up_to_24h_in_the_future_is_fine() -> None:
    assert svc.split_row(row(utc_time="2026-11-14T09:30:00+00:00"), NOW)
    with pytest.raises(svc.RowInvalid):
        svc.split_row("not an object", NOW)


def test_batch_rejects_rows_one_by_one_and_counts_repeats() -> None:
    good = row()
    rows = [good, row(kind="BAD"), 42, dict(good), row(utc_time="2030-01-01T00:00:00Z"), row()]
    out = svc.split_batch(rows, NOW)
    assert len(out.rows) == 2 and out.repeated == 1
    assert [(r.index, r.record_id is not None) for r in out.rejected] == [
        (1, True),
        (2, False),
        (4, True),
    ]
    assert out.rejected[0].record_id == rows[1]["record_id"]
    assert "kind" in out.rejected[0].reason and "object" in out.rejected[1].reason


def test_batch_envelope() -> None:
    bid, rows = svc.parse_records_doc(batch([row()]), 500)
    assert isinstance(bid, uuid.UUID) and len(rows) == 1
    for bad, field in (
        ([], "$"),
        (batch([], schema="aureon.mt5.push/v1"), "schema"),
        ({**batch([]), "schema": None}, "schema"),
        (batch([], batch_id="x"), "batch_id"),
        ({**batch([]), "batch_id": None}, "batch_id"),
        (batch({"a": 1}), "rows"),  # type: ignore[arg-type]
        (batch([row()] * 3), "rows"),
    ):
        with pytest.raises(PushInvalid) as exc:
            svc.parse_records_doc(bad, 2)
        assert exc.value.fields == [field]


def test_json_constants_are_refused() -> None:
    with pytest.raises(PushInvalid):
        svc.parse_json(b'{"rows": [NaN]}')
    with pytest.raises(PushInvalid):
        svc.parse_json(b"{not json")
    assert svc.parse_json(b'{"a": 1.5}', decimals=True) == {"a": svc.Decimal("1.5")}


# --- bars validation ------------------------------------------------------------------------


def test_bars_document_and_values() -> None:
    doc = svc.parse_bars_doc(bars_doc([bar()], timeframe="m30"))
    assert doc.timeframe == "M30" and doc.source == "bot"
    good, bad = svc.split_bars(doc)
    assert bad == [] and len(good) == 1
    v = good[0]
    assert v["ts"] == datetime(2026, 11, 13, 9, 0, tzinfo=UTC)
    assert v["server_ts"] == datetime(2026, 11, 13, 11, 0)
    assert v["symbol"] == "XAUUSD" and v["tick_volume"] == 1830 and v["spread"] == 16
    assert str(v["close"]) == "4038.1"
    # optional fields may be null or missing
    minimal = {k: bar()[k] for k in ("ts", "open", "high", "low", "close")}
    good, bad = svc.split_bars(svc.parse_bars_doc(bars_doc([minimal])))
    assert bad == [] and good[0]["ema8"] is None and good[0]["tick_volume"] is None


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({"high": 4030.0}, "high is below low"),
        ({"open": 0}, "open must be positive"),
        ({"low": -1}, "low must be positive"),
        ({"close": None}, "close is required"),
        ({"close": "abc"}, "close must be a number"),
        ({"close": True}, "close must be a number"),
        ({"high": 1e13}, "out of range"),
        ({"ts": "nope"}, "ts"),
        ({"tick_volume": -5}, "tick_volume"),
        ({"tick_volume": 1.5}, "tick_volume"),
        ({"spread": 2**40}, "spread"),
        ({"server_ts": "x"}, "server_ts"),
    ],
)
def test_bad_bars_are_rejected_alone(bad: dict[str, Any], reason: str) -> None:
    doc = svc.parse_bars_doc(bars_doc([bar(**bad), bar(ts="2026-11-13T09:30:00Z")]))
    good, rejected = svc.split_bars(doc)
    assert len(good) == 1 and len(rejected) == 1
    assert rejected[0].index == 0 and reason in rejected[0].reason


def test_bars_envelope_and_duplicate_ts() -> None:
    for bad, field in (
        (bars_doc([], schema=svc.RECORDS_SCHEMA), "schema"),
        (bars_doc([], symbol="XAU USD"), "symbol"),
        (bars_doc([], timeframe="30min"), "timeframe"),
        (bars_doc([], source="Bot!"), "source"),
        (bars_doc("x"), "bars"),  # type: ignore[arg-type]
        (bars_doc([bar()] * (svc.MAX_BARS + 1)), "bars"),
    ):
        with pytest.raises(PushInvalid) as exc:
            svc.parse_bars_doc(bad)
        assert exc.value.fields == [field]
    good, _ = svc.split_bars(svc.parse_bars_doc(bars_doc([bar(close=4038.0), bar(close=4039.0)])))
    assert len(good) == 1 and str(good[0]["close"]) == "4039.0"  # the last one wins


# --- cursor ---------------------------------------------------------------------------------


def test_cursor_round_trip_and_garbage() -> None:
    rid = uuid.uuid4()
    t = datetime(2026, 11, 13, 9, 30, 3, 123456, tzinfo=UTC)
    cur = svc.encode_cursor(t, rid)
    assert "=" not in cur and svc.decode_cursor(cur) == (t, rid)
    for bad in ("", "!!!", "bm9wZQ", svc.encode_cursor(t, rid)[:-4], "MjAyNi0xMS0xM3x4"):
        with pytest.raises(ValueError):
            svc.decode_cursor(bad)


# --- the token ------------------------------------------------------------------------------


async def test_good_token_stores_the_batch(ctx: Ctx) -> None:
    rows = [row(), row(kind="position", event="opened", ticket=7)]
    r = await _post(ctx.c, RECORDS, batch(rows))
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 2, "duplicate": 0, "rejected": []}
    again = await _post(ctx.c, RECORDS, batch(rows))
    assert again.json() == {"accepted": 0, "duplicate": 2, "rejected": []}
    assert not ctx.c.cookies  # no session, no CSRF


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong-token-of-some-length-xxxxxxxxxxxxx"},
        {"authorization": f"Bearer {TOKEN}x"},
        {"authorization": f"Bearer {TOKEN[:-1]}"},
        {"authorization": f"Basic {TOKEN}"},
        {"authorization": TOKEN},
        {"authorization": "Bearer "},
        {"authorization": f"Bearer {INGEST_TOKEN}"},
    ],
)
async def test_missing_or_bad_token_is_401(ctx: Ctx, headers: dict[str, str]) -> None:
    for url, doc in ((RECORDS, batch([row()])), (BARS, bars_doc([bar()]))):
        r = await ctx.c.post(
            url,
            content=json.dumps(doc).encode(),
            headers={"content-type": "application/json"} | headers,
        )
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"
        assert TOKEN not in r.text
    assert ctx.store.calls == 0


async def test_token_compare_is_constant_time(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = route.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(route.hmac, "compare_digest", spy)
    for h in (f"Basic {TOKEN}", "Bearer nope", f"Bearer {TOKEN}"):
        await ctx.c.post(RECORDS, content=b"{}", headers={"authorization": h})
    assert len(calls) == 3
    assert all(isinstance(a, bytes) and b == TOKEN.encode() for a, b in calls)


async def test_unset_token_is_503(settings: Settings) -> None:
    async for ctx in _ctx(_settings(settings, MT5_INGEST_TOKEN=INGEST_TOKEN)):
        for url in (RECORDS, BARS):
            r = await _post(ctx.c, url, batch([row()]))
            assert r.status_code == 503
            assert r.json()["detail"]["code"] == "records_disabled"


async def test_the_tokens_do_not_cross(settings: Settings) -> None:
    """The ingest token does not authorize records, and the records token does not authorize
    the push/v1 ingest or any session route."""
    s = _settings(settings, MT5_RECORDS_TOKEN=TOKEN, MT5_INGEST_TOKEN=INGEST_TOKEN)
    async for ctx in _ctx(s):
        r = await _post(ctx.c, RECORDS, batch([row()]), authorization=f"Bearer {INGEST_TOKEN}")
        assert r.status_code == 401
        r = await _post(ctx.c, "/api/trading/mt5/ingest", {}, authorization=f"Bearer {TOKEN}")
        assert r.status_code == 401
        auth = {"authorization": f"Bearer {TOKEN}"}
        for path in (
            RECORDS,
            "/api/trading/mt5/records/stats",
            f"{BARS}?symbol=XAUUSD",
            "/api/trading/mt5/dataset/crosses?symbol=XAUUSD",
            "/api/trading/mt5/bot-status",
            "/api/auth/me",
        ):
            assert (await ctx.c.get(path, headers=auth)).status_code == 401, path
        assert ctx.store.calls == 0


@pytest.mark.parametrize(
    "name", ["MT5_INGEST_TOKEN", "MT5_STATUS_TOKEN", "SECRET_KEY", "AUREON_API_COLLECTOR_TOKEN"]
)
async def test_reused_token_fails_closed(settings: Settings, name: str) -> None:
    async for ctx in _ctx(_settings(settings, MT5_RECORDS_TOKEN=TOKEN, **{name: TOKEN})):
        r = await _post(ctx.c, RECORDS, batch([row()]))
        assert r.status_code == 503, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "records_misconfigured"
        assert name in detail["detail"] and TOKEN not in r.text
        assert ctx.store.calls == 0


async def test_weak_token_fails_closed_in_prod_only(settings: Settings) -> None:
    async for ctx in _ctx(_settings(settings, MT5_RECORDS_TOKEN="short-but-set")):
        r = await _post(ctx.c, RECORDS, batch([row()]), authorization="Bearer short-but-set")
        assert r.status_code == 200
        live: Settings = ctx.app.state.settings
        live.ENV = Environment.PROD  # what an overlay of a managed value would leave behind
        r = await _post(ctx.c, RECORDS, batch([row()]), authorization="Bearer short-but-set")
        assert r.status_code == 503 and r.json()["detail"]["code"] == "records_misconfigured"
        assert ctx.store.calls == 1


@pytest.mark.parametrize("name", RECORDS_TOKEN_DISTINCT_FROM)
def test_prod_checklist_rejects_reuse_of_every_named_credential(
    settings: Settings, name: str
) -> None:
    s = settings.model_copy()
    s.MT5_RECORDS_TOKEN = SecretStr(TOKEN)
    value: Any = SecretStr(f"{TOKEN},other-master-key") if name == "SETTINGS_MASTER_KEY" else None
    setattr(s, name, value or SecretStr(TOKEN))
    problems = production_problems(s)
    assert any(name in p and "MT5_RECORDS_TOKEN" in p for p in problems), problems
    assert all(TOKEN not in p for p in problems)


def test_prod_checklist_strength(settings: Settings) -> None:
    s = _settings(settings, ENV=Environment.PROD)
    s.MT5_RECORDS_TOKEN = SecretStr("short")
    assert any("MT5_RECORDS_TOKEN" in p for p in production_problems(s))
    s.MT5_RECORDS_TOKEN = SecretStr("example-" + "z9" * 16)
    assert any("placeholder" in p for p in records_token_problems(s))
    s.MT5_RECORDS_TOKEN = SecretStr(TOKEN)
    assert records_token_problems(s) == []
    s.MT5_RECORDS_TOKEN = None
    assert records_token_problems(s) == []


def test_managed_settings_apply_the_same_checks(settings: Settings) -> None:
    spec = app_settings.REGISTRY["MT5_RECORDS_TOKEN"]
    assert spec.secret and spec.reauth and spec.category == "mt5"
    with pytest.raises(app_settings.SettingsError) as exc:
        app_settings.normalize(spec, "too-short-token")
    assert "32" in exc.value.detail
    assert app_settings.normalize(spec, TOKEN) == TOKEN
    env = _settings(settings, MT5_INGEST_TOKEN=TOKEN)
    with pytest.raises(app_settings.SettingsError) as exc:
        app_settings.checked_overlay(env, {"MT5_RECORDS_TOKEN": TOKEN}, "MT5_RECORDS_TOKEN")
    assert "MT5_INGEST_TOKEN" in exc.value.detail and TOKEN not in exc.value.detail
    # the other direction: a managed ingest token equal to the records token
    env = _settings(settings, MT5_RECORDS_TOKEN=TOKEN)
    with pytest.raises(app_settings.SettingsError):
        app_settings.checked_overlay(env, {"MT5_INGEST_TOKEN": TOKEN}, "MT5_INGEST_TOKEN")
    for key in (
        "MT5_RECORDS_MAX_BATCH",
        "MT5_RECORDS_RATE_PER_MINUTE",
        "MT5_RECORDS_RETENTION_DAYS",
    ):
        assert app_settings.REGISTRY[key].kind == "int"
    merged = app_settings.checked_overlay(
        env, {"MT5_RECORDS_RETENTION_DAYS": "30"}, "MT5_RECORDS_RETENTION_DAYS"
    )
    assert merged.MT5_RECORDS_RETENTION_DAYS == 30
    assert TOKEN in app_settings.secret_values(env)  # registered with the log scrubber


# --- limits ---------------------------------------------------------------------------------


async def test_rate_limit_429_shared_by_both_routes(ctx: Ctx) -> None:
    ctx.app.state.mt5_records_limiter = FakeLimiter(allow=1)
    assert (await _post(ctx.c, RECORDS, batch([row()]))).status_code == 200
    r = await _post(ctx.c, BARS, bars_doc([bar()]))
    assert r.status_code == 429 and r.headers["retry-after"] == "7"
    assert r.json()["detail"]["code"] == "rate_limited"


async def test_rate_limit_backend_down_fails_open(ctx: Ctx) -> None:
    class Broken:
        async def acquire(self, bucket: str, max_wait: float = 30.0) -> None:
            raise ConnectionError("redis down")

    ctx.app.state.mt5_records_limiter = Broken()
    assert (await _post(ctx.c, RECORDS, batch([row()]))).status_code == 200


async def test_limiter_is_per_token_and_rebuilt_on_change(settings: Settings) -> None:
    s = _settings(settings, MT5_RECORDS_TOKEN=TOKEN)
    app = create_app(s)
    async with LifespanManager(app):
        lim = route._limiter(_Req(app), s, TOKEN)
        assert route._limiter(_Req(app), s, TOKEN) is lim
        other = route._limiter(_Req(app), s, TOKEN[::-1])
        assert other is not lim and TOKEN not in other.prefix
        assert other.buckets["push"][0].limit == s.MT5_RECORDS_RATE_PER_MINUTE


class _Req:
    def __init__(self, app: FastAPI) -> None:
        self.app = app


async def test_too_many_rows_is_422(settings: Settings) -> None:
    async for ctx in _ctx(_settings(settings, MT5_RECORDS_TOKEN=TOKEN, MT5_RECORDS_MAX_BATCH=3)):
        r = await _post(ctx.c, RECORDS, batch([row() for _ in range(4)]))
        assert r.status_code == 422 and r.json()["detail"]["fields"] == ["rows"]
        assert (await _post(ctx.c, RECORDS, batch([row() for _ in range(3)]))).status_code == 200


async def test_wrong_schema_and_bad_json_are_422(ctx: Ctx) -> None:
    r = await _post(ctx.c, RECORDS, batch([row()], schema="aureon.mt5.push/v1"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_body"
    r = await _post(ctx.c, BARS, bars_doc([bar()], schema=svc.RECORDS_SCHEMA))
    assert r.status_code == 422
    assert (await _post(ctx.c, RECORDS, b"{nope")).status_code == 422
    assert (await _post(ctx.c, RECORDS, b'{"schema": NaN}')).status_code == 422
    assert ctx.store.calls == 0


async def test_raw_and_decompressed_caps_are_413(settings: Settings) -> None:
    s = _settings(
        settings,
        MT5_RECORDS_TOKEN=TOKEN,
        MT5_INGEST_MAX_BYTES=4096,
        MT5_INGEST_MAX_DECOMPRESSED_BYTES=8192,
    )
    async for ctx in _ctx(s):
        big = json.dumps(batch([row(note="x" * 5000)])).encode()
        r = await _post(ctx.c, RECORDS, big)
        assert r.status_code == 413 and r.json()["detail"]["code"] == "too_large"
        bomb = gzip.compress(json.dumps(batch([row(note="y" * 20000)])).encode())
        assert len(bomb) < 4096
        r = await _post(ctx.c, RECORDS, bomb, **{"content-encoding": "gzip"})
        assert r.status_code == 413
        assert ctx.store.calls == 0


async def test_gzip_is_accepted(ctx: Ctx) -> None:
    body = gzip.compress(json.dumps(bars_doc([bar()])).encode())
    r = await _post(ctx.c, BARS, body, **{"content-encoding": "gzip"})
    assert r.status_code == 200, r.text
    assert r.json() == {"inserted": 1, "updated": 0, "unchanged": 0, "rejected": []}


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "text/plain"},
        {"content-type": "application/x-www-form-urlencoded"},
        {"content-type": ""},
        {"content-encoding": "br"},
    ],
)
async def test_wrong_content_type_or_encoding_is_415(ctx: Ctx, headers: dict[str, str]) -> None:
    r = await _post(ctx.c, RECORDS, batch([row()]), **headers)
    assert r.status_code == 415
    assert r.json()["detail"]["code"] in ("unsupported_media_type", "unsupported_encoding")
    utf8 = {"content-type": "application/json; charset=utf-8"}
    ok = await _post(ctx.c, RECORDS, batch([row()]), **utf8)
    assert ok.status_code == 200


async def test_mixed_batch_answers_per_row(ctx: Ctx) -> None:
    good = row()
    rows = [good, row(kind="Nope"), dict(good), row(utc_time="2099-01-01T00:00:00Z")]
    r = await _post(ctx.c, RECORDS, batch(rows))
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 1 and body["duplicate"] == 1
    assert [(x["index"], x["record_id"]) for x in body["rejected"]] == [
        (1, rows[1]["record_id"]),
        (3, rows[3]["record_id"]),
    ]


async def test_bars_route_inserts_updates_and_rejects(ctx: Ctx) -> None:
    r = await _post(ctx.c, BARS, bars_doc([bar(), bar(ts="2026-11-13T09:30:00Z", high=1, low=2)]))
    assert r.status_code == 200
    body = r.json()
    assert (body["inserted"], body["updated"], body["unchanged"]) == (1, 0, 0)
    assert body["rejected"] == [
        {"ts": "2026-11-13T09:30:00Z", "index": 1, "reason": "high is below low"}
    ]
    r = await _post(ctx.c, BARS, bars_doc([bar(close=4039.5), bar(ts="2026-11-13T09:30:00Z")]))
    assert r.json() | {"rejected": []} == {
        "inserted": 1,
        "updated": 1,
        "unchanged": 0,
        "rejected": [],
    }
    r = await _post(ctx.c, BARS, bars_doc([bar(ts="2026-11-13T09:00:00Z", close=4039.5)]))
    assert r.json()["unchanged"] == 1
    r = await _post(ctx.c, BARS, bars_doc([bar()] * 5001))
    assert r.status_code == 422


# --- exposure: Access, CSRF, UI manifest ---------------------------------------------------


def test_access_exemption_is_post_only_and_exact() -> None:
    for p in (RECORDS, BARS):
        assert is_machine_request("POST", p)
        assert not is_machine_request("GET", p)
        assert not is_machine_path(p)
        for q in (p + "/", p + "x", p + "/stats", p.upper()):
            assert not is_machine_request("POST", q), q
    assert not is_machine_request("POST", "/api/trading/mt5/records/stats")
    assert not is_machine_request("POST", "/api/trading/mt5/dataset/crosses")
    assert is_machine_request("GET", "/api/trading/mt5/ingest")  # unchanged: path-only rule


async def test_access_middleware_lets_only_the_writes_through(settings: Settings) -> None:
    seen: list[tuple[str, str]] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append((scope["method"], scope["path"]))

    s = _settings(settings, CF_ACCESS_TEAM_DOMAIN="team.example", CF_ACCESS_AUD="aud")
    mw = CloudflareAccessMiddleware(inner, s)
    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    for method, path in (("POST", RECORDS), ("GET", RECORDS), ("POST", BARS), ("GET", BARS)):
        scope = {"type": "http", "method": method, "path": path, "headers": []}
        await mw(scope, None, send)  # type: ignore[arg-type]
    assert seen == [("POST", RECORDS), ("POST", BARS)]
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [403, 403]


def test_ui_manifest_hides_the_writes_and_lists_the_reads(settings: Settings) -> None:
    ops = {(o.method, o.path): o.ui for o in ui.describe_routes(create_app(settings))}
    assert ops[("POST", RECORDS)]["danger"] == "hidden"
    assert ops[("POST", BARS)]["danger"] == "hidden"
    for path in (RECORDS, f"{RECORDS}/stats", BARS, "/api/trading/mt5/dataset/crosses"):
        meta = ops[("GET", path)]
        assert meta["danger"] == "none" and meta["module"] == "mt5-records", path


async def test_reads_need_a_session(ctx: Ctx) -> None:
    for path in (RECORDS, f"{RECORDS}/stats", f"{BARS}?symbol=XAUUSD"):
        assert (await ctx.c.get(path)).status_code == 401


# --- purge job ------------------------------------------------------------------------------


def test_purge_job_only_when_records_retention_is_configured(settings: Settings) -> None:
    def ids(**kw: Any) -> set[str]:
        return {j for j, _, _ in default_jobs(_settings(settings, **kw))}

    assert "mt5-ingest-purge" not in ids()
    assert "mt5-ingest-purge" not in ids(MT5_RECORDS_TOKEN=TOKEN)  # 0 = keep forever
    assert "mt5-ingest-purge" in ids(MT5_RECORDS_TOKEN=TOKEN, MT5_RECORDS_RETENTION_DAYS=90)
    assert "mt5-ingest-watch" not in ids(MT5_RECORDS_TOKEN=TOKEN, MT5_RECORDS_RETENTION_DAYS=90)
    assert "mt5-ingest-purge" in ids(MT5_INGEST_TOKEN=INGEST_TOKEN)  # push/v1: unchanged


def test_retention_setting_bounds(settings: Settings) -> None:
    assert settings.MT5_RECORDS_RETENTION_DAYS == 0
    assert settings.MT5_RECORDS_MAX_BATCH == 500
    assert settings.MT5_RECORDS_RATE_PER_MINUTE == 60
    with pytest.raises(ValueError):
        _settings(settings, MT5_RECORDS_RETENTION_DAYS=-1)
