"""Runtime settings and secrets edited from Spasta (docs/settings.md).

THE MODEL. A short allowlist of keys (REGISTRY) can be set from the admin UI. Each value is
stored Fernet-encrypted in core.app_settings under SETTINGS_MASTER_KEY, and overrides the
same key from .env; .env stays the fallback, and deleting the row reverts to it. Bootstrap
keys (DATABASE_URL, SECRET_KEY, SETTINGS_MASTER_KEY, Redis, the LIVE trading flags) are not in
the registry and can never be written here.

THE OVERLAY. `overlay(env, overrides)` re-validates the .env settings with the DB values on
top, through the same pydantic validators .env goes through, so a DB value is parsed exactly
like the env var it replaces. `get_settings()` returns the overlay once it is installed.

RELOAD. A write publishes on Redis (CHANNEL). The API swaps `app.state.settings` and rebuilds
the clients that hold a credential (OpenRouter, Anthropic, Gemini, GitHub, Dhan) in place.
Workers build their clients once at startup, so a worker whose keys changed shuts down
gracefully and re-executes itself in the same container (`run_worker`): no container
restart, no `docker compose` call.
Workers without Redis (the coding worker) notice by polling a DB fingerprint.

SECRECY. Values never leave this module except into Settings: the API returns masks, tests
return outcomes, audit rows carry key names, and every secret value is registered with the
log scrubber (app/core/logging.py), which also covers incidents and AI incident briefs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal, NoReturn
from urllib.parse import urlsplit

import httpx
import structlog
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import SecretStr, ValidationError
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_env_settings, jwt_expiry, set_effective_settings
from app.core.logging import register_secret_values
from app.core.prod_checks import (
    INGEST_TOKEN_DISTINCT_FROM,
    ingest_token_reuse_problem,
    ingest_token_strength_problem,
)
from app.models.app_settings import AppSetting

log = structlog.get_logger(__name__)

CHANNEL = "aureon:settings:changed"
Kind = Literal[
    "str", "secret", "bool", "decimal", "int", "float", "url", "csv", "json_list", "json_object"
]


@dataclass(frozen=True, slots=True)
class Spec:
    key: str
    category: str
    kind: Kind
    description: str
    # Writing or reverting it needs the admin's password: every secret, and the URLs a
    # credential is sent to (pointing DHAN_BASE_URL elsewhere would hand over the token).
    reauth: bool = False
    # Written only by its own flow (the Dhan login), never by PUT/DELETE /api/v1/settings.
    # Listed (masked) so the admin sees it; part of the overlay like any other key.
    managed: bool = False

    @property
    def secret(self) -> bool:
        return self.kind == "secret"


def _s(
    key: str, cat: str, kind: Kind, desc: str, reauth: bool = False, managed: bool = False
) -> Spec:
    return Spec(key, cat, kind, desc, reauth or kind == "secret", managed)


CATEGORIES = (
    "openrouter",
    "anthropic",
    "gemini",
    "ai",
    "agents",
    "dhan",
    "github",
    "discord",
    "mt5",
)

REGISTRY: dict[str, Spec] = {
    s.key: s
    for s in (
        *(
            spec
            for agent, label in (
                ("NEWS", "News"),
                ("VOICE", "Voice answers"),
                ("RESEARCH", "Trading research"),
            )
            for spec in (
                _s(f"AGENT_{agent}_ENABLED", "agents", "bool", f"{label}: enabled"),
                _s(
                    f"AGENT_{agent}_PROVIDER",
                    "agents",
                    "str",
                    f"{label}: auto (the default external provider when allowed) | ollama"
                    " | openrouter | anthropic | gemini",
                ),
                _s(
                    f"AGENT_{agent}_MODEL",
                    "agents",
                    "str",
                    f"{label}: external model (blank = that provider's default)",
                ),
                _s(f"AGENT_{agent}_LOCAL_MODEL", "agents", "str", f"{label}: local Ollama model"),
            )
        ),
        _s("AGENT_IMAGE_ENABLED", "agents", "bool", "Image generation: enabled"),
        _s(
            "OPENROUTER_IMAGE_MODEL",
            "agents",
            "str",
            "OpenRouter image model (blank = local SD only; never personal/finance prompts)",
        ),
        _s(
            "MODEL_ROUTER_EXTERNAL_TIMEOUT_SECONDS",
            "agents",
            "float",
            "Agents: external timeout before local fallback (s)",
        ),
        _s("MODEL_ROUTER_LOCAL_QUEUE", "agents", "int", "Agents: local Ollama queue size"),
        _s("OPENROUTER_ENABLED", "openrouter", "bool", "Allow external models via OpenRouter"),
        _s("OPENROUTER_API_KEY", "openrouter", "secret", "OpenRouter API key"),
        _s("OPENROUTER_DEFAULT_MODEL", "openrouter", "str", "Default OpenRouter model id"),
        _s("OPENROUTER_DAILY_LIMIT_USD", "openrouter", "decimal", "Daily spend cap (USD)"),
        _s("OPENROUTER_ALLOWED_MODELS", "openrouter", "csv", "Allowed model ids, comma-separated"),
        _s(
            "AI_RESEARCH_EXTERNAL_ALLOWED",
            "openrouter",
            "bool",
            "Let research prompts go to external models",
        ),
        # --- direct provider APIs (docs/26-ai-providers.md) ---
        *(
            spec
            for prefix, cat, label in (
                ("ANTHROPIC", "anthropic", "Anthropic"),
                ("GEMINI", "gemini", "Gemini"),
            )
            for spec in (
                _s(f"{prefix}_ENABLED", cat, "bool", f"Allow external models via the {label} API"),
                _s(f"{prefix}_API_KEY", cat, "secret", f"{label} API key"),
                _s(f"{prefix}_DEFAULT_MODEL", cat, "str", f"Default {label} model id"),
                _s(
                    f"{prefix}_ALLOWED_MODELS",
                    cat,
                    "csv",
                    "Allowed model ids, comma-separated",
                ),
                _s(f"{prefix}_DAILY_LIMIT_USD", cat, "decimal", "Daily spend cap (USD)"),
                _s(
                    f"{prefix}_MODEL_PRICES",
                    cat,
                    "json_object",
                    'USD per million tokens: {"<model>": {"input": "..", "output": ".."}}'
                    " (a model without a price is never used)",
                ),
                _s(f"{prefix}_TIMEOUT_SECONDS", cat, "float", f"{label} timeout (s)"),
            )
        ),
        _s(
            "AI_DEFAULT_EXTERNAL_PROVIDER",
            "ai",
            "str",
            "Provider for new conversations and 'auto' agents: openrouter | anthropic | gemini",
        ),
        _s(
            "AI_EXTERNAL_FALLBACK_PROVIDERS",
            "ai",
            "csv",
            "External providers tried next, in order, before local (e.g. openrouter)",
        ),
        _s(
            "AI_EXTERNAL_DAILY_LIMIT_USD",
            "ai",
            "decimal",
            "Daily cap on total external spend across providers (USD)",
        ),
        _s(
            "AI_EXTERNAL_MAX_OUTPUT_TOKENS",
            "ai",
            "int",
            "Output tokens per external call when the caller sets none",
        ),
        _s("AI_EXTERNAL_MAX_RETRIES", "ai", "int", "SDK retries per external call"),
        _s(
            "AI_EXTERNAL_REQUESTS_PER_MINUTE",
            "ai",
            "int",
            "External calls per provider per minute (0 = no local limit)",
        ),
        _s("DHAN_CLIENT_ID", "dhan", "secret", "Dhan client id"),
        _s("DHAN_API_KEY", "dhan", "secret", "Dhan API key (web.dhan.co, Access DhanHQ APIs)"),
        _s("DHAN_API_SECRET", "dhan", "secret", "Dhan API secret"),
        _s(
            "DHAN_REDIRECT_URL",
            "dhan",
            "url",
            "Redirect URL registered with the API key: <this site>/api/v1/dhan/auth/callback",
            reauth=True,
        ),
        _s("DHAN_AUTH_URL", "dhan", "url", "Dhan login API base URL", reauth=True),
        _s(
            "DHAN_SESSION_TOKEN",
            "dhan",
            "secret",
            "Session token from 'Log in to Dhan' (set by the login, not by hand)",
            managed=True,
        ),
        _s(
            "DHAN_SESSION_EXPIRES_AT",
            "dhan",
            "str",
            "When the Dhan session expires (UTC; set by the login)",
            managed=True,
        ),
        _s("DHAN_SESSION_ALERT_MINUTES", "dhan", "int", "Alert this many minutes before expiry"),
        _s(
            "DHAN_ACCESS_TOKEN",
            "dhan",
            "secret",
            "Manual fallback: an access token from web.dhan.co (used without a Dhan login)",
        ),
        _s("DHAN_BASE_URL", "dhan", "url", "Dhan REST base URL", reauth=True),
        _s("DHAN_FEED_URL", "dhan", "url", "Dhan market feed URL (wss)", reauth=True),
        _s("DHAN_INSTRUMENT_CSV_URL", "dhan", "url", "Dhan instrument master CSV", reauth=True),
        _s("DHAN_HTTP_TIMEOUT", "dhan", "float", "Dhan HTTP timeout (s)"),
        _s("DHAN_TOKEN_WARN_HOURS", "dhan", "float", "Warn this many hours before expiry"),
        _s("DHAN_FEED_INSTRUMENTS", "dhan", "csv", "Feed instruments, comma-separated"),
        _s("DHAN_FEED_MODE", "dhan", "str", "Feed mode"),
        _s("DHAN_FEED_QUEUE_MAX", "dhan", "int", "Feed queue size"),
        _s("DHAN_FEED_STALE_SECONDS", "dhan", "float", "Feed staleness threshold (s)"),
        _s("DHAN_FEED_MAX_BACKOFF_SECONDS", "dhan", "float", "Feed reconnect backoff cap (s)"),
        _s("DHAN_RECONCILE_CRON", "dhan", "str", "Reconcile schedule (cron)"),
        _s("DHAN_INSTRUMENTS_CRON", "dhan", "str", "Instrument refresh schedule (cron)"),
        _s("DHAN_BACKFILL_CRON", "dhan", "str", "Backfill schedule (cron)"),
        _s("DHAN_BACKFILL_INSTRUMENTS", "dhan", "csv", "Backfill instruments, comma-separated"),
        _s("DHAN_BACKFILL_TIMEFRAMES", "dhan", "csv", "Backfill timeframes, comma-separated"),
        _s("DHAN_BACKFILL_DAYS", "dhan", "int", "Backfill depth (days)"),
        _s("DHAN_STATIC_IP", "dhan", "str", "Static IP registered with Dhan"),
        _s("DHAN_SESSION_CHECK_CRON", "dhan", "str", "Credential verification schedule (cron)"),
        _s("DHAN_MONITOR_CRON", "dhan", "str", "Dhan monitoring schedule (cron)"),
        _s("DHAN_MARKET_HOLIDAYS", "dhan", "csv", "NSE holidays (YYYY-MM-DD, comma-separated)"),
        _s("DHAN_FEED_STATUS_MAX_AGE_SECONDS", "dhan", "float", "Feed status max age (s)"),
        _s("DHAN_FEED_CRASH_LOOP_STARTS", "dhan", "int", "Feed starts in 10 min before delaying"),
        _s("DHAN_FEED_RECONNECT_COOLDOWN_SECONDS", "dhan", "float", "Feed reconnect cooldown (s)"),
        _s("GITHUB_TOKEN", "github", "secret", "GitHub token"),
        _s(
            "GITHUB_CODING_REPO_ALLOWLIST",
            "github",
            "json_list",
            'Repos the coding agent may use, JSON list: ["owner/name"]',
        ),
        _s("DISCORD_WEBHOOK_URL", "discord", "secret", "Discord webhook URL"),
        _s("MT5_INGEST_TOKEN", "mt5", "secret", "Token the MT5 bot pushes with"),
    )
}

# Named so a request for them gets a clear refusal rather than "unknown key".
BOOTSTRAP_KEYS = frozenset(
    {
        "DATABASE_URL",
        "MIGRATIONS_DATABASE_URL",
        "SECRET_KEY",
        "SETTINGS_MASTER_KEY",
        "REDIS_URL",
        "TRADING_LIVE_ENABLED",
        "PHASE_G_APPROVED",
    }
)

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


class SettingsError(Exception):
    """A refusal whose message is safe to show: it never contains the value."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# --- encryption ---------------------------------------------------------------------------


class Cipher:
    """MultiFernet over SETTINGS_MASTER_KEY: the first key encrypts, all keys decrypt."""

    def __init__(self, master: SecretStr | str | None) -> None:
        raw = master.get_secret_value() if isinstance(master, SecretStr) else (master or "")
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not keys:
            raise SettingsError("settings_key_missing", "SETTINGS_MASTER_KEY is not set")
        try:
            self._fernet = MultiFernet([Fernet(k.encode()) for k in keys])
        except (ValueError, TypeError) as exc:
            raise SettingsError(
                "settings_key_invalid", "SETTINGS_MASTER_KEY is not a valid Fernet key"
            ) from exc

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()

    def rotate(self, token: str) -> str:
        return self._fernet.rotate(token.encode()).decode()


# --- values -------------------------------------------------------------------------------


def normalize(spec: Spec, raw: str) -> str:
    """The canonical env-style string for a submitted value, or SettingsError (no echo)."""
    value = raw.strip()
    if not value and spec.kind not in ("csv", "json_list"):
        raise SettingsError("empty_value", f"{spec.key} cannot be empty; revert it instead")
    if len(value) > 8192:
        raise SettingsError("value_too_long", f"{spec.key} is too long")
    if spec.kind == "bool":
        if value.lower() in _TRUE:
            return "true"
        if value.lower() in _FALSE:
            return "false"
        raise SettingsError("invalid_value", f"{spec.key} must be true or false")
    if spec.kind in ("decimal", "float"):
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise SettingsError("invalid_value", f"{spec.key} must be a number") from exc
        if not number.is_finite() or number < 0:
            raise SettingsError("invalid_value", f"{spec.key} must be a number >= 0")
        return str(number)
    if spec.kind == "int":
        if not value.isdigit():
            raise SettingsError("invalid_value", f"{spec.key} must be a whole number >= 0")
        return str(int(value))
    if spec.kind == "url":
        parts = urlsplit(value)
        if parts.scheme not in ("https", "wss") or not parts.netloc:
            raise SettingsError("invalid_value", f"{spec.key} must be an https:// or wss:// URL")
        return value
    if spec.kind == "csv":
        return ",".join(p.strip() for p in value.split(",") if p.strip())
    if spec.kind == "json_object":
        try:
            obj = json.loads(value)
        except ValueError as exc:
            raise SettingsError("invalid_value", f"{spec.key} must be a JSON object") from exc
        if not isinstance(obj, dict):
            raise SettingsError("invalid_value", f"{spec.key} must be a JSON object")
        return json.dumps(obj, sort_keys=True, default=str)
    if spec.kind == "json_list":
        if not value:
            return "[]"
        try:
            items = json.loads(value)
        except ValueError as exc:
            raise SettingsError("invalid_value", f"{spec.key} must be a JSON list") from exc
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise SettingsError("invalid_value", f"{spec.key} must be a JSON list of strings")
        return json.dumps([i.strip() for i in items if i.strip()])
    if spec.key == "MT5_INGEST_TOKEN" and (problem := ingest_token_strength_problem(value)):
        # Same bar as the ENV=prod checklist; a managed value never bypasses it.
        raise SettingsError("invalid_value", problem)
    if spec.key == "DISCORD_WEBHOOK_URL":
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or parts.hostname
            not in (
                "discord.com",
                "discordapp.com",
                "canary.discord.com",
                "ptb.discord.com",
            )
            or not parts.path.startswith("/api/webhooks/")
        ):
            raise SettingsError(
                "invalid_value", "DISCORD_WEBHOOK_URL must be https://discord.com/api/webhooks/..."
            )
    return value


def overlay(env: Settings, overrides: Mapping[str, str]) -> Settings:
    """`env` with the DB values on top, through the same validators .env goes through.

    model_validate (not Settings()) so the environment is not read a second time; the dump
    keeps SecretStr and parsed lists, which the validators pass through unchanged.
    """
    usable = {k: v for k, v in overrides.items() if k in REGISTRY}
    if not usable:
        return env
    data = env.model_dump()
    data.update(usable)
    return Settings.model_validate(data)


def checked_overlay(env: Settings, overrides: Mapping[str, str], key: str) -> Settings:
    try:
        merged = overlay(env, overrides)
    except ValidationError as exc:
        # Field names and error types only: pydantic's messages include the input value.
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")} or {key})
        raise SettingsError(
            "invalid_value", f"rejected by validation: {', '.join(fields)}"
        ) from None
    # One value must never serve as two credentials (the MT5 ingest token vs any other).
    if key in ("MT5_INGEST_TOKEN", *INGEST_TOKEN_DISTINCT_FROM) and (
        problem := ingest_token_reuse_problem(merged)
    ):
        raise SettingsError("invalid_value", problem)
    return merged


def current_value(settings: Settings, key: str) -> str | None:
    """A field as the env-style string it was set from (None when unset/empty)."""
    value: Any = getattr(settings, key, None)
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str) if value else None
    if isinstance(value, list):
        spec = REGISTRY.get(key)
        if spec is not None and spec.kind == "json_list":
            return json.dumps(value) if value else None
        return ",".join(str(v) for v in value) or None
    text = str(value)
    return text if text.strip() else None


def mask(value: str | None, secret: bool) -> str | None:
    """Never the value: four bullets, plus the last four characters of a long enough one."""
    if value is None:
        return None
    if secret and len(value) < 16:
        return "••••"
    return "••••" + value[-4:] if len(value) > 4 else "••••"


def as_utc(value: datetime | None) -> datetime | None:
    """A stored timestamp as an aware UTC datetime (naive values are UTC)."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def dhan_token_expiry(token: str | None) -> datetime | None:
    """The `exp` claim of a Dhan access token (a JWT), read without verifying it."""
    return jwt_expiry(token)


def secret_values(settings: Settings) -> list[str]:
    """Every credential this process holds, for the log scrubber."""
    values = [current_value(settings, k) for k, s in REGISTRY.items() if s.secret]
    for name in ("SECRET_KEY", "DATABASE_URL", "MIGRATIONS_DATABASE_URL", "SETTINGS_MASTER_KEY"):
        values.append(current_value(settings, name))
    master = current_value(settings, "SETTINGS_MASTER_KEY")
    if master:
        values.extend(master.split(","))
    for name in ("GITHUB_WEBHOOK_SECRET", "MT5_STATUS_TOKEN"):
        values.append(current_value(settings, name))
    return [v for v in values if v]


def install(settings: Settings) -> None:
    """Make `settings` this process's get_settings() and teach the scrubber its secrets."""
    set_effective_settings(settings)
    register_secret_values(secret_values(settings))


# --- storage ------------------------------------------------------------------------------


async def load_overrides(db: AsyncSession, cipher: Cipher | None) -> dict[str, str]:
    """Decrypted DB values for registry keys. A row that will not decrypt is skipped."""
    if cipher is None:
        return {}
    out: dict[str, str] = {}
    for row in (await db.scalars(select(AppSetting))).all():
        if row.key not in REGISTRY:
            continue
        try:
            out[row.key] = cipher.decrypt(row.value_encrypted)
        except InvalidToken:
            log.error("app_setting_undecryptable", key=row.key)
    return out


async def fingerprint(db: AsyncSession) -> tuple[int, int, str]:
    """Changes whenever a row is written or deleted: count, version sum, latest write."""
    count, versions, latest = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(AppSetting.version), 0),
                func.max(AppSetting.updated_at),
            )
        )
    ).one()
    return int(count), int(versions), str(latest)


def cipher_for(settings: Settings) -> Cipher | None:
    try:
        return Cipher(settings.SETTINGS_MASTER_KEY)
    except SettingsError as exc:
        log.warning("app_settings_disabled", reason=exc.code)
        return None


async def effective(maker: async_sessionmaker[AsyncSession], env: Settings) -> Settings:
    """.env overlaid with the DB; .env alone when the DB or the key is unavailable."""
    cipher = cipher_for(env)
    if cipher is None:
        return env
    try:
        async with maker() as db:
            overrides = await load_overrides(db, cipher)
        return overlay(env, overrides)
    except ValidationError:
        log.error("app_settings_invalid_overlay")  # values not logged
        return env
    except Exception as exc:  # DB down at boot: run on .env, the watcher catches up
        log.warning("app_settings_unavailable", error=type(exc).__name__)
        return env


async def publish(redis: Redis, keys: Iterable[str]) -> None:
    with contextlib.suppress(Exception):  # the fingerprint poll is the safety net
        await redis.publish(CHANNEL, json.dumps({"keys": sorted(keys)}))


def changed_keys(old: Settings, new: Settings, prefixes: tuple[str, ...]) -> list[str]:
    return [
        k
        for k in REGISTRY
        if k.startswith(prefixes) and current_value(old, k) != current_value(new, k)
    ]


async def watch(
    maker: async_sessionmaker[AsyncSession],
    redis: Redis | None,
    on_change: Callable[[], Awaitable[bool]],
    stop: asyncio.Event,
    *,
    poll_seconds: float = 60.0,
) -> None:
    """Call `on_change` on every publish and whenever the DB fingerprint moves; returns when
    `stop` is set or `on_change` returns False (the caller is shutting down)."""
    last: tuple[int, int, str] | None = None

    async def moved() -> bool:
        nonlocal last
        try:
            async with maker() as db:
                now = await fingerprint(db)
        except Exception:
            return False
        was, last = last, now
        return was is not None and was != now

    await moved()
    pubsub = None
    if redis is not None:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(CHANNEL)
        except Exception as exc:
            log.warning("app_settings_pubsub_unavailable", error=type(exc).__name__)
            pubsub = None
    loop = asyncio.get_running_loop()
    next_poll = loop.time() + poll_seconds
    try:
        while not stop.is_set():
            fire = False
            if pubsub is not None:
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                    fire = msg is not None
                except Exception:
                    await asyncio.sleep(5)
            else:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), min(poll_seconds, 15.0))
            if loop.time() >= next_poll or pubsub is None:
                next_poll = loop.time() + poll_seconds
                fire = await moved() or fire
            elif fire:
                await moved()  # keep the fingerprint current
            if fire and not await on_change():
                return
    finally:
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.aclose()  # type: ignore[no-untyped-call]


# --- workers ------------------------------------------------------------------------------


class _Reload:
    requested = False


RELOAD = _Reload()
_WATCHERS: set[asyncio.Task[None]] = set()


async def worker_settings() -> Settings:
    """A worker's settings at startup: .env overlaid with core.app_settings."""
    from app.db.session import build_engine, build_sessionmaker

    env = get_env_settings()
    engine = build_engine(env)
    try:
        settings = await effective(build_sessionmaker(engine), env)
    finally:
        await engine.dispose()
    install(settings)
    return settings


def start_worker_watch(
    settings: Settings,
    maker: async_sessionmaker[AsyncSession],
    stop: asyncio.Event,
    prefixes: tuple[str, ...],
    redis: Redis | None = None,
    env: Settings | None = None,
) -> asyncio.Task[None]:
    """When a key with one of `prefixes` changes, stop the worker gracefully and ask
    run_worker to re-exec it (its clients are built once, from the values at startup)."""

    async def on_change() -> bool:
        new = await effective(maker, env or get_env_settings())
        keys = changed_keys(settings, new, prefixes)
        if not keys:
            return True
        log.info("app_settings_changed_reloading", keys=keys)
        RELOAD.requested = True
        stop.set()
        return False

    task = asyncio.create_task(
        watch(maker, redis, on_change, stop, poll_seconds=30.0), name="app-settings-watch"
    )
    _WATCHERS.add(task)  # a strong reference: the loop only keeps a weak one
    task.add_done_callback(_WATCHERS.discard)
    return task


def run_worker(main: Callable[[], Coroutine[Any, Any, int]], module: str) -> NoReturn:
    """`asyncio.run(main())`, then re-exec the same module when a settings change asked for
    it: the container keeps running, the process starts over with the new values."""
    rc = asyncio.run(main())
    if RELOAD.requested:
        sys.stdout.flush()
        sys.stderr.flush()
        # Same interpreter, fixed module name: nothing user-supplied reaches the argv.
        os.execv(sys.executable, [sys.executable, "-m", module])  # noqa: S606
    raise SystemExit(rc)


# --- connection tests ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TestOutcome:
    ok: bool
    detail: str


async def run_test(category: str, settings: Settings, http: httpx.AsyncClient) -> TestOutcome:
    """Try the category's credential against its service. The detail is a status line,
    never a response body (bodies can echo what was sent)."""
    try:
        return await _TESTS[category](settings, http)
    except httpx.HTTPError as exc:
        return TestOutcome(False, f"request failed ({type(exc).__name__})")


def _status(resp: httpx.Response, what: str) -> TestOutcome:
    if resp.is_success:
        return TestOutcome(True, f"{what}: OK")
    if resp.status_code in (401, 403):
        return TestOutcome(False, f"{what}: rejected ({resp.status_code})")
    return TestOutcome(False, f"{what}: HTTP {resp.status_code}")


async def _openrouter(s: Settings, http: httpx.AsyncClient) -> TestOutcome:
    key = current_value(s, "OPENROUTER_API_KEY")
    if not key:
        return TestOutcome(False, "OPENROUTER_API_KEY is not set")
    base = s.OPENROUTER_BASE_URL.rstrip("/")
    headers = {"Authorization": f"Bearer {key}"}
    # /key checks the key itself; /models (public) checks the default model exists.
    who = await http.get(f"{base}/key", headers=headers, timeout=10)
    if not who.is_success:
        return _status(who, "OpenRouter key")
    model = s.OPENROUTER_DEFAULT_MODEL.strip()
    if model:
        models = await http.get(f"{base}/models", headers=headers, timeout=15)
        if not models.is_success:
            return _status(models, "OpenRouter models")
        ids = {m.get("id") for m in models.json().get("data", []) if isinstance(m, dict)}
        if model not in ids:
            return TestOutcome(False, "key OK; OPENROUTER_DEFAULT_MODEL is not an OpenRouter model")
    return TestOutcome(True, "OpenRouter key OK" + (" and default model found" if model else ""))


async def _external_models(s: Settings, provider: str, label: str) -> TestOutcome:
    """List the provider's models with the key (no tokens are spent) and check the default
    model exists and is priced. Errors are status lines from ProviderError, never bodies."""
    from app.agents.providers.base import ProviderError
    from app.services.inference import build_external

    cfg = s.external(provider)
    if not cfg.api_key:
        return TestOutcome(False, f"{provider.upper()}_API_KEY is not set")
    # Built even when disabled, so a key can be tested before it is switched on.
    probe = s.model_copy(update={f"{provider.upper()}_ENABLED": True})
    client = build_external(probe, provider)
    if client is None:
        return TestOutcome(False, f"{label} client could not be built")
    try:
        ids = set(await client.list_models())
    except ProviderError as exc:
        return TestOutcome(False, f"{label} key: {exc.message}")
    finally:
        await client.aclose()
    model = cfg.default_model
    if not model:
        return TestOutcome(True, f"{label} key OK ({len(ids)} models); no default model set")
    if model.removeprefix("models/") not in ids:
        return TestOutcome(False, f"key OK; {provider.upper()}_DEFAULT_MODEL is not available")
    if not cfg.priced(model):
        return TestOutcome(
            False, f"key OK; the default model has no price in {provider.upper()}_MODEL_PRICES"
        )
    return TestOutcome(True, f"{label} key OK and default model found")


async def _anthropic(s: Settings, http: httpx.AsyncClient) -> TestOutcome:
    return await _external_models(s, "anthropic", "Anthropic")


async def _gemini(s: Settings, http: httpx.AsyncClient) -> TestOutcome:
    return await _external_models(s, "gemini", "Gemini")


async def _ai(s: Settings, http: httpx.AsyncClient) -> TestOutcome:
    """The shared routing settings are coherent (no network call)."""
    chain = s.external_chain(s.AI_DEFAULT_EXTERNAL_PROVIDER)
    states = [(p, s.external_unusable_reason(p)) for p in chain]
    order = " -> ".join(f"{p} ({r or 'usable'})" for p, r in states) + " -> local"
    cap = s.AI_EXTERNAL_DAILY_LIMIT_USD
    cap_text = f"global cap {cap} USD" if cap is not None else "global cap not set"
    if all(r for _, r in states):
        return TestOutcome(False, f"no external provider is usable: {order}")
    return TestOutcome(True, f"order: {order}; {cap_text}")


async def _dhan(s: Settings, http: httpx.AsyncClient) -> TestOutcome:
    cid, secret = current_value(s, "DHAN_CLIENT_ID"), s.dhan_access_token()
    if not cid or secret is None:
        return TestOutcome(
            False, "set DHAN_CLIENT_ID and log in to Dhan (or set DHAN_ACCESS_TOKEN)"
        )
    token = secret.get_secret_value()
    resp = await http.get(
        f"{s.DHAN_BASE_URL.rstrip('/')}/fundlimit",
        headers={"access-token": token, "client-id": cid, "Accept": "application/json"},
        timeout=s.DHAN_HTTP_TIMEOUT,
    )
    return _status(resp, "Dhan funds read")


async def _github(s: Settings, http: httpx.AsyncClient) -> TestOutcome:
    token = current_value(s, "GITHUB_TOKEN")
    if not token:
        return TestOutcome(False, "GITHUB_TOKEN is not set")
    resp = await http.get(
        f"{s.GITHUB_API_URL.rstrip('/')}/user",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=10,
    )
    if resp.is_success:
        login = resp.json().get("login")
        return TestOutcome(True, f"GitHub token OK (user {login})" if login else "GitHub token OK")
    return _status(resp, "GitHub /user")


async def _discord(s: Settings, http: httpx.AsyncClient) -> TestOutcome:
    url = current_value(s, "DISCORD_WEBHOOK_URL")
    if not url:
        return TestOutcome(False, "DISCORD_WEBHOOK_URL is not set")
    # GET reads the webhook's metadata and posts nothing to the channel.
    return _status(await http.get(url, timeout=10), "Discord webhook")


async def _mt5(s: Settings, http: httpx.AsyncClient) -> TestOutcome:
    token = current_value(s, "MT5_INGEST_TOKEN")
    if not token:
        return TestOutcome(False, "MT5_INGEST_TOKEN is not set")
    if problem := ingest_token_strength_problem(token) or ingest_token_reuse_problem(s):
        return TestOutcome(False, problem)
    return TestOutcome(True, "MT5_INGEST_TOKEN is set (the bot proves it on its next push)")


async def _agents(s: Settings, http: httpx.AsyncClient) -> TestOutcome:
    """The agents' local models are installed in Ollama (GET /api/tags; nothing is pulled)."""
    resp = await http.get(f"{s.OLLAMA_BASE_URL.rstrip('/')}/api/tags", timeout=10.0)
    if resp.status_code != 200:
        return TestOutcome(False, f"Ollama answered HTTP {resp.status_code}")
    installed = {m.get("name") for m in resp.json().get("models", []) if isinstance(m, dict)}
    names = (current_value(s, f"AGENT_{a}_LOCAL_MODEL") for a in ("NEWS", "VOICE", "RESEARCH"))
    wanted: set[str] = {n for n in names if n}
    missing = sorted(m for m in wanted if m not in installed)
    if missing:
        return TestOutcome(False, f"not installed in Ollama: {', '.join(missing)}")
    return TestOutcome(True, f"local models installed: {', '.join(sorted(wanted))}")


_TESTS: dict[str, Callable[[Settings, httpx.AsyncClient], Awaitable[TestOutcome]]] = {
    "agents": _agents,
    "openrouter": _openrouter,
    "anthropic": _anthropic,
    "gemini": _gemini,
    "ai": _ai,
    "dhan": _dhan,
    "github": _github,
    "discord": _discord,
    "mt5": _mt5,
}
