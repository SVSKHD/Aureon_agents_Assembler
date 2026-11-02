"""System endpoints under /api/v1/system (Phase 22, docs/24-production-integration.md)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.auth.deps import AdminRequired
from app.services.integrations import IntegrationsSummary

router = APIRouter(prefix="/v1/system", tags=["system"])


@router.get("/integrations")
async def integrations(request: Request, _: AdminRequired) -> dict[str, Any]:
    """The Spasta health card: MT5 feed freshness, Dhan, GitHub, webhook worker, Claude CLI,
    Ollama models, Discord, the DB role and backups. Read-only; admin only."""
    s = request.app.state
    return await IntegrationsSummary(
        s.settings, s.sessionmaker, s.ollama, getattr(s, "claude_cli", None)
    ).collect()
