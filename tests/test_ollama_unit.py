from __future__ import annotations

import pytest
import respx

from app.agents.ollama.client import ModelInfo
from app.agents.ollama.limiter import GenerationLimiter, QueueFull
from app.core.config import Settings


async def test_limiter_capacity_and_idempotent_release() -> None:
    lim = GenerationLimiter(max_concurrent=1, max_queue=1, retry_after=5)
    a, b = lim.admit(), lim.admit()
    with pytest.raises(QueueFull) as exc:
        lim.admit()
    assert exc.value.retry_after == 5
    await a.acquire_slot()
    assert lim.running == 1 and not b.slot_free()
    a.release()
    a.release()
    assert lim.admitted == 1 and lim.running == 0
    b.release()
    assert lim.admitted == 0


def test_model_info_parses_nanosecond_timestamps() -> None:
    m = ModelInfo.from_tag(
        {
            "name": "x",
            "modified_at": "2026-10-01T10:00:00.123456789-07:00",
            "details": {"quantization_level": "Q4_0"},
        }
    )
    assert m.modified_at is not None and m.modified_at.year == 2026
    assert m.quantization == "Q4_0"
    assert ModelInfo.from_tag({"name": "y", "modified_at": "garbage"}).modified_at is None


def test_trim_history_keeps_system_and_newest() -> None:
    from app.services.chat import trim_history

    h = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    assert trim_history(h, 0) == h
    assert [m["content"] for m in trim_history(h, 2)] == ["s", "u2", "a2"]
    assert trim_history(h, 99) == h
    assert trim_history([], 3) == []


def test_chat_options_only_sets_threads_when_asked() -> None:
    from app.services.chat import chat_options

    assert chat_options(2048, 0) == {"num_ctx": 2048}
    assert chat_options(2048, 4) == {"num_ctx": 2048, "num_thread": 4}


def test_keep_alive_setting_is_validated(settings: Settings) -> None:
    from pydantic import ValidationError

    base = settings.model_dump()
    for ok in ("", "24h", "30m", "-1", "0", "600"):
        Settings.model_validate({**base, "OLLAMA_KEEP_ALIVE": ok})
    for bad in ("24 hours", "1d", "-5m", "h"):
        with pytest.raises(ValidationError):
            Settings.model_validate({**base, "OLLAMA_KEEP_ALIVE": bad})


@respx.mock
async def test_warm_up_loads_with_chat_options_and_keep_alive(settings: Settings) -> None:
    import json

    import httpx

    from app.agents.ollama.client import OllamaClient
    from app.main import warm_up_chat_model

    base = "http://ollama.test:11434"
    respx.get(f"{base}/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]})
    )
    gen = respx.post(f"{base}/api/generate").mock(
        return_value=httpx.Response(200, json={"done": True})
    )
    client = OllamaClient(base, connect_timeout=1, read_timeout=5, cache_ttl=0, keep_alive="24h")
    s = Settings.model_validate(
        {
            **settings.model_dump(),
            "CHAT_DEFAULT_MODEL": "qwen2.5:3b",
            "CHAT_DEFAULT_NUM_CTX": 2048,
            "CHAT_NUM_THREAD": 4,
        }
    )
    await warm_up_chat_model(client, s)
    sent = json.loads(gen.calls[0].request.content)
    assert sent == {
        "model": "qwen2.5:3b",
        "prompt": "",
        "options": {"num_ctx": 2048, "num_thread": 4},
        "keep_alive": "24h",
    }
    # Not installed: skipped, no load. Ollama down: logged, never raised.
    s2 = Settings.model_validate({**s.model_dump(), "CHAT_DEFAULT_MODEL": "absent:1b"})
    await warm_up_chat_model(client, s2)
    assert gen.call_count == 1
    respx.get(f"{base}/api/tags").mock(side_effect=httpx.ConnectError("down"))
    await warm_up_chat_model(client, s)
    await client.aclose()
