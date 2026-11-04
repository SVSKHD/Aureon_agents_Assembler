"""Direct providers through POST /api/chat end to end with the DB (docs/26-ai-providers.md).

Gemini is used for the end-to-end path: its SDK runs on httpx, so respx intercepts the
app's real client. (Anthropic's SDK runs on httpx2; its adapter is covered against a mocked
transport in test_ai_providers_unit.py.) These need TEST_DATABASE_URL and TEST_REDIS_URL.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from tests.test_chat import MODEL, EnvFactory, make_env, parse_sse
from tests.test_chat_openrouter import (
    BASE as OR_BASE,
)
from tests.test_chat_openrouter import (
    OR_ENV,
    audit_rows,
    mock_ollama,
    post,
)

pytestmark = pytest.mark.db
FIXTURES = (make_env,)

GEM_BASE = "https://gemini.test"
GEM_KEY = "AIza" + "SyE2eT3stK3yN0tR3alAtAll0000"
GEM_MODEL = "gemini-test-flash"
GEM_ENV: dict[str, Any] = {
    "GEMINI_ENABLED": True,
    "GEMINI_API_KEY": GEM_KEY,
    "GEMINI_BASE_URL": GEM_BASE,
    "GEMINI_DEFAULT_MODEL": GEM_MODEL,
    "GEMINI_ALLOWED_MODELS": "gemini-unpriced",
    "GEMINI_DAILY_LIMIT_USD": "0.25",
    "GEMINI_MODEL_PRICES": json.dumps({GEM_MODEL: {"input": "0.5", "output": "2"}}),
    "AI_DEFAULT_EXTERNAL_PROVIDER": "gemini",
    "AI_EXTERNAL_MAX_RETRIES": 0,
    "CHAT_DEFAULT_MODEL": MODEL,
    "CHAT_WARMUP": False,
}


def gem_sse(*pieces: str) -> bytes:
    out = []
    for i, p in enumerate(pieces):
        chunk: dict[str, Any] = {
            "candidates": [{"content": {"role": "model", "parts": [{"text": p}]}, "index": 0}],
            "modelVersion": GEM_MODEL,
        }
        if i == len(pieces) - 1:
            chunk["candidates"][0]["finishReason"] = "STOP"
            chunk["usageMetadata"] = {"promptTokenCount": 8, "candidatesTokenCount": 2}
        out.append(f"data: {json.dumps(chunk)}\r\n\r\n")
    return "".join(out).encode()


def gem_route(response: httpx.Response) -> respx.Route:
    return respx.post(url__regex=rf"^{GEM_BASE}/.*:streamGenerateContent.*").mock(
        return_value=response
    )


@respx.mock
async def test_gemini_default_answers_and_is_recorded(make_env: EnvFactory) -> None:
    ollama = mock_ollama()
    route = gem_route(httpx.Response(200, content=gem_sse("Monsoon ", "rain")))
    async with make_env(**GEM_ENV) as env:
        r = await post(env)
        events = parse_sse(r.text)
        meta, done = events[0][1], events[-1][1]
        assert (meta["provider"], meta["model"], meta["requested_provider"]) == (
            "gemini",
            GEM_MODEL,
            "gemini",
        )
        assert done["provider"] == "gemini" and done["completion_tokens"] == 2
        assert done["cost_usd"] is not None
        assert not ollama.called and route.called
        assert route.calls[0].request.headers["x-goog-api-key"] == GEM_KEY
        msgs = await env.messages()
        assert (msgs[-1].provider, msgs[-1].content) == ("gemini", "Monsoon rain")
        details = await audit_rows(env)
        assert details[-1]["provider"] == "gemini" and details[-1]["outcome"] == "ok"
        assert "haiku" not in json.dumps(details[-1])  # metadata only
        # The widened check constraint (migration 0023) accepted the conversation row.
        assert env.convs[0]["provider"] == "gemini"  # type: ignore[attr-defined]
        usage = (await env.a.get("/api/ai/usage", params={"provider": "gemini"})).json()
        assert usage["provider"] == "gemini" and usage["calls"] == 1
        assert usage["limit_usd"] == "0.25"
        cat = (await env.a.get("/api/models/catalog")).json()
        assert cat["default"] == {"provider": "gemini", "model": GEM_MODEL}
        listed = {(m["provider"], m["id"]): m for m in cat["models"]}
        assert listed[("gemini", GEM_MODEL)]["available"] is True
        assert listed[("gemini", "gemini-unpriced")]["available"] is False
        providers = {p["provider"]: p for p in cat["providers"]}
        assert providers["gemini"]["usable"] and providers["gemini"]["configured"]
        assert providers["ollama"]["privacy"] == "local"
        assert GEM_KEY not in json.dumps(cat)


@respx.mock
async def test_gemini_error_falls_back_to_local_not_to_openrouter(make_env: EnvFactory) -> None:
    ollama = mock_ollama()
    gem_route(httpx.Response(500, json={"error": {"code": 500, "message": "boom"}}))
    or_route = respx.post(f"{OR_BASE}/chat/completions").mock(return_value=httpx.Response(500))
    async with make_env(**{**OR_ENV, **GEM_ENV}) as env:
        events = parse_sse((await post(env)).text)
        kinds = [e[0] for e in events]
        assert "fallback" in kinds and kinds[-1] == "done"
        fb = next(d for k, d in events if k == "fallback")
        assert fb["provider"] == "ollama"
        assert events[-1][1]["fallback_reason"] == "upstream_error"
        assert ollama.called and not or_route.called


@respx.mock
async def test_private_data_and_unpriced_models_stay_local(make_env: EnvFactory) -> None:
    mock_ollama()
    route = gem_route(httpx.Response(200, content=gem_sse("x")))
    async with make_env(**GEM_ENV) as env:
        r = await env.a.post(
            "/api/chat",
            json={"messages": [{"role": "user", "content": "explain my salary slip and loan EMI"}]},
            headers={"x-csrf-token": env.csrf_a},
        )
        meta = parse_sse(r.text)[0][1]
        assert (meta["provider"], meta["fallback_reason"], meta["local_only"]) == (
            "ollama",
            "filter",
            True,
        )
        meta = parse_sse((await post(env, provider="gemini", model="gemini-unpriced")).text)[0][1]
        assert (meta["provider"], meta["fallback_reason"]) == ("ollama", "no_pricing")
        r = await post(env, provider="gemini", model="gemini-not-allowed")
        assert r.status_code == 404
    assert not route.called
