from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "spasta-data"
    root.mkdir()
    (root / ".spasta-data-mounted").touch()
    return root


@pytest.fixture
def settings(data_root: Path) -> Settings:
    return Settings(
        ENV="test",
        # Unreachable by default; DB tests override with TEST_DATABASE_URL.
        DATABASE_URL=TEST_DATABASE_URL or "postgresql+asyncpg://x:x@127.0.0.1:1/none",
        REDIS_URL=TEST_REDIS_URL,
        OLLAMA_BASE_URL="http://ollama.test:11434",
        DATA_ROOT=data_root,
        SECRET_KEY="t" * 40,
        CORS_ORIGINS="http://localhost:5173",
        HEALTH_CHECK_TIMEOUT=1.0,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app) as mgr:
        transport = ASGITransport(app=mgr.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def require_db() -> Iterator[str]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set (run `make test-db`)")
    yield TEST_DATABASE_URL
