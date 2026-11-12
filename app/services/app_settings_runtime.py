"""Applying a settings change inside the API process (docs/settings.md).

Routes read `request.app.state.settings` and the clients on `app.state` per request, so a
change is a swap: install the new Settings, rebuild only the clients whose keys changed, and
close the old clients a little later so requests already holding them can finish.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import structlog
from fastapi import FastAPI

from app.core.config import Settings
from app.services import app_settings

log = structlog.get_logger(__name__)

RETIRE_AFTER_SECONDS = 60.0


async def _retire(*clients: Any) -> None:
    await asyncio.sleep(RETIRE_AFTER_SECONDS)
    for client in clients:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()


def apply(app: FastAPI, new: Settings) -> list[str]:
    """Swap `new` in; returns the registry keys that changed."""
    from app.agents.trading.dhan import build_dhan_adapter
    from app.ai.gateway import ClaudeGateway
    from app.integrations.github import build_github_client

    state = app.state
    old: Settings = state.settings
    changed = app_settings.changed_keys(old, new, ("",))
    app_settings.install(new)
    state.settings = new
    if not changed:
        return changed
    retired: list[Any] = []
    if any(k.startswith(("ANTHROPIC_", "AI_")) for k in changed):
        # The gateway holds the key and reads its limits from the settings it was built with.
        retired.append(getattr(state, "ai", None))
        state.ai = ClaudeGateway.build(new, state.sessionmaker)
    if "GITHUB_TOKEN" in changed:
        retired.append(state.github)
        state.github = build_github_client(new)
    if any(k.startswith("DHAN_") for k in changed):
        adapters = dict(state.trading_adapters)
        retired.append(adapters.pop("dhan", None))
        dhan = build_dhan_adapter(new, state.redis, state.sessionmaker)
        if dhan is not None:
            adapters["dhan"] = dhan
        state.trading_adapters = adapters
    if any(c is not None for c in retired):
        task = asyncio.create_task(_retire(*retired), name="app-settings-retire")
        state.settings_retiring = {*getattr(state, "settings_retiring", set()), task}
        task.add_done_callback(state.settings_retiring.discard)
    log.info("app_settings_applied", keys=changed)
    return changed


async def reload(app: FastAPI) -> list[str]:
    """Re-read core.app_settings over the .env settings and apply the result."""
    new = await app_settings.effective(app.state.sessionmaker, app.state.env_settings)
    changed = apply(app, new)
    await report(app)
    return changed


async def report(app: FastAPI) -> None:
    """Record what this API process now runs with, for settings diagnostics."""
    from datetime import UTC, datetime

    app.state.settings_applied_at = datetime.now(UTC)
    await app_settings.report_applied(app.state.redis, app.state.settings, "api", ("",))


def start_listener(app: FastAPI, stop: asyncio.Event) -> asyncio.Task[None]:
    async def on_change() -> bool:
        try:
            await reload(app)
        except Exception as exc:  # keep the old settings; never log values
            log.error("app_settings_reload_failed", error=type(exc).__name__)
        return True

    return asyncio.create_task(
        app_settings.watch(app.state.sessionmaker, app.state.redis, on_change, stop),
        name="app-settings-listener",
    )


async def close_clients(app: FastAPI) -> None:
    """Shutdown: the current clients (they may have been rebuilt since startup)."""
    state = app.state
    for task in list(getattr(state, "settings_retiring", ())):
        task.cancel()
    for client in (
        state.trading_adapters.get("dhan"),
        state.trading_adapters.get("mt5"),
        state.github,
        getattr(state, "ai", None),
    ):
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()
