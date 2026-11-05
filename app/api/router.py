from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    agents,
    ai,
    ai_usage,
    app_settings,
    approvals,
    audit_events,
    auth,
    code,
    dhan_auth,
    dhan_feed,
    finance,
    github,
    github_actions,
    github_issues,
    github_pulls,
    github_webhooks,
    health,
    images,
    mt5_push,
    paper,
    public,
    repairs,
    reviews,
    system,
    trading,
    ui_manifest,
    voice,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(finance.router)
api_router.include_router(ai.router)
api_router.include_router(ai_usage.router)  # /api/v1/ai/usage (per-agent model calls)
api_router.include_router(images.router)
api_router.include_router(agents.router)
api_router.include_router(trading.router)
api_router.include_router(mt5_push.router)  # /api/trading/mt5/ingest (bearer) + dashboard reads
api_router.include_router(paper.router)
api_router.include_router(code.router)
api_router.include_router(public.router)
api_router.include_router(github.router)  # /api/v1/github
api_router.include_router(github_issues.router)  # /api/v1/github issues, links, operations
api_router.include_router(github_pulls.router)  # /api/v1/github pulls + gated operations
api_router.include_router(github_actions.router)  # /api/v1/github …/actions (Phase 17)
api_router.include_router(github_webhooks.router)  # POST /api/v1/github/webhooks (public, HMAC)
api_router.include_router(approvals.router)  # /api/v1/approvals
api_router.include_router(audit_events.router)  # /api/v1/audit/events
api_router.include_router(voice.router)  # /api/v1/voice/intent (navigation only)
api_router.include_router(repairs.router)  # /api/v1/repairs (Phase 18 self-repair)
api_router.include_router(reviews.router)  # /api/v1/reviews + …/pulls/{n}/claude-review
api_router.include_router(app_settings.router)  # /api/v1/settings (runtime settings + secrets)
api_router.include_router(dhan_auth.router)  # /api/v1/dhan/auth (Dhan API key login)
api_router.include_router(dhan_feed.router)  # /api/v1/dhan/feed (feed health, reconnect)
api_router.include_router(ui_manifest.router)  # /api/v1/ui/manifest + /api/openapi.json (auth)
api_router.include_router(system.router)  # /api/v1/system (Phase 22 integrations card)
