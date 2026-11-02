"""Bounded admission + concurrency limit for Ollama generations (per API process).

capacity = max_concurrent running + max_queue waiting. A request past capacity is
refused up front (HTTP 429) instead of piling up on a 4-core CPU box.
"""

from __future__ import annotations

import asyncio


class QueueFull(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("generation queue is full")
        self.retry_after = retry_after


class GenerationLimiter:
    def __init__(self, max_concurrent: int, max_queue: int, retry_after: int) -> None:
        self._sem = asyncio.Semaphore(max_concurrent)
        self.max_concurrent = max_concurrent
        self.capacity = max_concurrent + max_queue
        self.retry_after = retry_after
        self.admitted = 0

    def admit(self) -> Ticket:
        if self.admitted >= self.capacity:
            raise QueueFull(self.retry_after)
        self.admitted += 1
        return Ticket(self)

    @property
    def running(self) -> int:
        return self.max_concurrent - self._sem._value


class Ticket:
    """One admitted request. `release()` is idempotent and frees the slot and the admission."""

    def __init__(self, limiter: GenerationLimiter) -> None:
        self._limiter = limiter
        self._has_slot = False
        self._admitted = True

    def slot_free(self) -> bool:
        return not self._limiter._sem.locked()

    async def acquire_slot(self) -> None:
        await self._limiter._sem.acquire()
        self._has_slot = True

    def release_slot(self) -> None:
        if self._has_slot:
            self._has_slot = False
            self._limiter._sem.release()

    def release(self) -> None:
        self.release_slot()
        if self._admitted:
            self._admitted = False
            self._limiter.admitted -= 1
