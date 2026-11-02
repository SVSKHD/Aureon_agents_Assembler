from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    agents,
    ai,
    auth,
    code,
    finance,
    github,
    health,
    images,
    paper,
    public,
    trading,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(finance.router)
api_router.include_router(ai.router)
api_router.include_router(images.router)
api_router.include_router(agents.router)
api_router.include_router(trading.router)
api_router.include_router(paper.router)
api_router.include_router(code.router)
api_router.include_router(public.router)
api_router.include_router(github.router)  # /api/v1/github
