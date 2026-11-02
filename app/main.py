"""FastAPI application factory and lifespan."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.agents.ollama.client import OllamaClient
from app.agents.ollama.limiter import GenerationLimiter
from app.agents.registry import OLLAMA, AgentRegistry, Heartbeat
from app.agents.trading.dhan import build_dhan_adapter
from app.agents.trading.mt5 import build_mt5_adapter
from app.api.router import api_router
from app.core.cf_access import CloudflareAccessMiddleware
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.middleware import RequestIdMiddleware, SecurityHeadersMiddleware
from app.core.prod_checks import assert_production_ready
from app.db.session import build_engine, build_sessionmaker
from app.integrations.claude_cli import ClaudeCLI
from app.integrations.github import build_github_client
from app.services import incidents
from app.services.claude_review import ReviewRunner
from app.services.health import HealthService

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ENV=prod checklist: refuse to start on a weak SECRET_KEY, DEBUG/docs on, insecure
        # cookies, a missing HDD or an unapproved LIVE flag (app/core/prod_checks.py).
        assert_production_ready(settings)

        engine = build_engine(settings)
        redis = Redis.from_url(settings.REDIS_URL, socket_timeout=settings.HEALTH_CHECK_TIMEOUT)
        http = httpx.AsyncClient(timeout=settings.HEALTH_CHECK_TIMEOUT)
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = build_sessionmaker(engine)
        app.state.redis = redis
        app.state.http = http
        app.state.health = HealthService(settings, engine, redis, http)
        ollama = OllamaClient(
            settings.OLLAMA_BASE_URL,
            connect_timeout=settings.OLLAMA_CONNECT_TIMEOUT,
            read_timeout=settings.OLLAMA_READ_TIMEOUT,
            cache_ttl=settings.OLLAMA_MODEL_CACHE_SECONDS,
        )
        app.state.ollama = ollama
        app.state.chat_limiter = GenerationLimiter(
            settings.OLLAMA_MAX_CONCURRENT,
            settings.OLLAMA_MAX_QUEUE,
            settings.OLLAMA_QUEUE_RETRY_AFTER,
        )
        # Read-only broker adapters by broker name: Dhan (Phase B), the MT5 bot export (Phase C).
        app.state.trading_adapters = {}
        dhan = build_dhan_adapter(settings, redis, app.state.sessionmaker)
        if dhan is not None:
            app.state.trading_adapters["dhan"] = dhan
        mt5 = build_mt5_adapter(settings)
        if mt5 is not None:
            app.state.trading_adapters["mt5"] = mt5
        # GitHub REST client (docs/18-github-foundation.md); None without GITHUB_TOKEN.
        github = build_github_client(settings)
        app.state.github = github
        # Claude PR reviews (docs/22-claude-review.md): off unless CLAUDE_REVIEW_ENABLED.
        app.state.claude_cli = ClaudeCLI(settings)
        app.state.review_runner = ReviewRunner(settings.CLAUDE_REVIEW_MAX_QUEUE)
        registry = AgentRegistry(app.state.sessionmaker)
        app.state.registry = registry
        app.state.agent_paused = {}
        limiter = app.state.chat_limiter

        def ollama_state() -> tuple[str, dict[str, object]]:
            details = {
                "running": limiter.running,
                "admitted": limiter.admitted,
                "capacity": limiter.capacity,
            }
            return ("busy" if limiter.running else "idle", details)

        def ollama_paused(paused: bool) -> None:
            app.state.agent_paused[OLLAMA.id] = paused

        heartbeat = Heartbeat(
            registry, OLLAMA, settings.AGENT_HEARTBEAT_SECONDS, ollama_state, ollama_paused
        )
        heartbeat.start()  # registers in the background; a DB outage doesn't block startup
        # Incidents (docs/21-self-repair.md): captured exceptions are written in the
        # background, and Docker health is probed here (only the API reaches the proxy).
        flusher = incidents.start_exception_flusher(app.state.sessionmaker, settings, "api")
        docker_stop = asyncio.Event()
        docker_task = None
        if settings.INCIDENT_DOCKER_PROBE_SECONDS > 0 and settings.DOCKER_API_URL:
            docker_task = asyncio.create_task(
                incidents.docker_monitor(
                    app.state.sessionmaker,
                    incidents.proxy_docker_probe(http, settings),
                    settings.INCIDENT_DOCKER_PROBE_SECONDS,
                    docker_stop,
                ),
                name="incident-docker-monitor",
            )
        log.info("startup", env=settings.ENV.value)
        try:
            yield
        finally:
            log.info("shutdown")
            docker_stop.set()
            if docker_task is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(docker_task, 5)
            await incidents.stop_exception_flusher(flusher)
            await heartbeat.stop()
            await app.state.review_runner.aclose()
            if dhan is not None:
                await dhan.aclose()
            if mt5 is not None:
                await mt5.aclose()
            if github is not None:
                await github.aclose()
            await ollama.aclose()
            await http.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Aureon_Agentic",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.docs_enabled else None,
    )
    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["content-type", "x-csrf-token", "x-request-id"],
        )
    if settings.cf_access_enabled:
        # Phase 22 (docs/24): verify the Cloudflare Access JWT, except on the bypass paths.
        app.add_middleware(CloudflareAccessMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(api_router)
    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn --factory app.main:app_factory`."""
    return create_app()
