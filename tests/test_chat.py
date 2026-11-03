"""Phase 3: models, SSE chat, cancellation, queue limits, user isolation (mocked Ollama)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.security import hash_password
from app.core.config import Settings
from app.main import create_app
from app.models.ai import Message, ModelInventory
from app.models.auth import User

pytestmark = pytest.mark.db

OLLAMA = "http://ollama.test:11434"
MODEL = "qwen2.5:7b"
PASSWORD = "correct horse battery staple"
TAGS = {
    "models": [
        {
            "name": MODEL,
            "model": MODEL,
            "modified_at": "2026-10-01T10:00:00.123456789+05:30",
            "size": 4_683_087_332,
            "digest": "abc123",
            "details": {
                "family": "qwen2",
                "parameter_size": "7.6B",
                "quantization_level": "Q4_K_M",
            },
        },
        {"name": "mistral:7b", "model": "mistral:7b", "size": 4_100_000_000, "details": {}},
    ]
}


def ndjson(*chunks: dict[str, Any]) -> bytes:
    return b"".join(json.dumps(c).encode() + b"\n" for c in chunks)


def tok(text: str) -> dict[str, Any]:
    return {"model": MODEL, "message": {"role": "assistant", "content": text}, "done": False}


DONE = {
    "model": MODEL,
    "message": {"role": "assistant", "content": ""},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 12,
    "eval_count": 2,
}


class GatedStream(httpx.AsyncByteStream):
    """Upstream body that sends `first`, then waits for `gate` (or forever)."""

    def __init__(
        self,
        first: bytes,
        rest: bytes = b"",
        gate: asyncio.Event | None = None,
        delay: float | None = None,
    ) -> None:
        self.first, self.rest = first, rest
        self.gate = gate or asyncio.Event()
        self.delay = delay
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.delay is not None:
            await asyncio.sleep(self.delay)
        if self.first:
            yield self.first
        await self.gate.wait()
        if self.rest:
            yield self.rest

    async def aclose(self) -> None:
        self.closed.set()


def parse_sse(raw: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(":"):
            events.append(("ping", None))
            continue
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


async def _make_user(url: str, email: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(
            User.__table__.insert().values(
                email=email, password_hash=hash_password(PASSWORD), role_id=2
            )
        )
    await engine.dispose()


class Env:
    def __init__(
        self, app: FastAPI, a: AsyncClient, b: AsyncClient, csrf_a: str, csrf_b: str, db_url: str
    ) -> None:
        self.app, self.a, self.b, self.csrf_a, self.csrf_b = app, a, b, csrf_a, csrf_b
        self.db_url = db_url

    async def messages(self) -> list[Message]:
        engine = create_async_engine(self.db_url)
        async with engine.connect() as conn:
            rows = (await conn.execute(select(Message).order_by(Message.id))).all()
        await engine.dispose()
        return [Message(**r._mapping) for r in rows]

    def chat(self, client: str = "a", **body: Any) -> Any:
        c, csrf = (self.a, self.csrf_a) if client == "a" else (self.b, self.csrf_b)
        payload = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], **body}
        return c.post("/api/chat", json=payload, headers={"x-csrf-token": csrf})


EnvFactory = Callable[..., contextlib.AbstractAsyncContextManager[Env]]


@pytest.fixture
def make_env(db_clean: str, settings: Settings) -> EnvFactory:
    @contextlib.asynccontextmanager
    async def factory(**overrides: Any) -> AsyncIterator[Env]:
        await _make_user(db_clean, "a@spasta.online")
        await _make_user(db_clean, "b@spasta.online")
        s = Settings.model_validate({**settings.model_dump(), **overrides})
        app = create_app(s)
        async with LifespanManager(app) as mgr:
            t = ASGITransport(app=mgr.app)
            async with (
                AsyncClient(transport=t, base_url="https://test") as a,
                AsyncClient(transport=t, base_url="https://test") as b,
            ):
                csrf = []
                for c, email in ((a, "a@spasta.online"), (b, "b@spasta.online")):
                    r = await c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
                    csrf.append(r.json()["csrf_token"])
                yield Env(app, a, b, csrf[0], csrf[1], db_clean)

    return factory


# --- models -------------------------------------------------------------------------------


@respx.mock
async def test_models_list_cached_and_inventory_synced(make_env: EnvFactory) -> None:
    tags = respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    async with make_env() as env:
        r1 = await env.a.get("/api/models")
        r2 = await env.a.get("/api/models")
        assert r1.status_code == r2.status_code == 200
        assert [m["name"] for m in r1.json()["models"]] == [MODEL, "mistral:7b"]
        assert r1.json()["models"][0]["quantization"] == "Q4_K_M"
        assert tags.call_count == 1  # 60 s cache
        engine = create_async_engine(env.db_url)
        async with engine.connect() as conn:
            names = (await conn.execute(select(ModelInventory.name))).scalars().all()
        await engine.dispose()
        assert sorted(names) == sorted([MODEL, "mistral:7b"])


@respx.mock
async def test_models_require_auth_and_report_unreachable(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    async with make_env() as env:
        anon = AsyncClient(transport=ASGITransport(app=env.app), base_url="https://test")
        assert (await anon.get("/api/models")).status_code == 401
        assert (await env.a.get("/api/models")).status_code == 503
        status = await env.a.get("/api/models/status")
        assert status.status_code == 200
        assert status.json() == {"reachable": False, "models": []}


@respx.mock
async def test_models_status_loaded_and_last_used(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    respx.get(f"{OLLAMA}/api/ps").mock(
        return_value=httpx.Response(
            200,
            json={
                "models": [{"name": MODEL, "size_vram": 0, "expires_at": "2026-11-02T10:05:00Z"}]
            },
        )
    )
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, content=ndjson(tok("ok"), DONE))
    )
    async with make_env() as env:
        assert (await env.chat()).status_code == 200
        body = (await env.a.get("/api/models/status")).json()
        assert body["reachable"] is True
        by_name = {m["name"]: m for m in body["models"]}
        assert by_name[MODEL]["loaded"] is True and by_name[MODEL]["last_used_at"]
        assert by_name["mistral:7b"]["loaded"] is False
        assert by_name["mistral:7b"]["last_used_at"] is None


# --- streaming ----------------------------------------------------------------------------


@respx.mock
async def test_chat_streams_and_persists_both_turns(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    chat = respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, content=ndjson(tok("Hel"), tok("lo"), DONE))
    )
    async with make_env() as env:
        r = await env.chat(
            messages=[
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "Say hello"},
            ],
            temperature=0.2,
            num_ctx=2048,
            max_tokens=64,
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["x-accel-buffering"] == "no"
        events = parse_sse(r.text)
        assert [e[0] for e in events] == ["meta", "start", "token", "token", "done"]
        assert "".join(e[1]["content"] for e in events if e[0] == "token") == "Hello"
        assert events[-1][1]["completion_tokens"] == 2
        sent = json.loads(chat.calls[0].request.content)
        assert sent["stream"] is True
        assert sent["options"] == {"num_ctx": 2048, "temperature": 0.2, "num_predict": 64}

        conv_id = events[0][1]["conversation_id"]
        msgs = await env.messages()
        assert [(m.role, m.content, m.status) for m in msgs] == [
            ("system", "be brief", "complete"),
            ("user", "Say hello", "complete"),
            ("assistant", "Hello", "complete"),
        ]

        # Second turn: server prepends stored history.
        r2 = await env.chat(
            conversation_id=conv_id, messages=[{"role": "user", "content": "again"}]
        )
        assert r2.status_code == 200
        sent2 = json.loads(chat.calls[1].request.content)
        assert [m["content"] for m in sent2["messages"]] == [
            "be brief",
            "Say hello",
            "Hello",
            "again",
        ]

        detail = (await env.a.get(f"/api/conversations/{conv_id}")).json()
        assert detail["title"] == "Say hello"
        assert len(detail["messages"]) == 5


@respx.mock
async def test_missing_model_is_clear_404(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    async with make_env() as env:
        r = await env.chat(model="llama3:70b")
        assert r.status_code == 404
        assert r.json()["available"] == [MODEL, "mistral:7b"]
        assert await env.messages() == []


@respx.mock
async def test_model_load_failure_reported_and_stored(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(
            500, json={"error": "llama runner process has terminated: signal: killed"}
        )
    )
    async with make_env() as env:
        events = parse_sse((await env.chat()).text)
        assert events[-1][0] == "error"
        assert events[-1][1]["code"] == "model_error"
        assert "runner process" in events[-1][1]["message"]
        assert (await env.messages())[-1].status == "error"
        # A failed turn is not fed back into the next prompt.
        assert env.app.state.chat_limiter.admitted == 0


@respx.mock
async def test_timeout_with_heartbeats(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    upstream = GatedStream(ndjson(tok("partial")))
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(200, stream=upstream))
    async with make_env(CHAT_TIMEOUT_SECONDS=0.6, CHAT_HEARTBEAT_SECONDS=0.1) as env:
        events = parse_sse((await env.chat()).text)
        kinds = [e[0] for e in events]
        assert "ping" in kinds
        assert kinds[-1] == "error" and events[-1][1]["code"] == "timeout"
        last = (await env.messages())[-1]
        assert (last.status, last.content) == ("timeout", "partial")
        assert upstream.closed.is_set()  # Ollama connection closed => generation stops


# --- cancellation via client disconnect ---------------------------------------------------


async def _raw_chat_then_disconnect(env: Env, spec_version: str) -> list[dict[str, Any]]:
    """Drive the ASGI app directly so we can disconnect mid-stream."""
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "long"}]})
    cookie = "; ".join(f"{k}={v}" for k, v in env.a.cookies.items())
    headers = [
        (b"content-type", b"application/json"),
        (b"cookie", cookie.encode()),
        (b"x-csrf-token", env.csrf_a.encode()),
        (b"content-length", str(len(body)).encode()),
    ]
    got_token = asyncio.Event()
    sent: list[dict[str, Any]] = []
    first = True

    async def receive() -> dict[str, Any]:
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": body.encode(), "more_body": False}
        await got_token.wait()
        return {"type": "http.disconnect"}

    async def send(msg: dict[str, Any]) -> None:
        if spec_version == "2.4" and got_token.is_set() and msg["type"] == "http.response.body":
            raise OSError("client went away")
        sent.append(msg)
        if b"event: token" in msg.get("body", b""):
            got_token.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/chat",
        "raw_path": b"/api/chat",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("test", 443),
        "state": {},
    }
    with contextlib.suppress(Exception):
        await asyncio.wait_for(env.app(scope, receive, send), timeout=10)
    return sent


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@respx.mock
async def test_client_disconnect_cancels_generation(
    make_env: EnvFactory, spec_version: str
) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    upstream = GatedStream(ndjson(tok("Once upon")), ndjson(tok(" a time"), DONE))
    respx.post(f"{OLLAMA}/api/chat").mock(return_value=httpx.Response(200, stream=upstream))
    async with make_env(CHAT_HEARTBEAT_SECONDS=0.05) as env:
        await _raw_chat_then_disconnect(env, spec_version)
        await asyncio.wait_for(upstream.closed.wait(), 5)
        last = (await env.messages())[-1]
        assert (last.role, last.status, last.content) == ("assistant", "cancelled", "Once upon")
        assert env.app.state.chat_limiter.admitted == 0
        assert env.app.state.chat_limiter.running == 0


# --- concurrency --------------------------------------------------------------------------


@respx.mock
async def test_queue_full_returns_429_then_recovers(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    gate, upstream_started = asyncio.Event(), asyncio.Event()

    def chat_side_effect(request: httpx.Request) -> httpx.Response:
        upstream_started.set()
        return httpx.Response(200, stream=GatedStream(ndjson(tok("a")), ndjson(DONE), gate))

    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=chat_side_effect)
    async with make_env(
        OLLAMA_MAX_CONCURRENT=1, OLLAMA_MAX_QUEUE=0, OLLAMA_QUEUE_RETRY_AFTER=7
    ) as env:
        first = asyncio.create_task(env.chat())
        await asyncio.wait_for(upstream_started.wait(), 5)
        busy = await env.chat(client="b")
        assert busy.status_code == 429
        assert busy.headers["retry-after"] == "7"
        gate.set()
        assert parse_sse((await first).text)[-1][0] == "done"
        assert env.app.state.chat_limiter.admitted == 0
        assert (await env.chat(client="b")).status_code == 200


@respx.mock
async def test_second_request_waits_in_queue(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    gate, started = asyncio.Event(), asyncio.Event()
    calls = 0

    def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.set()
        g = gate if calls == 1 else asyncio.Event()
        if calls > 1:
            g.set()
        return httpx.Response(200, stream=GatedStream(ndjson(tok("x")), ndjson(DONE), g))

    respx.post(f"{OLLAMA}/api/chat").mock(side_effect=side_effect)
    async with make_env(OLLAMA_MAX_CONCURRENT=1, OLLAMA_MAX_QUEUE=1) as env:
        first = asyncio.create_task(env.chat())
        await asyncio.wait_for(started.wait(), 5)
        second = asyncio.create_task(env.chat(client="b"))
        await asyncio.sleep(0.2)
        assert calls == 1  # second is queued, not sent to Ollama
        gate.set()
        events = parse_sse((await second).text)
        assert [e[0] for e in events][:2] == ["meta", "queued"]
        assert events[-1][0] == "done"
        await first


# --- CSRF / isolation ---------------------------------------------------------------------


@respx.mock
async def test_chat_requires_csrf(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    async with make_env() as env:
        r = await env.a.post(
            "/api/chat", json={"model": MODEL, "messages": [{"role": "user", "content": "x"}]}
        )
        assert r.status_code == 403


@respx.mock
async def test_num_ctx_cap_and_last_message_rule(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    async with make_env(CHAT_MAX_NUM_CTX=4096) as env:
        assert (await env.chat(num_ctx=8192)).status_code == 422
        r = await env.chat(messages=[{"role": "system", "content": "only system"}])
        assert r.status_code == 422


@respx.mock
async def test_user_isolation(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    respx.post(f"{OLLAMA}/api/chat").mock(
        return_value=httpx.Response(200, content=ndjson(tok("secret"), DONE))
    )
    async with make_env() as env:
        conv_id = parse_sse((await env.chat()).text)[0][1]["conversation_id"]

        assert (await env.a.get("/api/conversations")).json()["total"] == 1
        assert (await env.b.get("/api/conversations")).json() == {
            "items": [],
            "total": 0,
            "limit": 20,
            "offset": 0,
        }
        assert (await env.b.get(f"/api/conversations/{conv_id}")).status_code == 404
        r = await env.b.delete(
            f"/api/conversations/{conv_id}", headers={"x-csrf-token": env.csrf_b}
        )
        assert r.status_code == 404
        hijack = await env.chat(client="b", conversation_id=conv_id)
        assert hijack.status_code == 404
        assert env.app.state.chat_limiter.admitted == 0

        # Owner can delete; messages cascade.
        r = await env.a.delete(
            f"/api/conversations/{conv_id}", headers={"x-csrf-token": env.csrf_a}
        )
        assert r.status_code == 204
        assert await env.messages() == []


@respx.mock
async def test_default_model_first_keep_alive_threads_and_trimmed_history(
    make_env: EnvFactory,
) -> None:
    """The CPU tuning knobs: default model listed first (the SPA preselects it), keep_alive
    and num_thread sent to Ollama, and only the newest stored turns re-sent."""
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    chat = respx.post(f"{OLLAMA}/api/chat").mock(
        side_effect=lambda req: httpx.Response(200, content=ndjson(tok("ok"), DONE))
    )
    async with make_env(
        CHAT_DEFAULT_MODEL="mistral:7b",
        CHAT_WARMUP=False,
        OLLAMA_KEEP_ALIVE="24h",
        CHAT_NUM_THREAD=4,
        CHAT_HISTORY_MAX_MESSAGES=2,
    ) as env:
        r = await env.a.get("/api/models")
        assert r.json()["default_model"] == "mistral:7b"
        assert [m["name"] for m in r.json()["models"]] == ["mistral:7b", MODEL]

        r = await env.chat(
            messages=[
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "one"},
            ]
        )
        conv = parse_sse(r.text)[0][1]["conversation_id"]
        await env.chat(conversation_id=conv, messages=[{"role": "user", "content": "two"}])
        await env.chat(conversation_id=conv, messages=[{"role": "user", "content": "three"}])
        sent = json.loads(chat.calls[-1].request.content)
        assert sent["keep_alive"] == "24h"
        assert sent["options"] == {"num_ctx": 4096, "num_thread": 4}
        # Stored: system, one, ok, two, ok. Re-sent: the system message + the newest 2.
        assert [m["content"] for m in sent["messages"]] == ["be brief", "two", "ok", "three"]
        assert len(await env.messages()) == 7  # everything is still stored


@respx.mock
async def test_unknown_default_model_is_ignored(make_env: EnvFactory) -> None:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    async with make_env(CHAT_DEFAULT_MODEL="qwen2.5:3b", CHAT_WARMUP=False) as env:
        r = await env.a.get("/api/models")
        assert r.json()["default_model"] is None
        assert [m["name"] for m in r.json()["models"]] == [MODEL, "mistral:7b"]
