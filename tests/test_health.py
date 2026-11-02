from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient

from app.core.config import Settings
from app.services.health import check_data_root

OLLAMA_TAGS = "http://ollama.test:11434/api/tags"


async def test_liveness(client: AsyncClient) -> None:
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_request_id_generated_and_echoed(client: AsyncClient) -> None:
    r = await client.get("/api/health")
    assert len(r.headers["x-request-id"]) == 32
    r = await client.get("/api/health", headers={"x-request-id": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"


async def test_request_id_unsafe_value_replaced(client: AsyncClient) -> None:
    r = await client.get("/api/health", headers={"x-request-id": "bad id\nwith newline"})
    assert r.headers["x-request-id"] != "bad id\nwith newline"


@respx.mock
async def test_readiness_reports_each_failure(client: AsyncClient) -> None:
    # Default test settings point Postgres at a closed port; Ollama returns 500.
    respx.get(OLLAMA_TAGS).mock(return_value=httpx.Response(500))
    r = await client.get("/api/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert set(body["checks"]) == {"postgres", "redis", "ollama", "data_root"}
    assert body["checks"]["ollama"]["ok"] is False
    assert body["checks"]["data_root"]["ok"] is True


def test_data_root_missing_marker(settings: Settings, data_root: Path) -> None:
    (data_root / ".spasta-data-mounted").unlink()
    with pytest.raises(RuntimeError, match="marker"):
        check_data_root(settings)


def test_data_root_missing_dir(settings: Settings, tmp_path: Path) -> None:
    s = settings.model_copy(update={"DATA_ROOT": tmp_path / "nope"})
    with pytest.raises(RuntimeError, match="does not exist"):
        check_data_root(s)


async def test_prod_refuses_to_start_without_hdd(settings: Settings, data_root: Path) -> None:
    from asgi_lifespan import LifespanManager

    from app.main import create_app

    (data_root / ".spasta-data-mounted").unlink()
    app = create_app(Settings.model_validate({**settings.model_dump(), "ENV": "prod"}))
    with pytest.raises(RuntimeError, match="marker"):
        async with LifespanManager(app):
            pass


async def test_cors_allowlist(client: AsyncClient) -> None:
    ok = await client.options(
        "/api/health",
        headers={"origin": "http://localhost:5173", "access-control-request-method": "GET"},
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:5173"
    bad = await client.options(
        "/api/health",
        headers={"origin": "https://evil.example", "access-control-request-method": "GET"},
    )
    assert "access-control-allow-origin" not in bad.headers
