"""Dependency readiness checks: Postgres, Redis, Ollama, HDD mount."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings


@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    latency_ms: float
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            **({"detail": self.detail} if self.detail else {}),
        }


async def _timed(name: str, fn: Callable[[], Awaitable[str | None]], limit: float) -> CheckResult:
    start = time.perf_counter()
    try:
        async with asyncio.timeout(limit):
            detail = await fn()
        ok = True
    except TimeoutError:
        ok, detail = False, f"timeout after {limit}s"
    except Exception as exc:
        # Report the exception type only: messages can contain DSNs/hosts.
        ok, detail = False, type(exc).__name__
    return CheckResult(name, ok, round((time.perf_counter() - start) * 1000, 2), detail)


def check_data_root(settings: Settings) -> str | None:
    """Raise if the HDD is not mounted. Sync: used at startup and in readiness."""
    root = settings.DATA_ROOT
    if not root.is_dir():
        raise RuntimeError(f"{root} does not exist")
    if not settings.data_marker_path.is_file():
        raise RuntimeError(f"marker {settings.DATA_ROOT_MARKER} missing (HDD not mounted?)")
    if not os.access(root, os.W_OK):
        raise RuntimeError(f"{root} not writable")
    return None


class HealthService:
    def __init__(
        self,
        settings: Settings,
        engine: AsyncEngine,
        redis: Redis,
        http: httpx.AsyncClient,
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.redis = redis
        self.http = http

    async def _postgres(self) -> str | None:
        async with self.engine.connect() as conn:
            version = (await conn.execute(text("SHOW server_version"))).scalar_one()
        return f"server_version={version}"

    async def _redis(self) -> str | None:
        await self.redis.ping()
        return None

    async def _ollama(self) -> str | None:
        resp = await self.http.get(f"{self.settings.OLLAMA_BASE_URL.rstrip('/')}/api/tags")
        resp.raise_for_status()
        return f"models={len(resp.json().get('models', []))}"

    async def _data_root(self) -> str | None:
        return await asyncio.to_thread(check_data_root, self.settings)

    async def readiness(self) -> list[CheckResult]:
        t = self.settings.HEALTH_CHECK_TIMEOUT
        return list(
            await asyncio.gather(
                _timed("postgres", self._postgres, t),
                _timed("redis", self._redis, t),
                _timed("ollama", self._ollama, t),
                _timed("data_root", self._data_root, t),
            )
        )
