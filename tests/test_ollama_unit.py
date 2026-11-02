from __future__ import annotations

import pytest

from app.agents.ollama.client import ModelInfo
from app.agents.ollama.limiter import GenerationLimiter, QueueFull


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
