"""OpenRouter routing through POST /api/chat (docs/25-openrouter.md), end to end with the DB:
default provider, the filter forcing local, fallbacks on errors and the cap, the catalog and
usage endpoints, the audit row (metadata only), and the key never leaving the server."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.ai import Conversation
from app.services.ai_usage import day_key, utc_today
from tests.conftest import TEST_REDIS_URL
from tests.test_chat import (
    DONE,
    MODEL,
    OLLAMA,
    TAGS,
    Env,
    EnvFactory,
    make_env,
    ndjson,
    parse_sse,
    tok,
)

pytestmark = pytest.mark.db
FIXTURES = (make_env,)  # the shared fixture, re-exported for the tests below

BASE = "https://openrouter.test/api/v1"
KEY = "sk-or-v1-" + "0a1b2c3d4e5f6a7b8c9d" * 3
OR_MODEL = "google/gemini-2.5-flash-lite"
OR_ENV: dict[str, Any] = {
    "OPENROUTER_ENABLED": True,
    "OPENROUTER_API_KEY": KEY,
    "OPENROUTER_BASE_URL": BASE,
    "OPENROUTER_DEFAULT_MODEL": OR_MODEL,
    "OPENROUTER_ALLOWED_MODELS": "openai/gpt-4.1-nano",
    "OPENROUTER_DAILY_LIMIT_USD": "0.25",
    "CHAT_DEFAULT_MODEL": MODEL,
    "CHAT_WARMUP": False,
}


def or_sse(*pieces: str, cost: float = 0.00002) -> bytes:
    out = [": OPENROUTER PROCESSING\n\n"]
    for p in pieces:
        chunk = {"model": OR_MODEL, "choices": [{"delta": {"content": p}}]}
        out.append(f"data: {json.dumps(chunk)}\n\n")
    usage = {"prompt_tokens": 9, "completion_tokens": len(pieces), "cost": cost}
    final = {"model": OR_MODEL, "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage}
    out.append(f"data: {json.dumps(final)}\n\ndata: [DONE]\n\n")
    return "".join(out).encode()


def post(env: Env, **body: Any) -> Any:
    payload = {"messages": [{"role": "user", "content": "write a haiku"}], **body}
    return env.a.post("/api/chat", json=payload, headers={"x-csrf-token": env.csrf_a})


def mock_ollama() -> Any:
    respx.get(f"{OLLAMA}/api/tags").mock(return_value=httpx.Response(200, json=TAGS))
    return respx.post(f"{OLLAMA}/api/chat").mock(
        side_effect=lambda req: httpx.Response(200, content=ndjson(tok("lo"), tok("cal"), DONE))
    )


async def audit_rows(env: Env) -> list[dict[str, Any]]:
    engine = create_async_engine(env.db_url)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT details FROM audit.audit_events WHERE event_type = 'ai.external_call'"
                    " ORDER BY id"
                )
            )
        ).all()
        convs = (await conn.execute(select(Conversation))).all()
    await engine.dispose()
    env.convs = [c._mapping for c in convs]  # type: ignore[attr-defined]
    return [r[0] for r in rows]


@respx.mock
async def test_default_is_openrouter_and_answers(make_env: EnvFactory) -> None:
    ollama = mock_ollama()
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, content=or_sse("Autumn ", "leaves"))
    )
    async with make_env(**OR_ENV) as env:
        r = await post(env)
        events = parse_sse(r.text)
        assert [e[0] for e in events] == ["meta", "start", "token", "token", "done"]
        meta, done = events[0][1], events[-1][1]
        assert (meta["provider"], meta["model"]) == ("openrouter", OR_MODEL)
        assert meta["local_only"] is False
        assert meta["fallback_reason"] is None
        assert (done["provider"], done["cost_usd"], done["completion_tokens"]) == (
            "openrouter",
            "0.00002",
            2,
        )
        assert not ollama.called
        sent = json.loads(route.calls[0].request.content)
        assert sent["model"] == OR_MODEL and sent["usage"] == {"include": True}

        msgs = await env.messages()
        assert (msgs[-1].provider, msgs[-1].model, msgs[-1].content) == (
            "openrouter",
            OR_MODEL,
            "Autumn leaves",
        )
        details = await audit_rows(env)
        assert len(details) == 1
        d = details[0]
        assert d["outcome"] == "ok" and d["model"] == OR_MODEL
        assert d["prompt_tokens"] == 9 and d["completion_tokens"] == 2 and d["cost_usd"]
        assert d["conversation_id"] == meta["conversation_id"] and d["latency_ms"] >= 0
        assert "haiku" not in json.dumps(d) and "Autumn" not in json.dumps(d)  # no content
        conv = env.convs[0]  # type: ignore[attr-defined]
        assert (conv["provider"], conv["model"], conv["context"]) == (
            "openrouter",
            OR_MODEL,
            "general",
        )

        usage = (await env.a.get("/api/ai/usage")).json()
        assert usage["calls"] == 1 and usage["spent_usd"] == "0.00002"
        assert usage["limit_usd"] == "0.25" and usage["cap_reached"] is False
        assert usage["prompt_tokens"] == 9

        # The conversation keeps OpenRouter on the next turn without naming it.
        r = await post(env, conversation_id=meta["conversation_id"])
        assert parse_sse(r.text)[-1][1]["provider"] == "openrouter"


@respx.mock
async def test_filter_forces_local_and_nothing_is_sent(make_env: EnvFactory) -> None:
    mock_ollama()
    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, content=or_sse("x"))
    )
    async with make_env(**OR_ENV) as env:
        r = await post(
            env,
            messages=[
                {"role": "system", "content": "This month (INR):\n- spent ₹4,000"},
                {"role": "user", "content": "how am I doing?"},
            ],
        )
        events = parse_sse(r.text)
        meta, done = events[0][1], events[-1][1]
        assert (meta["provider"], meta["model"]) == ("ollama", MODEL)
        assert meta["requested_provider"] == "openrouter"
        assert (meta["fallback_reason"], meta["local_only"]) == ("filter", True)
        assert meta["filter_categories"] == ["finance"]
        assert (done["provider"], done["local_only"]) == ("ollama", True)
        assert not route.called
        assert await audit_rows(env) == []
        msgs = await env.messages()
        assert (msgs[-1].provider, msgs[-1].fallback_reason) == ("ollama", "filter")

        # History is part of the payload: the next clean question stays local too.
        r = await post(
            env,
            conversation_id=meta["conversation_id"],
            messages=[{"role": "user", "content": "and tomorrow?"}],
        )
        assert parse_sse(r.text)[0][1]["local_only"] is True
        assert not route.called


@respx.mock
async def test_linked_context_forces_local(make_env: EnvFactory) -> None:
    mock_ollama()
    route = respx.post(f"{BASE}/chat/completions")
    async with make_env(**OR_ENV) as env:
        meta = parse_sse((await post(env, context="trading")).text)[0][1]
        assert (meta["provider"], meta["fallback_reason"], meta["local_only"]) == (
            "ollama",
            "context",
            True,
        )
        # a link never loosens
        r = await post(env, conversation_id=meta["conversation_id"], context="general")
        assert parse_sse(r.text)[0][1]["fallback_reason"] == "context"
        assert not route.called


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(429, json={"error": {"message": "slow down"}}), "rate_limited"),
        (httpx.Response(502, text="bad gateway"), "upstream_error"),
        (httpx.Response(401, json={"error": {"message": "no auth"}}), "rejected"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("down"), "unavailable"),
    ],
)
@respx.mock
async def test_errors_fall_back_to_local(make_env: EnvFactory, response: Any, reason: str) -> None:
    mock_ollama()
    route = respx.post(f"{BASE}/chat/completions")
    if isinstance(response, Exception):
        route.side_effect = response
    else:
        route.mock(return_value=response)
    async with make_env(**OR_ENV) as env:
        events = parse_sse((await post(env)).text)
        kinds = [e[0] for e in events]
        assert [k for k in kinds if k != "queued"] == [
            *("meta", "start", "fallback", "start", "token", "token", "done")
        ]
        fb = next(e[1] for e in events if e[0] == "fallback")
        assert fb == {
            "provider": "ollama",
            "model": MODEL,
            "reason": reason,
            "local_only": False,
            "reset": True,
        }
        done = events[-1][1]
        assert (done["provider"], done["model"], done["fallback_reason"]) == (
            "ollama",
            MODEL,
            reason,
        )
        msgs = await env.messages()
        assert (msgs[-1].content, msgs[-1].provider, msgs[-1].fallback_reason) == (
            "local",
            "ollama",
            reason,
        )
        d = await audit_rows(env)
        assert len(d) == 1 and d[0]["outcome"] == f"error:{reason}"
        assert d[0]["fell_back_to"] == MODEL
        usage = (await env.a.get("/api/ai/usage")).json()
        assert (usage["calls"], usage["failures"]) == (1, 1)


@respx.mock
async def test_mid_stream_failure_resets_partial_text(make_env: EnvFactory) -> None:
    mock_ollama()
    partial = (
        b'data: {"choices":[{"delta":{"content":"half "}}]}\n\ndata: {"error":{"code":500}}\n\n'
    )
    respx.post(f"{BASE}/chat/completions").mock(return_value=httpx.Response(200, content=partial))
    async with make_env(**OR_ENV) as env:
        events = parse_sse((await post(env)).text)
        assert events[2] == ("token", {"content": "half "})
        assert events[3][0] == "fallback" and events[3][1]["reset"] is True
        assert (await env.messages())[-1].content == "local"


@respx.mock
async def test_daily_cap_keeps_it_local(make_env: EnvFactory) -> None:
    mock_ollama()
    route = respx.post(f"{BASE}/chat/completions")
    async with make_env(**OR_ENV) as env:
        redis = Redis.from_url(TEST_REDIS_URL)
        await redis.hset(day_key(utc_today()), "cost_micro", 250_000)
        await redis.aclose()
        meta = parse_sse((await post(env)).text)[0][1]
        assert (meta["provider"], meta["fallback_reason"], meta["local_only"]) == (
            "ollama",
            "daily_cap_reached",
            False,
        )
        assert not route.called
        usage = (await env.a.get("/api/ai/usage")).json()
        assert usage["cap_reached"] is True and Decimal(usage["remaining_usd"]) == 0


@respx.mock
async def test_disabled_or_unconfigured_stays_local(make_env: EnvFactory) -> None:
    mock_ollama()
    route = respx.post(f"{BASE}/chat/completions")
    async with make_env(CHAT_DEFAULT_MODEL=MODEL, CHAT_WARMUP=False) as env:
        # no provider: the local default
        meta = parse_sse((await post(env)).text)[0][1]
        assert (meta["provider"], meta["model"], meta["fallback_reason"]) == ("ollama", MODEL, None)
        # OpenRouter asked for explicitly: local, and the UI is told why
        meta = parse_sse((await post(env, provider="openrouter")).text)[0][1]
        assert (meta["provider"], meta["fallback_reason"]) == ("ollama", "disabled")
        cat = (await env.a.get("/api/models/catalog")).json()
        assert cat["default"] == {"provider": "ollama", "model": MODEL}
        assert cat["openrouter"]["unavailable_reason"] == "disabled"
        assert {m["provider"] for m in cat["models"]} == {"ollama"}
    assert not route.called


@respx.mock
async def test_unconfigured_stays_local(make_env: EnvFactory) -> None:
    mock_ollama()
    route = respx.post(f"{BASE}/chat/completions")
    async with make_env(**{**OR_ENV, "OPENROUTER_API_KEY": ""}) as env:
        meta = parse_sse((await post(env, provider="openrouter", model=OR_MODEL)).text)[0][1]
        assert (meta["provider"], meta["fallback_reason"]) == ("ollama", "not_configured")
    assert not route.called


@respx.mock
async def test_catalog_picker_and_disallowed_model(make_env: EnvFactory) -> None:
    mock_ollama()
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=httpx.Response(200, content=or_sse("ok"))
    )
    async with make_env(**OR_ENV) as env:
        cat = (await env.a.get("/api/models/catalog")).json()
        assert cat["default"] == {"provider": "openrouter", "model": OR_MODEL}
        assert cat["local_fallback_model"] == MODEL
        assert [(m["provider"], m["id"]) for m in cat["models"]] == [
            ("ollama", MODEL),
            ("ollama", "mistral:7b"),
            ("openrouter", OR_MODEL),
            ("openrouter", "openai/gpt-4.1-nano"),
        ]
        r = await post(env, provider="openrouter", model="evil/expensive-model")
        assert r.status_code == 404 and r.json()["available"] == [OR_MODEL, "openai/gpt-4.1-nano"]
        # a local pick on an OpenRouter-enabled server stays local, no reason
        meta = parse_sse((await post(env, provider="ollama", model="mistral:7b")).text)[0][1]
        assert (meta["provider"], meta["model"], meta["fallback_reason"]) == (
            "ollama",
            "mistral:7b",
            None,
        )
        # the allowlisted model by id alone
        meta = parse_sse((await post(env, model="openai/gpt-4.1-nano")).text)[0][1]
        assert (meta["provider"], meta["model"]) == ("openrouter", "openai/gpt-4.1-nano")
        assert (await env.a.get("/api/ai/usage")).status_code == 200
        assert (await env.b.get("/api/models/catalog")).status_code == 200


@respx.mock
async def test_key_never_in_responses_or_logs(
    make_env: EnvFactory, capfd: pytest.CaptureFixture[str]
) -> None:
    mock_ollama()
    route = respx.post(f"{BASE}/chat/completions")
    # An upstream that echoes the key in its error body, then a success.
    route.side_effect = [
        httpx.Response(401, json={"error": {"message": f"invalid key {KEY}"}}),
        httpx.Response(200, content=or_sse("fine")),
    ]
    bodies: list[str] = []
    async with make_env(**OR_ENV) as env:
        for _ in range(2):
            r = await post(env)
            bodies.append(r.text)
        conv = parse_sse(bodies[-1])[0][1]["conversation_id"]
        for path in (
            "/api/models/catalog",
            "/api/ai/usage",
            "/api/models",
            "/api/conversations",
            f"/api/conversations/{conv}",
            "/api/openapi.json",
        ):
            resp = await env.a.get(path)
            bodies.append(resp.text + json.dumps(dict(resp.headers)))
        details = await audit_rows(env)
        msgs = await env.messages()
    assert route.calls[0].request.headers["authorization"] == f"Bearer {KEY}"  # it IS used
    out, err = capfd.readouterr()
    for blob in [*bodies, out, err, json.dumps(details), *(m.error or "" for m in msgs)]:
        assert KEY not in blob
        assert KEY[9:30] not in blob
