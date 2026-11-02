from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import ai, auth, health, images

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(ai.router)
api_router.include_router(images.router)
