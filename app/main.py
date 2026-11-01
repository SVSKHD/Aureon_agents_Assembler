"""FastAPI application factory and lifespan."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.api.router import api_router
from app.core.config import Environment, Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestIdMiddleware
from app.db.session import build_engine, build_sessionmaker
from app.services.health import HealthService, check_data_root

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Refuse to start in prod if the 2 TB HDD is not mounted.
        if settings.ENV is Environment.PROD:
            check_data_root(settings)

        engine = build_engine(settings)
        redis = Redis.from_url(settings.REDIS_URL, socket_timeout=settings.HEALTH_CHECK_TIMEOUT)
        http = httpx.AsyncClient(timeout=settings.HEALTH_CHECK_TIMEOUT)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = build_sessionmaker(engine)
        app.state.redis = redis
        app.state.http = http
        app.state.health = HealthService(settings, engine, redis, http)
        log.info("startup", env=settings.ENV.value)
        try:
            yield
        finally:
            log.info("shutdown")
            await http.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Aureon_Agentic",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.ENV is Environment.PROD else "/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["content-type", "x-csrf-token", "x-request-id"],
        )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(api_router)
    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn --factory app.main:app_factory`."""
    return create_app()
