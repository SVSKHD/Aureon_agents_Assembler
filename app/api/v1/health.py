"""Liveness and readiness endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response, status

from app.api.ui import ui_module
from app.services.health import HealthService

UI_MODULE = ui_module("health", label="Health", group="admin", icon="heart-pulse", order=950)
router = APIRouter(prefix="/health", tags=["health"])


@router.get("", summary="Liveness: the process is up and serving")
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", summary="Readiness: Postgres, Redis, Ollama and the HDD are reachable")
async def readiness(request: Request, response: Response) -> dict[str, Any]:
    svc: HealthService = request.app.state.health
    results = await svc.readiness()
    ready = all(r.ok for r in results)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready else "not_ready",
        "checks": {r.name: r.as_dict() for r in results},
    }
