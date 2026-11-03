"""MT5 push ingest without a database: auth, body caps, schema, idempotency at the route,
failure classification, stale detection and the exact-path Access/CSRF exemption."""

from __future__ import annotations

import gzip
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.agents.trading.mt5.push import (
    BodyTooLarge,
    BodyUnsupported,
    PushInvalid,
    decode_body,
    parse_push,
)
from app.agents.trading.ratelimit import RateLimitExceeded
from app.api.v1 import mt5_push
from app.core.cf_access import CloudflareAccessMiddleware, is_machine_path
from app.core.config import Environment, Settings
from app.core.prod_checks import production_problems
from app.main import create_app
from app.services.mt5_ingest import (
    EventRow,
    IngestResult,
    classify,
    event_kind,
    event_rows,
    heartbeat_state,
    stale_heartbeat,
)
from app.workers.scheduler import default_jobs
from tests.mt5_push_fixture import TOKEN, push_doc
from tests.routes import api_routes

URL = "/api/trading/mt5/ingest"


class FakeStore:
    def __init__(self) -> None:
        self.push_ids: set[uuid.UUID] = set()
        self.event_ids: set[str] = set()
        self.calls = 0

    async def store(self, push: Any, size_bytes: int, settings: Settings) -> IngestResult:
        self.calls += 1
        if push.push_id in self.push_ids:
            return IngestResult(duplicate=True, push_id=push.push_id)
        self.push_ids.add(push.push_id)
        rows = event_rows(push, datetime.now(UTC))
        new = [r for r in rows if r.id not in self.event_ids]
        self.event_ids.update(r.id for r in new)
        return IngestResult(False, push.push_id, len(new), len(rows) - len(new))


class FakeLimiter:
    def __init__(self, allow: int = 1000) -> None:
        self.allow = allow

    async def acquire(self, bucket: str, max_wait: float = 30.0) -> None:
        self.allow -= 1
        if self.allow < 0:
            raise RateLimitExceeded(bucket, 12.0)


def _settings(settings: Settings, **kw: Any) -> Settings:
    return Settings.model_validate({**settings.model_dump(), **kw})


@pytest.fixture
def ingest_settings(settings: Settings) -> Settings:
    return _settings(settings, MT5_INGEST_TOKEN=TOKEN)


class Ctx:
    def __init__(self, app: FastAPI, client: AsyncClient, store: FakeStore) -> None:
        self.app, self.c, self.store = app, client, store


async def _ctx(s: Settings) -> AsyncIterator[Ctx]:
    app = create_app(s)
    async with LifespanManager(app) as mgr:
        store = FakeStore()
        app.state.mt5_ingest_store = store
        app.state.mt5_ingest_limiter = FakeLimiter()
        async with AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c:
            yield Ctx(app, c, store)


@pytest.fixture
async def ctx(ingest_settings: Settings) -> AsyncIterator[Ctx]:
    async for x in _ctx(ingest_settings):
        yield x


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "content-type": "application/json"}


async def _post(c: AsyncClient, doc: dict[str, Any] | bytes, **headers: str) -> Any:
    body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
    return await c.post(URL, content=body, headers={**_auth(), **headers})


# --- token auth ---------------------------------------------------------------------------


async def test_good_token_is_accepted(ctx: Ctx) -> None:
    d = push_doc()
    r = await _post(ctx.c, d, **{"x-aureon-push-id": d["push_id"]})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body == {
        "accepted": True,
        "push_id": d["push_id"],
        "events_new": len(d["events"]),
        "events_duplicate": 0,
    }


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
    ],
)
async def test_missing_or_bad_token_is_401(ctx: Ctx, headers: dict[str, str]) -> None:
    r = await ctx.c.post(URL, content=json.dumps(push_doc()).encode(), headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in r.text
    assert ctx.store.calls == 0


async def test_token_compare_is_constant_time(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = mt5_push.hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(mt5_push.hmac, "compare_digest", spy)
    # compared even for a wrong scheme, so the scheme check leaks nothing about the token
    for h in (f"Basic {TOKEN}", "Bearer nope", f"Bearer {TOKEN}"):
        await ctx.c.post(URL, content=b"{}", headers={"authorization": h})
    assert len(calls) == 3
    assert all(isinstance(a, bytes) and b == TOKEN.encode() for a, b in calls)


async def test_ingest_disabled_without_token_is_503(settings: Settings) -> None:
    async for ctx in _ctx(settings):
        r = await _post(ctx.c, push_doc())
        assert r.status_code == 503
        assert r.json()["detail"]["code"] == "ingest_disabled"


async def test_no_session_or_csrf_needed(ctx: Ctx) -> None:
    # no cookies, no x-csrf-token: the bearer token alone is enough
    r = await _post(ctx.c, push_doc())
    assert r.status_code == 202
    assert not ctx.c.cookies


async def test_rate_limit_429(ctx: Ctx) -> None:
    ctx.app.state.mt5_ingest_limiter = FakeLimiter(allow=1)
    assert (await _post(ctx.c, push_doc())).status_code == 202
    r = await _post(ctx.c, push_doc())
    assert r.status_code == 429
    assert r.headers["retry-after"] == "12"


async def test_rate_limit_backend_down_fails_open(ctx: Ctx) -> None:
    class Broken:
        async def acquire(self, bucket: str, max_wait: float = 30.0) -> None:
            raise ConnectionError("redis down")

    ctx.app.state.mt5_ingest_limiter = Broken()
    assert (await _post(ctx.c, push_doc())).status_code == 202


# --- 413 / 415 caps -----------------------------------------------------------------------


async def test_raw_body_over_2mib_is_413(ctx: Ctx) -> None:
    big = push_doc(console={"pad": "x" * (2 * 1024 * 1024)})
    r = await _post(ctx.c, big)
    assert r.status_code == 413
    assert ctx.store.calls == 0


async def test_declared_content_length_over_cap_is_413_before_reading(ctx: Ctx) -> None:
    r = await ctx.c.post(
        URL,
        content=b"{}",
        headers={**_auth(), "content-length": str(3 * 1024 * 1024)},
    )
    assert r.status_code == 413


async def test_gzip_body_is_accepted(ctx: Ctx) -> None:
    d = push_doc()
    r = await _post(ctx.c, gzip.compress(json.dumps(d).encode()), **{"content-encoding": "gzip"})
    assert r.status_code == 202, r.text


async def test_gzip_bomb_is_413(ctx: Ctx) -> None:
    # ~9 MiB of JSON whitespace compresses to a few KiB: under the raw cap, over the 8 MiB one
    d = json.dumps(push_doc()).encode()
    bomb = gzip.compress(d[:-1] + b" " * (9 * 1024 * 1024) + b"}")
    assert len(bomb) < 64 * 1024
    r = await _post(ctx.c, bomb, **{"content-encoding": "gzip"})
    assert r.status_code == 413
    assert ctx.store.calls == 0


async def test_unknown_encoding_is_415_and_bad_gzip_422(ctx: Ctx) -> None:
    r = await _post(ctx.c, b"xx", **{"content-encoding": "br"})
    assert r.status_code == 415
    r = await _post(ctx.c, b"not gzip at all", **{"content-encoding": "gzip"})
    assert r.status_code == 422


def test_decode_body_caps_output_not_input() -> None:
    blob = gzip.compress(b"a" * 5000)
    assert decode_body(blob, "gzip", 5000) == b"a" * 5000
    with pytest.raises(BodyTooLarge):
        decode_body(blob, "gzip", 4999)
    with pytest.raises(BodyTooLarge):
        decode_body(b"a" * 10, None, 9)
    with pytest.raises(BodyUnsupported):
        decode_body(b"", "deflate", 10)
    with pytest.raises(PushInvalid):
        decode_body(blob[:-8], "gzip", 10_000)  # truncated


# --- schema 422 + idempotency ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"schema": "aureon.mt5.status/v1"}, "schema"),
        ({"push_id": "not-a-uuid"}, "push_id"),
        ({"generated_at": "yesterday"}, "generated_at"),
        ({"bot": None}, "bot"),
        ({"guardian": {"XAUUSD": [{"phase": "ride"}]}}, "guardian.XAUUSD.0.ticket"),
        ({"events": [{"id": 5, "event": "x", "t": 1}]}, "events"),
    ],
)
async def test_bad_schema_is_422(ctx: Ctx, change: dict[str, Any], field: str) -> None:
    r = await _post(ctx.c, push_doc(**change))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "invalid_push"
    assert field in detail["fields"]
    assert ctx.store.calls == 0


async def test_not_json_and_header_mismatch_are_422(ctx: Ctx) -> None:
    assert (await _post(ctx.c, b"{nope")).status_code == 422
    d = push_doc()
    r = await _post(ctx.c, d, **{"x-aureon-push-id": str(uuid.uuid4())})
    assert r.status_code == 422
    assert r.json()["detail"]["fields"] == ["push_id"]


async def test_duplicate_push_id_answers_200_duplicate(ctx: Ctx) -> None:
    d = push_doc()
    assert (await _post(ctx.c, d)).status_code == 202
    r = await _post(ctx.c, d)
    assert r.status_code == 200
    assert r.json() == {"duplicate": True, "push_id": d["push_id"]}


async def test_duplicate_event_ids_are_counted_not_stored_twice(ctx: Ctx) -> None:
    d1 = push_doc()
    await _post(ctx.c, d1)
    d2 = push_doc()  # new push_id, same event ids (the bot re-sends unacked events)
    d2["events"].append({"id": "brand-new", "t": 1793612800, "event": "trail_update"})
    r = await _post(ctx.c, d2)
    assert r.json()["events_new"] == 1
    assert r.json()["events_duplicate"] == len(d1["events"])


def test_lenient_on_bot_pr38_shapes() -> None:
    d = push_doc(
        health=None,
        tracebacks=[{"id": "tb1", "where": "agent:XAUUSD", "message": None}],
        signals_latest={"XAUUSD": {"locked": {"a": 1}, "pre_cross": None, "weird": 5}},
        events=[
            {"id": "a" * 40, "t": 1, "event": "entry_blocked_htf", "extra": {"x": [1]}},
            {"id": "b" * 40, "t": 2, "event": "agent_crash", "symbol": "XAUUSD"},
        ],
        errors={"candles": "trimmed 120 bars", "events": "held back 40 events"},
        strategy={"ema": {"fast": 50, "slow": 80}, "params": {"guardian": {"XAUUSD": {}}}},
        unknown_top_level={"ok": True},
    )
    d["candles"]["XAUUSD"]["M5"][0]["ema"]["x"] = "n/a"
    p = parse_push(json.dumps(d).encode())
    assert p.health.mt5.connected is None
    assert p.guardian["XAUUSD"][0].leg_phase == "B"
    kinds = {r.id: r.kind for r in event_rows(p, datetime.now(UTC))}
    assert kinds["a" * 40] == "entry_blocked_htf"
    assert kinds["b" * 40] == "error"
    assert kinds["tb1"] == "error"


def test_candles_over_300_keep_the_newest() -> None:
    d = push_doc()
    d["candles"]["XAUUSD"]["M5"] = [
        {"t": i * 300, "o": 1, "h": 2, "l": 1, "c": 1.5} for i in range(350)
    ]
    p = parse_push(json.dumps(d).encode())
    bars = p.candles["XAUUSD"]["M5"]
    assert len(bars) == 300 and bars[-1].t == 349 * 300


# --- classification -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ev", "kind"),
    [
        ({"event": "signal", "signal_kind": "cross"}, "cross"),
        ({"event": "signal", "signal_kind": "P"}, "P"),
        ({"event": "signal"}, "cross"),
        ({"event": "signal", "signal_kind": "reentry"}, "reentry"),
        ({"event": "protected"}, "protected"),
        ({"event": "secured"}, "secured"),
        ({"event": "exit"}, "exit"),
        ({"event": "closed_by_rule"}, "closed"),
        ({"event": "closed"}, "closed"),
        ({"event": "error"}, "error"),
        ({"event": "agent_crash"}, "error"),
        ({"event": "soft_lock_exit"}, "soft_lock_exit"),
        ({"event": "position_seen"}, "position_seen"),
    ],
)
def test_event_kind(ev: dict[str, Any], kind: str) -> None:
    assert event_kind(ev) == kind


def _classify(settings: Settings, **changes: Any) -> list[tuple[str, str | None, str]]:
    p = parse_push(json.dumps(push_doc(**changes)).encode())
    rows = event_rows(p, datetime.now(UTC))
    return [(f.kind, f.symbol, f.reason) for f in classify(p, rows, settings)]


def test_healthy_push_has_only_the_journal_error(settings: Settings) -> None:
    # the fixture journal has one `error` event (XAGUSD tick timeout)
    assert _classify(settings) == [("exception", "XAGUSD", "tick timeout")]


def test_classification_matches_the_contract(settings: Settings) -> None:
    d = push_doc()
    d["events"] = []
    d["health"]["mt5"] = {"connected": False, "terminal": "DISCONNECTED", "last_error": "IPC"}
    d["health"]["discord"] = {"ok": False, "last_error": "429 Too Many Requests"}
    d["health"]["market"]["XAUUSD"]["tick_age_s"] = 300
    d["health"]["market"]["XAGUSD"] = {"open": False, "tick_age_s": 9999}
    d["agents"]["XAGUSD"]["connected"] = False
    d["tracebacks"] = [
        {"id": "1", "t": 1, "where": "guardian", "type": "KeyError", "message": "'sl'"},
        {"id": "2", "t": 1, "where": "discord", "type": "HTTPError", "message": "503"},
        {"id": "3", "t": 1, "where": "agent:XAUUSD", "type": "ValueError", "message": "bad"},
        {"id": "4", "t": 1, "where": "main", "type": "RuntimeError", "message": "boom"},
    ]
    d["events"] = [
        {"id": "e1", "t": 1, "event": "error", "where": "guardian", "error": "modify failed"},
    ]
    out = _classify(settings, **d)
    kinds = [(k, s) for k, s, _ in out]
    assert ("mt5_disconnected", None) in kinds
    assert ("mt5_disconnected", "XAGUSD") in kinds
    assert ("stale_ticks", "XAUUSD") in kinds
    assert ("stale_ticks", "XAGUSD") not in kinds  # market closed
    assert ("discord_error", None) in kinds
    assert kinds.count(("guardian_error", None)) == 2  # traceback + error event
    assert ("exception", "XAUUSD") in kinds
    assert ("exception", None) in kinds
    reasons = {(k, s): r for k, s, r in out}
    assert "IPC" in reasons[("mt5_disconnected", None)]
    assert "300 s old" in reasons[("stale_ticks", "XAUUSD")]
    discord = [r for k, _, r in out if k == "discord_error"]
    assert any("429" in r for r in discord) and any("HTTPError: 503" in r for r in discord)
    guardian = [r for k, _, r in out if k == "guardian_error"]
    assert any("KeyError" in r for r in guardian) and "modify failed" in guardian


def test_only_new_events_are_classified(settings: Settings) -> None:
    p = parse_push(json.dumps(push_doc()).encode())
    assert classify(p, [], settings) == []
    tb = EventRow("x", datetime.now(UTC), "traceback", "error", None, None, {"where": "main"})
    assert [f.kind for f in classify(p, [tb], settings)] == ["exception"]


# --- stale detection ----------------------------------------------------------------------


def test_stale_heartbeat_and_card_state(settings: Settings) -> None:
    now = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    assert stale_heartbeat(None, now, settings) is None
    assert stale_heartbeat(now - timedelta(seconds=90), now, settings) is None
    f = stale_heartbeat(now - timedelta(seconds=91), now, settings)
    assert f is not None and f.kind == "stale_heartbeat" and "91 s" in f.reason
    kw: dict[str, Any] = {"health_status": "ok", "v1_ok": True, "open_failures": 0}
    assert heartbeat_state(None, now, settings, **kw) == ("stale", ["no push received yet"])
    assert heartbeat_state(now - timedelta(seconds=10), now, settings, **kw) == ("online", [])
    assert heartbeat_state(now - timedelta(seconds=200), now, settings, **kw)[0] == "stale"
    kw["health_status"] = "error"
    assert heartbeat_state(now, now, settings, **kw)[0] == "error"
    kw.update(health_status="ok", v1_ok=False)
    assert heartbeat_state(now, now, settings, **kw)[0] == "error"
    s2 = _settings(settings, MT5_STALE_HEARTBEAT_SECONDS=300)
    assert stale_heartbeat(now - timedelta(seconds=200), now, s2) is None


# --- exemption scope, prod checks, jobs ------------------------------------------------------


def test_access_and_csrf_exemption_is_exactly_the_ingest_path(settings: Settings) -> None:
    assert is_machine_path("/api/trading/mt5/ingest")
    for p in (
        "/api/trading/mt5/ingest/",
        "/api/trading/mt5/ingest/x",
        "/api/trading/mt5/ingestx",
        "/api/trading/mt5",
        "/api/trading/mt5/status",
        "/api/trading/mt5/bot-status",
        "/API/trading/mt5/ingest",
    ):
        assert not is_machine_path(p), p
    # Only the ingest route lacks both a session and CSRF among the /api/trading routes.
    no_session = sorted(
        (m, r.path)
        for r in api_routes(create_app(settings))
        if r.path.startswith("/api/trading")
        for m in r.methods
        if "get_current" not in r.deps
    )
    assert no_session == [("POST", "/api/trading/mt5/ingest")]


async def test_access_middleware_lets_only_ingest_through(settings: Settings) -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["path"])

    s = _settings(settings, CF_ACCESS_TEAM_DOMAIN="team.example", CF_ACCESS_AUD="aud")
    mw = CloudflareAccessMiddleware(inner, s)
    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    for path in ("/api/trading/mt5/ingest", "/api/trading/mt5/status", "/api/trading/mt5/ingest/x"):
        await mw({"type": "http", "path": path, "headers": []}, None, send)  # type: ignore[arg-type]
    assert seen == ["/api/trading/mt5/ingest"]
    assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [403, 403]


def test_prod_check_rejects_short_ingest_token(settings: Settings) -> None:
    s = _settings(settings, ENV=Environment.PROD)
    s.MT5_INGEST_TOKEN = SecretStr("short")
    assert any("MT5_INGEST_TOKEN" in p for p in production_problems(s))
    s.MT5_INGEST_TOKEN = SecretStr(TOKEN)
    assert not any("MT5_INGEST_TOKEN" in p for p in production_problems(s))
    s.MT5_INGEST_TOKEN = None
    assert not any("MT5_INGEST_TOKEN" in p for p in production_problems(s))


def test_watch_and_purge_jobs_only_when_ingest_enabled(settings: Settings) -> None:
    ids = {j for j, _, _ in default_jobs(settings)}
    assert "mt5-ingest-watch" not in ids
    ids = {j for j, _, _ in default_jobs(_settings(settings, MT5_INGEST_TOKEN=TOKEN))}
    assert {"mt5-ingest-watch", "mt5-ingest-purge"} <= ids
