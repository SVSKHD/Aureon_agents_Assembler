from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    agents,
    ai,
    approvals,
    auth,
    code,
    finance,
    github,
    github_issues,
    github_pulls,
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
api_router.include_router(github_issues.router)  # /api/v1/github issues, links, operations
api_router.include_router(github_pulls.router)  # /api/v1/github pulls + gated operations
api_router.include_router(approvals.router)  # /api/v1/approvals
