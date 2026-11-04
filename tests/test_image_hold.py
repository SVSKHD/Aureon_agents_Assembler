"""Image generation yields to chat/voice and trading windows (docs/image-agent.md)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.services import interactive
from app.workers.imaging import ImagingWorker, in_pause_window


class ZRedis:
    def __init__(self) -> None:
        self.z: dict[str, float] = {}

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        self.z.update(mapping)

    async def zrem(self, key: str, member: str) -> None:
        self.z.pop(member, None)

    async def zremrangebyscore(self, key: str, lo: Any, hi: float) -> None:
        self.z = {k: v for k, v in self.z.items() if v > hi}

    async def zcard(self, key: str) -> int:
        return len(self.z)


def test_pause_windows_are_ist_weekdays() -> None:
    w = ["09:00-09:35", "15:00-15:35"]
    # 2026-11-04 is a Wednesday. 03:40 UTC = 09:10 IST.
    assert in_pause_window(w, datetime(2026, 11, 4, 3, 40, tzinfo=UTC))
    assert not in_pause_window(w, datetime(2026, 11, 4, 4, 10, tzinfo=UTC))  # 09:40 IST
    assert in_pause_window(w, datetime(2026, 11, 4, 9, 45, tzinfo=UTC))  # 15:15 IST
    assert not in_pause_window(w, datetime(2026, 11, 7, 3, 40, tzinfo=UTC))  # Saturday
    assert not in_pause_window([], datetime(2026, 11, 4, 3, 40, tzinfo=UTC))


async def test_interactive_marks_expire_and_clear() -> None:
    r = ZRedis()
    t = await interactive.mark(r, "chat", 60)
    assert await interactive.active(r) == 1
    await interactive.clear(r, t)
    assert await interactive.active(r) == 0
    r.z["voice:stale"] = time.time() - 1  # a crashed request's mark has expired
    assert await interactive.active(r) == 0


async def test_worker_holds_new_jobs_while_chat_runs(settings: Settings) -> None:
    w = ImagingWorker(settings, maker=None, generator=object())  # type: ignore[arg-type]
    w.redis = ZRedis()  # type: ignore[assignment]
    await interactive.mark(w.redis, "chat", 60)
    assert await w.hold_reason() == "interactive"
    assert await w.run_once() is False and w.hold == "interactive"  # nothing claimed
    off = ImagingWorker(
        settings.model_copy(update={"IMAGE_PAUSE_FOR_INTERACTIVE": False}),
        maker=None,  # type: ignore[arg-type]
        generator=object(),  # type: ignore[arg-type]
    )
    off.redis = w.redis
    assert await off.hold_reason() is None


async def test_running_job_is_suspended_and_resumed(settings: Settings) -> None:
    class Gen:
        def __init__(self) -> None:
            self.events: list[str] = []

        def suspend(self) -> bool:
            self.events.append("stop")
            return True

        def resume(self) -> bool:
            self.events.append("cont")
            return True

    g = Gen()
    w = ImagingWorker(settings, maker=None, generator=g)  # type: ignore[arg-type]
    r = ZRedis()
    w.redis = r  # type: ignore[assignment]
    token = await interactive.mark(r, "voice", 60)
    await w._yield_if_needed()
    await w._yield_if_needed()  # already suspended: no second SIGSTOP
    assert g.events == ["stop"] and w.suspended
    await interactive.clear(r, token)
    await w._yield_if_needed()
    assert g.events == ["stop", "cont"] and not w.suspended
