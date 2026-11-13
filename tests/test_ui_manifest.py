"""Self-describing API: x-ui on every route, the manifest, the schema hash (docs/ui-manifest.md).

`test_every_route_declares_ui_metadata` is the CI check: a new route in a router module with
no UI_MODULE (and no per-route ui_meta, and not hidden) fails here.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from pydantic import BaseModel

from app.api import ui
from app.core.config import Settings
from app.main import create_app
from app.services import trading_stream


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


def _ops(app: FastAPI) -> dict[tuple[str, str], dict[str, Any]]:
    return {(o.method, o.path): o.ui for o in ui.describe_routes(app) if o.ui is not None}


def test_every_route_declares_ui_metadata(app: FastAPI) -> None:
    missing = ui.undeclared(app)
    assert missing == [], (
        "routes without x-ui: declare UI_MODULE = ui_module(...) in the router module, or "
        f"openapi_extra=ui_meta(...) / ui_meta(hidden=True) on the route: {missing}"
    )


def test_openapi_carries_x_ui_on_every_operation(app: FastAPI) -> None:
    schema = app.openapi()
    for path, methods in schema["paths"].items():
        for method, node in methods.items():
            meta = node.get("x-ui")
            assert meta is not None, (method, path)
            assert set(ui.FIELDS) <= set(meta), (method, path, set(ui.FIELDS) - set(meta))
            assert meta["kind"] in ui.KINDS and meta["danger"] in ui.DANGER_RANK


SENSITIVE: list[tuple[str, str, set[str]]] = [
    ("PUT", "/api/v1/settings/{key}", {"hidden"}),
    ("GET", "/api/v1/settings", {"hidden"}),
    ("POST", "/api/v1/dhan/auth/consume", {"hidden"}),
    ("POST", "/api/trading/accounts/{account_id}/mode", {"hidden"}),
    ("POST", "/api/trading/accounts/{account_id}/mode/live-token", {"hidden"}),
    ("POST", "/api/trading/order-intents", {"approval", "hidden"}),
    ("POST", "/api/trading/order-intents/{intent_id}/approve", {"approval", "hidden"}),
    ("POST", "/api/v1/approvals/{approval_id}/approve", {"approval", "hidden"}),
    ("POST", "/api/v1/approvals/{approval_id}/deny", {"approval", "hidden"}),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/pulls/{n}/merge", {"approval", "hidden"}),
    ("POST", "/api/code/git/push", {"hidden"}),
    ("POST", "/api/v1/github/repositories/{owner}/{repo}/force-push", {"hidden"}),
    ("POST", "/api/v1/github/webhooks", {"hidden"}),
    ("POST", "/api/trading/mt5/ingest", {"hidden"}),
    ("POST", "/api/trading/mt5/records", {"hidden"}),
    ("POST", "/api/trading/mt5/bars", {"hidden"}),
    ("POST", "/api/v1/ingest/bars", {"hidden"}),
    ("PATCH", "/api/v1/ingest/trade/{tid}", {"hidden"}),
    ("POST", "/api/v1/admin/risk/clear", {"hidden"}),
    ("POST", "/api/v1/admin/review/run", {"hidden"}),
    ("GET", "/api/v1/status", {"hidden"}),
    ("POST", "/api/auth/login", {"hidden"}),
    ("PUT", "/api/v1/approvals/policies", {"hidden"}),
]


@pytest.mark.parametrize(("method", "path", "allowed"), SENSITIVE)
def test_sensitive_routes_are_never_plain_buttons(
    app: FastAPI, method: str, path: str, allowed: set[str]
) -> None:
    meta = _ops(app)[(method, path)]
    assert meta["danger"] in allowed, (method, path, meta["danger"])


def test_every_writing_route_is_at_least_confirm_unless_declared(app: FastAPI) -> None:
    for (method, path), meta in _ops(app).items():
        if method in ui.UNSAFE:
            assert meta["danger"] != "none", (method, path)


def _tiny(extra_route: bool = False, danger: str | None = None) -> FastAPI:
    class Item(BaseModel):
        id: int
        title: str
        price: float

    class Page(BaseModel):
        items: list[Item]
        total: int

    r = APIRouter()

    @r.get("/api/v1/things", response_model=Page)
    async def list_things() -> Any: ...

    @r.put(
        "/api/v1/settings/{key}",
        openapi_extra=ui.ui_meta(module="things", danger=danger),  # type: ignore[arg-type]
    )
    async def put_setting(key: str) -> Any: ...

    if extra_route:

        @r.delete("/api/v1/things/{thing_id}")
        async def delete_thing(thing_id: int) -> Any: ...

    app = FastAPI()
    app.include_router(r)
    ui.install_openapi(app)
    return app


# Routers defined in this test module use this module's UI declaration.
UI_MODULE = ui.ui_module("things", label="Things", group="test", icon="box", order=1)


def test_defaults_are_derived_from_the_response_model() -> None:
    meta = _ops(_tiny())[("GET", "/api/v1/things")]
    assert meta["kind"] == "list" and meta["module"] == "things"
    assert meta["columns"] == ["id", "title", "price"]
    assert meta["id_field"] == "id" and meta["title_field"] == "title"
    assert meta["roles"] == ["public", "user", "admin"]
    assert meta["label"] == "List things"


def test_explicit_metadata_cannot_lower_a_safety_floor() -> None:
    meta = _ops(_tiny(danger="none"))[("PUT", "/api/v1/settings/{key}")]
    assert meta["danger"] == "hidden" and meta["danger_reason"] == "secrets and settings"


def test_manifest_hides_hidden_and_admin_only_operations(app: FastAPI) -> None:
    admin = ui.manifest(app, is_admin=True, schema="h")
    user = ui.manifest(app, is_admin=False, schema="h")
    assert admin["schema_hash"] == "h"

    def ops(m: dict[str, Any]) -> list[dict[str, Any]]:
        return [o for mod in m["modules"] for o in mod["operations"]]

    for o in ops(admin):
        assert o["x-ui"]["danger"] != "hidden", o["path"]
        assert {"method", "path", "operation_id", "x-ui"} <= set(o)
    for o in ops(user):
        assert "user" in o["x-ui"]["roles"] or "public" in o["x-ui"]["roles"], o["path"]
    admin_paths = {(o["method"], o["path"]) for o in ops(admin)}
    user_paths = {(o["method"], o["path"]) for o in ops(user)}
    assert user_paths < admin_paths
    assert ("GET", "/api/v1/system/integrations") in admin_paths - user_paths
    for absent in ("/api/v1/settings", "/api/auth/login", "/api/v1/github/webhooks"):
        assert not any(p == absent for _, p in admin_paths), absent
    orders = [m["order"] for m in admin["modules"]]
    assert orders == sorted(orders)
    assert all(
        {"module", "label", "group", "icon", "operations"} <= set(m) for m in admin["modules"]
    )


def test_schema_hash_changes_when_routes_change() -> None:
    a, b = ui.schema_hash(_tiny()), ui.schema_hash(_tiny())
    assert a == b and len(a) == 16
    assert ui.schema_hash(_tiny(extra_route=True)) != a


def test_app_exposes_its_hash_and_no_public_openapi(app: FastAPI) -> None:
    assert app.state.schema_hash == ui.schema_hash(app)
    assert app.openapi_url is None and app.docs_url is None


class FakeRedis:
    def __init__(self) -> None:
        self.h: dict[str, dict[str, str]] = {}

    async def hget(self, key: str, field: str) -> str | None:
        return self.h.get(key, {}).get(field)

    async def hset(self, key: str, mapping: dict[str, str]) -> None:
        self.h.setdefault(key, {}).update(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.h.get(key, {}))


async def test_schema_change_is_announced_once_and_reaches_reconnecting_streams() -> None:
    r = FakeRedis()
    assert await ui.announce_schema(r, "aaaa") is False  # first ever: nothing to announce
    assert await ui.announce_schema(r, "aaaa") is False
    assert await ui.announce_schema(r, "bbbb") is True
    event = await trading_stream._schema_event(r, "bbbb", reconnect=True)  # type: ignore[arg-type]
    assert event == {"hash": "bbbb", "previous": "aaaa"}
    assert await trading_stream._schema_event(r, "bbbb", reconnect=False) is None  # type: ignore[arg-type]
    r.h[ui.SCHEMA_KEY]["changed_at"] = str(time.time() - ui.SCHEMA_CHANGED_WINDOW_S - 1)
    assert await trading_stream._schema_event(r, "bbbb", reconnect=True) is None  # type: ignore[arg-type]
