"""Runs only against an isolated test database (TEST_DATABASE_URL) — see scripts/test_db.sh."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import httpx
import pytest
import respx
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.db


def _alembic(*args: str, url: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        check=True,
        env={**os.environ, "DATABASE_URL": url},
    )


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT table_schema||'.'||table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog','information_schema')"
            )
        )
        names = {r[0] for r in rows}
    await engine.dispose()
    return names


async def test_migrations_upgrade_downgrade_leave_existing_tables(require_db: str) -> None:
    engine = create_async_engine(require_db)
    async with engine.begin() as conn:
        # Stand-in for a pre-existing Spasta table that must survive.
        await conn.execute(text("CREATE TABLE IF NOT EXISTS public.trades (id int primary key)"))
    await engine.dispose()

    await asyncio.to_thread(_alembic, "upgrade", "head", url=require_db)
    after_up = await _tables(require_db)
    assert "public.aureon_alembic_version" in after_up
    assert "public.trades" in after_up

    # Earlier tests leave rows in aureon.*, and migration 0027 refuses (rightly) to drop a
    # schema holding data; empty them first, as a fresh install would be.
    engine = create_async_engine(require_db)
    async with engine.begin() as conn:
        aureon_tables = (await conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'aureon'"
        ))).scalars().all()  # fmt: skip
        if aureon_tables:
            names = ", ".join(f"aureon.{t}" for t in aureon_tables)
            await conn.execute(text(f"TRUNCATE {names} CASCADE"))
    await engine.dispose()
    try:
        await asyncio.to_thread(_alembic, "downgrade", "base", url=require_db)
        assert "public.trades" in await _tables(require_db)
    finally:
        # Always back to head: every later test in the run needs the full schema.
        await asyncio.to_thread(_alembic, "upgrade", "head", url=require_db)


@respx.mock
async def test_readiness_all_green(require_db: str, client: AsyncClient) -> None:
    respx.get("http://ollama.test:11434/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})
    )
    r = await client.get("/api/health/ready")
    assert r.status_code == 200, r.json()
    checks = r.json()["checks"]
    assert all(c["ok"] for c in checks.values())
    assert checks["ollama"]["detail"] == "models=1"
