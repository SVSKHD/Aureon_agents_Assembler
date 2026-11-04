"""The UI manifest and the OpenAPI document, for signed-in users only (docs/ui-manifest.md).

GET /api/v1/ui/manifest   modules -> operations -> x-ui, filtered for this viewer
GET /api/openapi.json     the full OpenAPI document (with x-ui), never public
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api import ui
from app.auth.deps import Current
from app.models.auth import ROLE_ADMIN

UI_MODULE = ui.ui_module("system", label="System", group="admin", icon="server", order=900)

router = APIRouter(tags=["ui"])


def _is_admin(current: Any) -> bool:
    return bool(current.user.role.name == ROLE_ADMIN)


@router.get(
    "/v1/ui/manifest",
    openapi_extra=ui.ui_meta(hidden=True),  # the manifest does not list itself
)
async def ui_manifest(request: Request, current: Current) -> dict[str, Any]:
    """What the frontend can auto-wire for this user, and the schema hash it was built from.
    Hidden operations are absent; admin-only ones appear for admins only."""
    return ui.manifest(
        request.app, is_admin=_is_admin(current), schema=request.app.state.schema_hash
    )


@router.get("/openapi.json", include_in_schema=False)
async def openapi_json(request: Request, _: Current) -> JSONResponse:
    """The OpenAPI document with x-ui. Signed-in only (it maps every route)."""
    return JSONResponse(request.app.openapi())
