"""HTTPX client for the local Ollama container. Offline only: there is no external fallback."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx


class OllamaError(Exception):
    """Ollama answered, but with an error (bad model, runner crash, ...)."""

    code = "model_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class OllamaUnavailable(OllamaError):
    code = "upstream_unavailable"


class OllamaTimeout(OllamaError):
    code = "timeout"


class ModelNotFound(OllamaError):
    code = "model_not_found"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    name: str
    digest: str | None
    size: int | None
    family: str | None
    parameter_size: str | None
    quantization: str | None
    modified_at: datetime | None

    @classmethod
    def from_tag(cls, raw: dict[str, Any]) -> ModelInfo:
        details = raw.get("details") or {}
        modified: datetime | None
        try:
            modified = datetime.fromisoformat(raw["modified_at"])
        except (KeyError, TypeError, ValueError):
            modified = None
        return cls(
            name=str(raw.get("name") or raw.get("model")),
            digest=raw.get("digest"),
            size=raw.get("size"),
            family=details.get("family"),
            parameter_size=details.get("parameter_size"),
            quantization=details.get("quantization_level"),
            modified_at=modified,
        )


def _error_text(body: bytes) -> str:
    try:
        return str(json.loads(body).get("error") or body.decode(errors="replace"))
    except (ValueError, AttributeError):
        return body.decode(errors="replace")[:500]


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        *,
        connect_timeout: float,
        read_timeout: float,
        cache_ttl: float,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(
                connect=connect_timeout, read=read_timeout, write=30.0, pool=connect_timeout
            ),
        )
        self._cache_ttl = cache_ttl
        self._cache: tuple[float, list[ModelInfo]] | None = None
        self._lock = asyncio.Lock()
        # Bumped on every successful refresh, so callers can sync the inventory table once.
        self.generation = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get_json(self, path: str) -> dict[str, Any]:
        try:
            resp = await self._http.get(path)
        except httpx.TimeoutException as exc:
            raise OllamaUnavailable("Ollama did not respond") from exc
        except httpx.TransportError as exc:
            raise OllamaUnavailable("Ollama is unreachable") from exc
        if resp.status_code >= 400:
            raise OllamaUnavailable(f"Ollama returned HTTP {resp.status_code}")
        data: dict[str, Any] = resp.json()
        return data

    async def list_models(self, *, force: bool = False) -> list[ModelInfo]:
        """Installed models from GET /api/tags, cached for `cache_ttl` seconds."""
        async with self._lock:
            now = time.monotonic()
            if not force and self._cache and now - self._cache[0] < self._cache_ttl:
                return self._cache[1]
            data = await self._get_json("/api/tags")
            models = [ModelInfo.from_tag(m) for m in data.get("models", [])]
            self._cache = (now, models)
            self.generation += 1
            return models

    async def running(self) -> list[dict[str, Any]]:
        """Models currently loaded in memory (GET /api/ps)."""
        data = await self._get_json("/api/ps")
        return list(data.get("models", []))

    async def chat_stream(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield Ollama's NDJSON chunks. Closing the generator closes the HTTP stream,
        which makes Ollama stop generating."""
        payload = {"model": model, "messages": messages, "stream": True, "options": options}
        try:
            async with self._http.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code == 404:
                    raise ModelNotFound(_error_text(await resp.aread()))
                if resp.status_code >= 400:
                    raise OllamaError(_error_text(await resp.aread()))
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk: dict[str, Any] = json.loads(line)
                    if chunk.get("error"):
                        raise OllamaError(str(chunk["error"]))
                    yield chunk
        except httpx.TimeoutException as exc:
            raise OllamaTimeout("Ollama stopped responding mid-generation") from exc
        except httpx.TransportError as exc:
            raise OllamaUnavailable("lost connection to Ollama") from exc
