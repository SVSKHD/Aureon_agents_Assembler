"""FastAPI application factory and lifespan."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from redis.asyncio import Redis

from app.agents.registry import CLAUDE, AgentRegistry, Heartbeat
from app.agents.trading.dhan import build_dhan_adapter
from app.agents.trading.mt5 import build_mt5_adapter
from app.ai.gateway import ClaudeGateway
from app.api import ui
from app.api.router import api_router
from app.auth.deps import Current
from app.core.cf_access import CloudflareAccessMiddleware
from app.core.config import Settings, get_env_settings
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    RequestIdMiddleware,
    SchemaHeaderMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.prod_checks import assert_production_ready
from app.db.session import build_engine, build_sessionmaker
from app.integrations.claude_cli import ClaudeCLI
from app.integrations.github import build_github_client
from app.services import app_settings, app_settings_runtime, incidents
from app.services.claude_review import ReviewRunner
from app.services.health import HealthService
from app.services.mt5_ingest import DbIngestStore

log = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_env_settings()
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
        await ui.announce_schema(redis, app.state.schema_hash)
        # Runtime settings (docs/settings.md): .env overlaid with core.app_settings. `settings`
        # stays the .env view; `live` is what routes and clients see, swapped on change.
        app.state.env_settings = settings
        live = await app_settings.effective(app.state.sessionmaker, settings)
        app_settings.install(live)
        app.state.settings = live
        # What this process loaded, for /settings/diagnostics: in the background, never
        # holding up startup (Redis may be slow or down).
        settings_report = asyncio.create_task(
            app_settings_runtime.report(app), name="app-settings-report"
        )
        app.state.http = http
        app.state.health = HealthService(settings, engine, redis, http)
        # Claude, the only AI provider (docs/26-ai-providers.md): the gateway holds the key,
        # the budgets and the redaction; it is rebuilt when ANTHROPIC_/AI_ settings change.
        app.state.ai = ClaudeGateway.build(live, app.state.sessionmaker)
        # Read-only broker adapters by broker name: Dhan (Phase B), the MT5 bot export (Phase C).
        app.state.trading_adapters = {}
        dhan = build_dhan_adapter(live, redis, app.state.sessionmaker)
        if dhan is not None:
            app.state.trading_adapters["dhan"] = dhan
        mt5 = build_mt5_adapter(settings)
        if mt5 is not None:
            app.state.trading_adapters["mt5"] = mt5
        # MT5 push ingest (docs/12-mt5-adapter.md §3): POST /api/trading/mt5/ingest stores here.
        app.state.mt5_ingest_store = DbIngestStore(app.state.sessionmaker)
        # GitHub REST client (docs/18-github-foundation.md); None without GITHUB_TOKEN.
        github = build_github_client(live)
        app.state.github = github
        # Claude PR reviews (docs/22-claude-review.md): off unless CLAUDE_REVIEW_ENABLED.
        app.state.claude_cli = ClaudeCLI(settings)
        app.state.review_runner = ReviewRunner(settings.CLAUDE_REVIEW_MAX_QUEUE)
        registry = AgentRegistry(app.state.sessionmaker)
        app.state.registry = registry
        app.state.agent_paused = {}

        def claude_state() -> tuple[str, dict[str, object]]:
            s: Settings = app.state.settings
            details: dict[str, object] = {
                "configured": bool(app.state.ai.configured),
                "model_main": s.AI_MODEL_MAIN,
                "model_router": s.AI_MODEL_ROUTER or None,
            }
            return ("idle", details)  # `configured` in the details says whether it can answer

        def claude_paused(paused: bool) -> None:
            app.state.agent_paused[CLAUDE.id] = paused

        heartbeat = Heartbeat(
            registry, CLAUDE, settings.AGENT_HEARTBEAT_SECONDS, claude_state, claude_paused
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
        settings_stop = asyncio.Event()
        settings_task = None
        if app_settings.cipher_for(settings) is not None:
            settings_task = app_settings_runtime.start_listener(app, settings_stop)
        log.info("startup", env=settings.ENV.value)
        try:
            yield
        finally:
            log.info("shutdown")
            docker_stop.set()
            settings_stop.set()
            settings_report.cancel()
            if settings_task is not None:  # a listener: nothing to finish, just stop it
                settings_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await settings_task
            if docker_task is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(docker_task, 5)
            await incidents.stop_exception_flusher(flusher)
            await heartbeat.stop()
            await app.state.review_runner.aclose()
            # The current clients: a settings change may have replaced the startup ones.
            await app_settings_runtime.close_clients(app)
            await http.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Aureon_Agentic",
        version="0.1.0",
        lifespan=lifespan,
        # /api/openapi.json is served by app/api/v1/ui_manifest.py for signed-in users only;
        # Swagger (dev only) below reads it with the session cookie.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
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
    app.add_middleware(SchemaHeaderMiddleware, state=app.state)
    app.add_middleware(RequestIdMiddleware)
    app.include_router(api_router)
    if settings.docs_enabled:

        @app.get("/api/docs", include_in_schema=False)
        async def swagger(_: Current) -> Any:
            return get_swagger_ui_html(openapi_url="/api/openapi.json", title="Aureon API")

    # x-ui on every operation, and the schema hash (X-API-Schema, the stream's hello).
    ui.install_openapi(app)
    app.state.schema_hash = ui.schema_hash(app)
    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn --factory app.main:app_factory`."""
    return create_app()
