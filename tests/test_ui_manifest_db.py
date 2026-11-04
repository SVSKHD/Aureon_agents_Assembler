"""GET /api/v1/ui/manifest and /api/openapi.json over HTTP (docs/ui-manifest.md)."""

from __future__ import annotations

import httpx
import pytest

from tests import test_finance_db
from tests.test_finance_db import EnvFactory

pytestmark = pytest.mark.db
make_env = test_finance_db.make_env


async def test_manifest_and_openapi_need_a_session_and_filter_by_role(
    make_env: EnvFactory,
) -> None:
    async with make_env() as env:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=env.app), base_url="https://test"
        ) as anon:
            for path in ("/api/v1/ui/manifest", "/api/openapi.json"):
                assert (await anon.get(path)).status_code == 401, path
            # Swagger exists only outside prod, and even then only behind the session.
            assert (await anon.get("/api/docs")).status_code in (401, 404)

        admin = await env.get("admin", "/api/v1/ui/manifest")
        user = await env.get("a", "/api/v1/ui/manifest")
        assert admin.status_code == user.status_code == 200
        h = env.app.state.schema_hash
        assert admin.json()["schema_hash"] == h and admin.headers["x-api-schema"] == h

        def paths(r: httpx.Response) -> set[str]:
            return {o["path"] for m in r.json()["modules"] for o in m["operations"]}

        assert "/api/v1/system/integrations" in paths(admin)
        assert "/api/v1/system/integrations" not in paths(user)
        assert "/api/finance/expenses" in paths(user)
        for r in (admin, user):
            assert not any(p.startswith("/api/v1/settings") for p in paths(r))

        spec = await env.get("a", "/api/openapi.json")
        assert spec.status_code == 200
        assert spec.json()["paths"]["/api/finance/expenses"]["get"]["x-ui"]["module"] == "finance"
