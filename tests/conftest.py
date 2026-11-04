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
        # Never pick up a real token from the host environment.
        GITHUB_TOKEN=None,
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app) as mgr:
        transport = ASGITransport(app=mgr.app)
        async with AsyncClient(transport=transport, base_url="https://test") as c:
            yield c


@pytest.fixture
def require_db() -> Iterator[str]:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set (run `make test-db`)")
    yield TEST_DATABASE_URL


@pytest.fixture(scope="session")
def migrated() -> Iterator[str]:
    """Bring the isolated test database to head once per run."""
    import subprocess
    import sys

    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set (run `make test-db`)")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        env={**os.environ, "DATABASE_URL": TEST_DATABASE_URL},
    )
    yield TEST_DATABASE_URL


@pytest.fixture
async def db_clean(migrated: str) -> AsyncIterator[str]:
    """Empty auth tables and the test Redis DB before each test."""
    from redis.asyncio import Redis
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(migrated)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE agents.agent_logs, agents.agent_runs, agents.scheduled_jobs, "
                "agents.agents, agents.repair_tasks, agents.incidents, "
                "agents.generated_assets, agents.image_jobs, "
                "ai.messages, ai.conversations, ai.model_inventory, "
                "audit.audit_events, core.app_settings, core.sessions, core.users "
                "RESTART IDENTITY CASCADE"
            )
        )
    await engine.dispose()
    redis = Redis.from_url(TEST_REDIS_URL)
    await redis.flushdb()
    await redis.aclose()
    yield migrated
