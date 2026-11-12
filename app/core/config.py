"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def jwt_expiry(token: str | None) -> datetime | None:
    """The `exp` claim of a JWT (a Dhan access token), read WITHOUT verifying it: only used to
    stop using a token that has certainly expired. None when there is no readable claim."""
    if not token or token.count(".") != 2:
        return None
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        exp = claims.get("exp") if isinstance(claims, dict) else None
        return datetime.fromtimestamp(float(exp), UTC) if exp is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _secret_text(value: SecretStr | str | None) -> str:
    """A secret's text, stripped ("" when unset). Accepts a plain str too (model_copy skips
    validation, so a copied Settings can hold one)."""
    if value is None:
        return ""
    raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
    return raw.strip()


# Claude list prices, USD per million tokens (input, output, cache read), from the Claude API
# model table (checked 2026-11-12). A cache write is billed at 1.25x input (5-minute TTL).
# Claude Haiku 5.5's prices are for prompts up to 100K tokens; above that 0.50 / 2.50.
# AI_MODEL_PRICES overrides or adds a model; a model with no price is never called.
CLAUDE_PRICES: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00"), Decimal("0.20")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00"), Decimal("0.20")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"), Decimal("0.01")),
}
# The budgets a call is charged to, and their default monthly caps (USD).
AI_AGENT_BUDGET_DEFAULTS: dict[str, Decimal] = {
    "investments": Decimal("40"),
    "business": Decimal("30"),
    "chat": Decimal("20"),
    "reserve": Decimal("10"),
}


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    ENV: Environment = Environment.DEV
    LOG_LEVEL: str = "INFO"
    # Never true in prod (startup refuses it; see app/core/prod_checks.py).
    DEBUG: bool = False
    # Swagger UI + /api/openapi.json. Unset = on outside prod, off in prod. True is refused in prod.
    API_DOCS_ENABLED: bool | None = None

    # postgresql+asyncpg://user:pass@<postgres-container>:5432/spasta
    DATABASE_URL: SecretStr
    # Phase 22 (docs/24-production-integration.md): the migrator role's URL, used ONLY by the
    # `migrate` service / alembic (it owns the app schemas). Unset = DATABASE_URL is used.
    # Keep it out of the shared .env in production (compose reads it from .env.migrate).
    MIGRATIONS_DATABASE_URL: SecretStr | None = None
    # redis://aureon-redis:6379/0
    REDIS_URL: str = "redis://aureon-redis:6379/0"
    IMAGE_AGENT_PATH: Path = Path("/opt/image-agent")
    DATA_ROOT: Path = Path("/mnt/spasta-data")
    # File that must exist on the HDD; proves the 2 TB disk is mounted, not an empty mountpoint.
    DATA_ROOT_MARKER: str = ".spasta-data-mounted"

    SECRET_KEY: SecretStr
    # Fernet key(s) for core.app_settings (docs/settings.md). Comma-separated: the FIRST key
    # encrypts, every key decrypts (rotation). Bootstrap only: never editable from the UI.
    SETTINGS_MASTER_KEY: SecretStr | None = None
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # Readiness-probe timeout per dependency, seconds.
    HEALTH_CHECK_TIMEOUT: float = 3.0

    # --- auth ---
    SESSION_TTL_HOURS: int = Field(default=12, ge=1, le=24 * 30)
    # Secure cookies (and the __Host- prefix). Browsers drop Secure cookies over plain http
    # except on localhost, so only disable this for local http development.
    COOKIE_SECURE: bool = True
    # Failed logins allowed per (email, client IP) and per client IP within the window.
    LOGIN_MAX_FAILURES: int = Field(default=5, ge=1)
    LOGIN_MAX_FAILURES_PER_IP: int = Field(default=20, ge=1)
    LOGIN_WINDOW_SECONDS: int = Field(default=900, ge=1)

    # --- imaging (docs/04-imaging.md) ---
    # "subprocess" runs IMAGE_AGENT_COMMAND; "stub" writes a placeholder PNG (tests/dev only).
    IMAGE_GENERATOR: Literal["subprocess", "stub"] = "subprocess"
    # JSON argv template for the existing /opt/image-agent entrypoint, run WITHOUT a shell.
    # Placeholders: {prompt} {negative_prompt} {seed} {steps} {width} {height} {output}.
    IMAGE_AGENT_COMMAND: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Extra env var NAMES copied into the entrypoint's environment (secrets are never passed).
    IMAGE_AGENT_ENV_PASSTHROUGH: Annotated[list[str], NoDecode] = Field(default_factory=list)
    IMAGE_THREADS: int = Field(default=3, ge=1)
    IMAGE_MODEL_NAME: str = "sd-1.5"
    IMAGE_ALLOWED_SIZES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["512x512", "512x768", "768x512"]
    )
    IMAGE_DEFAULT_STEPS: int = Field(default=25, ge=1)
    IMAGE_MAX_STEPS: int = Field(default=50, ge=1)
    # Jobs allowed to wait (status=queued) across all users; beyond that POST returns 429.
    IMAGE_QUEUE_MAX: int = Field(default=5, ge=1)
    IMAGE_RETRY_AFTER: int = Field(default=600, ge=1)
    IMAGE_JOB_TIMEOUT_SECONDS: float = Field(default=1800.0, gt=0)
    # A job interrupted by a worker stop is retried until it has started this many times.
    IMAGE_MAX_ATTEMPTS: int = Field(default=2, ge=1)
    # How often the worker checks for cancel requests and saves progress.
    IMAGE_POLL_SECONDS: float = Field(default=2.0, gt=0)
    IMAGE_QUEUE_KEY: str = "aureon:imaging:wake"
    IMAGE_STUB_DELAY: float = Field(default=0.0, ge=0)
    # Selectable local models (docs/image-agent.md): JSON {"name": {"file": "/abs/model.gguf",
    # "steps": 20, "cfg": 7.0, "sampler": "euler_a"}}. The job's model fills the command's
    # {model_path} {cfg} {sampler} placeholders; IMAGE_MODEL_NAME is the default. Empty = the
    # single IMAGE_MODEL_NAME as before (the command decides everything).
    IMAGE_MODELS: Annotated[dict[str, dict[str, Any]], NoDecode] = Field(default_factory=dict)
    # Generation yields: a running job is suspended (SIGSTOP) while a chat/voice request is
    # active, and no new job starts inside these windows (Asia/Kolkata, Mon-Fri).
    IMAGE_PAUSE_FOR_INTERACTIVE: bool = True
    IMAGE_PAUSE_WINDOWS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["09:00-09:35", "15:00-15:35"]
    )

    # --- agents / scheduler / monitoring (docs/05-agents.md) ---
    AGENT_HEARTBEAT_SECONDS: float = Field(default=15.0, gt=0)
    # An agent whose last heartbeat is older than this is reported as "stale".
    AGENT_STALE_SECONDS: float = Field(default=60.0, gt=0)
    AGENT_LOG_RETENTION_DAYS: int = Field(default=30, ge=1)
    AGENT_RUN_RETENTION_DAYS: int = Field(default=90, ge=1)
    SCHEDULER_TICK_SECONDS: float = Field(default=5.0, gt=0)
    # Cron expressions are evaluated in this zone.
    SCHEDULER_TIMEZONE: str = "Asia/Kolkata"
    # Read-only Docker API (a socket proxy that only allows GET /containers). Empty = off.
    DOCKER_API_URL: str = "http://aureon-docker-proxy:2375"
    # JSON written by deploy/host-status (systemctl is-active for a fixed unit list).
    HOST_STATUS_FILE: Path = Path("/mnt/spasta-data/status/host-services.json")
    HOST_STATUS_MAX_AGE_SECONDS: float = Field(default=120.0, gt=0)

    # --- trading (docs/10-trading-foundation.md) ---
    # Feature flag for LIVE mode. Off: LIVE cannot be selected at all. On: LIVE still needs
    # an admin confirmation token AND the Phase G preconditions (not implemented yet).
    TRADING_LIVE_ENABLED: bool = False
    # TRADING_LIVE_ENABLED=true is refused at load time unless this is exactly "yes" (the
    # Phase G sign-off). Even then LIVE stays unreachable until Phase G ships its preconditions.
    PHASE_G_APPROVED: str = ""
    TRADING_LIVE_TOKEN_TTL_SECONDS: int = Field(default=300, ge=30, le=3600)
    # Per-call timeout for read-only broker adapter calls.
    TRADING_ADAPTER_TIMEOUT: float = Field(default=10.0, gt=0)

    # --- Dhan (docs/11-dhan.md). Credentials come from env only and are never stored. ---
    DHAN_CLIENT_ID: str | None = None
    DHAN_ACCESS_TOKEN: SecretStr | None = None
    DHAN_BASE_URL: str = "https://api.dhan.co/v2"
    DHAN_FEED_URL: str = "wss://api-feed.dhan.co"
    DHAN_INSTRUMENT_CSV_URL: str = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
    DHAN_HTTP_TIMEOUT: float = Field(default=15.0, gt=0)
    # Warn (health detail + agent log) when the 24 h access token has less than this left.
    DHAN_TOKEN_WARN_HOURS: float = Field(default=4.0, ge=0)
    # Live feed worker: "SEGMENT:SECURITY_ID" list, e.g. "IDX_I:13,NSE_EQ:2885".
    DHAN_FEED_INSTRUMENTS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    DHAN_FEED_MODE: Literal["ticker", "quote", "full"] = "quote"
    DHAN_FEED_QUEUE_MAX: int = Field(default=10_000, ge=100)
    # A subscribed instrument with no tick for this long (market open) is marked stale.
    DHAN_FEED_STALE_SECONDS: float = Field(default=60.0, gt=0)
    DHAN_FEED_MAX_BACKOFF_SECONDS: float = Field(default=60.0, gt=0)
    # Scheduled jobs (created only when DHAN_CLIENT_ID is set).
    DHAN_RECONCILE_CRON: str = "*/5 9-15 * * 1-5"
    DHAN_INSTRUMENTS_CRON: str = "11 8 * * 1-5"
    DHAN_BACKFILL_CRON: str = "41 16 * * 1-5"
    DHAN_BACKFILL_INSTRUMENTS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    DHAN_BACKFILL_TIMEFRAMES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["1d", "15m"]
    )
    DHAN_BACKFILL_DAYS: int = Field(default=30, ge=1, le=3650)
    # --- instrument master, market data, screener (docs/35-market-data.md) ---
    # Exchange segments the instrument refresh keeps active. Currency and commodity segments
    # only when listed here explicitly.
    DHAN_INSTRUMENT_SEGMENTS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["NSE_EQ", "BSE_EQ", "IDX_I", "NSE_FNO", "BSE_FNO"]
    )
    # Readiness: the instrument master is stale after this long without a successful refresh.
    DHAN_INSTRUMENT_MAX_AGE_HOURS: float = Field(default=80.0, gt=0)
    DHAN_INSTRUMENT_SYNC_TIMEOUT_SECONDS: int = Field(default=1800, ge=60, le=7200)
    # Universes whose daily candles the `dhan-universe-backfill` job keeps current (opt-in:
    # each instrument is one Dhan data-API call). Names: nse_equities, bse_equities, indices,
    # fno_stocks, watchlists (every instrument in any watchlist).
    DHAN_UNIVERSE_BACKFILL: Annotated[list[str], NoDecode] = Field(default_factory=list)
    DHAN_UNIVERSE_BACKFILL_CRON: str = "55 16 * * 1-5"
    DHAN_UNIVERSE_BACKFILL_DAYS: int = Field(default=400, ge=30, le=3650)
    # Short-lived caches in front of rate-limited Dhan endpoints (quote 1/s, chain 1/3 s).
    MARKET_QUOTE_CACHE_SECONDS: float = Field(default=3.0, ge=0, le=300)
    OPTION_CHAIN_CACHE_SECONDS: float = Field(default=5.0, ge=0, le=300)
    OPTION_EXPIRY_CACHE_SECONDS: float = Field(default=900.0, ge=0, le=86_400)
    INDICATOR_CACHE_SECONDS: float = Field(default=60.0, ge=0, le=3600)
    # Per-user request budgets for endpoints that can reach Dhan or do heavy work.
    MARKET_USER_REQUESTS_PER_MINUTE: int = Field(default=120, ge=1, le=10_000)
    # History requests: depth caps and how many one user may have open at once.
    CANDLE_BACKFILL_MAX_DAYS_INTRADAY: int = Field(default=90, ge=1, le=365)
    CANDLE_BACKFILL_MAX_DAYS_DAILY: int = Field(default=3650, ge=30, le=7300)
    CANDLE_BACKFILL_USER_OPEN_MAX: int = Field(default=50, ge=1, le=5000)
    CANDLE_BACKFILL_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    # Screener bounds.
    SCREENER_MAX_UNIVERSE: int = Field(default=6000, ge=10, le=50_000)
    SCREENER_INLINE_MAX: int = Field(default=100, ge=0, le=1000)
    SCREENER_CHUNK: int = Field(default=200, ge=10, le=2000)
    SCREENER_LOOKBACK_BARS: int = Field(default=300, ge=50, le=2000)
    SCREENER_STALE_BARS: float = Field(default=4.0, gt=0)
    SCREENER_CACHE_SECONDS: float = Field(default=300.0, ge=0, le=86_400)
    SCREENER_USER_ACTIVE_MAX: int = Field(default=3, ge=1, le=100)
    SCREENER_RETENTION_DAYS: int = Field(default=14, ge=1, le=365)
    # Hard wall-clock budget for one screener run (inline or in the research worker).
    SCREENER_RUN_TIMEOUT_SECONDS: int = Field(default=300, ge=10, le=3600)
    # Timeframes the dhan-universe-backfill job keeps current (1d by default; intraday for a
    # whole exchange is one Dhan data call per instrument per refresh, so opt in knowingly).
    DHAN_UNIVERSE_BACKFILL_TIMEFRAMES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["1d"]
    )
    WATCHLIST_MAX_PER_USER: int = Field(default=50, ge=1, le=1000)
    WATCHLIST_MAX_ITEMS: int = Field(default=500, ge=1, le=5000)
    # The static IP you whitelisted on Dhan (only scripts/dhan_preflight.py reads it).
    DHAN_STATIC_IP: str | None = None
    # API key login (docs/11-dhan.md, "Log in with the API key"): the key + secret from
    # web.dhan.co -> Access DhanHQ APIs -> API key; the redirect URL registered there.
    DHAN_API_KEY: SecretStr | None = None
    DHAN_API_SECRET: SecretStr | None = None
    DHAN_REDIRECT_URL: str | None = None
    DHAN_AUTH_URL: str = "https://auth.dhan.co"
    # Written by the login flow only (core.app_settings, encrypted): the session's access
    # token and its expiry (UTC). Never set these in .env.
    DHAN_SESSION_TOKEN: SecretStr | None = None
    DHAN_SESSION_EXPIRES_AT: datetime | None = None
    # Discord alert when the Dhan session has expired or expires within this many minutes.
    DHAN_SESSION_ALERT_MINUTES: int = Field(default=60, ge=0, le=24 * 60)
    # Automatic login with PIN + TOTP (docs/11-dhan.md, "Automatic login"): the scheduler
    # mints a new session token itself when none is active or it expires within
    # DHAN_AUTO_LOGIN_BEFORE_MINUTES. Needs TOTP enabled on the Dhan account; the TOTP
    # secret is the base32 key shown when you set up the authenticator. Both are stored
    # encrypted (core.app_settings) and never logged or returned. Blank cron = off.
    DHAN_PIN: SecretStr | None = None
    DHAN_TOTP_SECRET: SecretStr | None = None
    DHAN_AUTO_LOGIN_CRON: str = "*/10 * * * *"
    DHAN_AUTO_LOGIN_BEFORE_MINUTES: int = Field(default=120, ge=10, le=20 * 60)
    # Credential verification against GET /profile (docs/28-dhan-operations.md). The result
    # is kept in dhan.connections; a Dhan outage never downgrades a verdict.
    DHAN_SESSION_CHECK_CRON: str = "*/15 * * * *"
    # Monitoring: auth, feed, Redis, subscriptions, staleness -> incidents (source "dhan").
    DHAN_MONITOR_CRON: str = "* * * * *"
    # NSE trading holidays (YYYY-MM-DD, comma-separated). On these days a silent feed is
    # "market_closed", not "stale". Weekends are always closed.
    DHAN_MARKET_HOLIDAYS: Annotated[list[date], NoDecode] = Field(default_factory=list)
    # The feed worker's status record (Redis) older than this = the worker is not reporting.
    DHAN_FEED_STATUS_MAX_AGE_SECONDS: float = Field(default=30.0, gt=0)
    # Crash-loop guard: this many starts within 10 minutes delays the next start
    # (exponentially, up to 5 min) instead of hammering Dhan.
    DHAN_FEED_CRASH_LOOP_STARTS: int = Field(default=5, ge=2, le=100)
    # Minimum gap between two feed reconnects requested by an admin or by recovery.
    DHAN_FEED_RECONNECT_COOLDOWN_SECONDS: float = Field(default=60.0, ge=5)

    def dhan_session_active(self, now: datetime | None = None) -> bool:
        """A login-flow token that has not expired yet."""
        exp = self.DHAN_SESSION_EXPIRES_AT
        if not _secret_text(self.DHAN_SESSION_TOKEN) or exp is None:
            return False
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return exp > (now or datetime.now(UTC))

    def dhan_access_token(self, now: datetime | None = None) -> SecretStr | None:
        """The token every Dhan call uses: the logged-in session's while it is valid, else the
        manual DHAN_ACCESS_TOKEN fallback (None = no usable token)."""
        if self.dhan_session_active(now):
            return SecretStr(_secret_text(self.DHAN_SESSION_TOKEN))
        manual = _secret_text(self.DHAN_ACCESS_TOKEN)
        if not manual:
            return None
        # A manual token whose own `exp` has passed is not usable: returning it would keep
        # every Dhan job running (and failing) against a dead token (docs/27 finding 4).
        exp = jwt_expiry(manual)
        if exp is not None and exp <= (now or datetime.now(UTC)):
            return None
        return SecretStr(manual)

    @property
    def dhan_configured(self) -> bool:
        return bool(self.DHAN_CLIENT_ID and self.dhan_access_token())

    @property
    def dhan_auto_login_configured(self) -> bool:
        """Client id, PIN and TOTP secret all set, and the schedule not blanked."""
        return bool(
            self.DHAN_CLIENT_ID
            and _secret_text(self.DHAN_PIN)
            and _secret_text(self.DHAN_TOTP_SECRET)
            and self.DHAN_AUTO_LOGIN_CRON.strip()
        )

    # --- MT5 bot (Trading Phase C, docs/12-mt5-adapter.md). Read-only status export only. ---
    # Either the bot's logs/status.json (bind-mounted read-only) or its GET /status endpoint.
    MT5_STATUS_FILE: str | None = None
    MT5_STATUS_URL: str | None = None
    MT5_STATUS_TOKEN: SecretStr | None = None
    MT5_HTTP_TIMEOUT: float = Field(default=5.0, gt=0)
    # The bot is marked stale when its export is older than this.
    MT5_STALE_SECONDS: float = Field(default=90.0, gt=0)
    # MT5 login + server of the account the bot trades (needed for positions + mt5.terminals).
    MT5_ACCOUNT_ID: str | None = None
    MT5_SERVER: str | None = None
    # Deposit currency of that account (the bot does not export it).
    MT5_ACCOUNT_CURRENCY: str = Field(default="USD", min_length=3, max_length=3)
    MT5_SYNC_CRON: str = "* * * * *"

    @property
    def mt5_configured(self) -> bool:
        return bool(self.MT5_STATUS_FILE or self.MT5_STATUS_URL)

    # --- MT5 push ingest (schema aureon.mt5.push/v1, docs/12-mt5-adapter.md §3) ---
    # Bearer token the bot sends to POST /api/trading/mt5/ingest. Unset = ingest off (503).
    # Prod refuses to start when it is set and shorter than 32 characters.
    MT5_INGEST_TOKEN: SecretStr | None = None
    MT5_INGEST_MAX_BYTES: int = Field(default=2 * 1024 * 1024, ge=1024)
    MT5_INGEST_MAX_DECOMPRESSED_BYTES: int = Field(default=8 * 1024 * 1024, ge=1024)
    # Pushes per minute accepted from the bot (it pushes every ~15 s and retries next poll).
    MT5_INGEST_RATE_PER_MINUTE: int = Field(default=30, ge=1, le=6000)
    # No push for longer than this = stale (status card, failures, agents.agents "mt5-bot").
    MT5_STALE_HEARTBEAT_SECONDS: float = Field(default=90.0, gt=0)
    # Market open but the newest tick older than this = a `stale_ticks` failure.
    MT5_STALE_TICK_SECONDS: float = Field(default=120.0, gt=0)
    # An exception / guardian_error failure with no new occurrence for this long is resolved.
    MT5_FAILURE_QUIET_SECONDS: float = Field(default=3600.0, gt=0)
    # Retention of pushed candles, and of events / push ids / resolved failures.
    MT5_CANDLE_RETENTION_DAYS: int = Field(default=7, ge=1, le=3650)
    MT5_EVENT_RETENTION_DAYS: int = Field(default=30, ge=1, le=3650)
    MT5_INGEST_WATCH_CRON: str = "* * * * *"
    MT5_INGEST_PURGE_CRON: str = "37 3 * * *"

    @property
    def mt5_ingest_enabled(self) -> bool:
        return bool(self.MT5_INGEST_TOKEN and self.MT5_INGEST_TOKEN.get_secret_value())

    # --- Aureon V1.6.3 database API (docs/34-aureon-v163.md) ---
    # One bearer token per role, least privilege; unset = that role is off. The Windows box
    # holds only COLLECTOR and/or GUARD; READ/WRITE are for reporting clients; ADMIN clears a
    # risk lock and starts a manual review (keep it offline). SVC is V1.6's legacy single MT5
    # token: it acts as collector + guard only while neither of those is set.
    # Prod refuses to start when a set token is short, a placeholder, or reused.
    AUREON_API_READ_TOKEN: SecretStr | None = None
    AUREON_API_WRITE_TOKEN: SecretStr | None = None
    AUREON_API_COLLECTOR_TOKEN: SecretStr | None = None
    AUREON_API_GUARD_TOKEN: SecretStr | None = None
    AUREON_API_SERVICE_TOKEN: SecretStr | None = None
    AUREON_API_ADMIN_TOKEN: SecretStr | None = None
    AUREON_API_SVC_TOKEN: SecretStr | None = None
    # Requests per minute per (role, client), as V1.6.3 (600).
    AUREON_API_RATE_PER_MINUTE: int = Field(default=600, ge=1, le=60_000)
    AUREON_API_MAX_BODY_BYTES: int = Field(default=1024 * 1024, ge=1024, le=8 * 1024 * 1024)
    # Background work (detector, scheduler, alerts, reviews) runs only when enabled.
    AUREON_WORKERS_ENABLED: bool = False
    AUREON_TICK_CRON: str = "* * * * *"
    AUREON_ALERTS_CRON: str = "* * * * *"
    # Advances any RUNNING review; starts last week's automatically only on a closed weekend.
    AUREON_WEEKEND_REVIEW_CRON: str = "*/5 * * * *"
    # The Sunday review waits for its verify job across runs (V1.6.3 ran it at 12:00).
    AUREON_WEEKLY_REVIEW_CRON: str = "*/10 12-13 * * 0"
    AUREON_VERIFY_CRON: str = "30 2 * * 2-6"
    # Optional: also write weekly report files here (they are always stored in PostgreSQL).
    AUREON_REVIEW_REPORTS_DIR: str = ""

    @property
    def aureon_api_enabled(self) -> bool:
        return any(
            t and t.get_secret_value()
            for t in (
                self.AUREON_API_READ_TOKEN,
                self.AUREON_API_WRITE_TOKEN,
                self.AUREON_API_COLLECTOR_TOKEN,
                self.AUREON_API_GUARD_TOKEN,
                self.AUREON_API_SERVICE_TOKEN,
                self.AUREON_API_ADMIN_TOKEN,
                self.AUREON_API_SVC_TOKEN,
            )
        )

    # --- research agents (Trading Phase D, docs/13-research-agents.md) ---
    RESEARCH_TICK_SECONDS: float = Field(default=60.0, ge=5)
    RESEARCH_QUEUE_MAX: int = Field(default=200, ge=1, le=10_000)
    # Candles read per (instrument, timeframe) for indicators and signals.
    RESEARCH_LOOKBACK_BARS: int = Field(default=500, ge=50, le=20_000)
    # Extra EMA periods always computed (strategies add their own).
    RESEARCH_EMA_PERIODS: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [8, 9, 20, 21, 32, 50, 200]
    )
    # Days of history the market-data agent asks the adapter for on an empty series.
    RESEARCH_BACKFILL_DAYS: int = Field(default=10, ge=1, le=365)
    # Data is stale when the newest candle is older than this many bars (in market hours).
    RESEARCH_STALE_BARS: float = Field(default=3.0, gt=0)
    # Risk assumptions (basis points per side) and freshness limits.
    RISK_SPREAD_BPS: Decimal = Field(default=Decimal("2"), ge=0)
    RISK_SLIPPAGE_BPS: Decimal = Field(default=Decimal("2"), ge=0)
    RISK_FEE_BPS: Decimal = Field(default=Decimal("3"), ge=0)
    RISK_MAX_COST_BPS: Decimal = Field(default=Decimal("25"), ge=0)
    RISK_ACCOUNT_STALE_SECONDS: float = Field(default=900.0, gt=0)
    RISK_SIGNAL_MAX_AGE_BARS: float = Field(default=2.0, gt=0)
    # Backtests
    BACKTEST_MAX_GRID: int = Field(default=50, ge=1, le=500)
    BACKTEST_MAX_BARS: int = Field(default=200_000, ge=100)
    # AI research agent (explanations only), through the Claude gateway (app/ai).
    RESEARCH_AI_QUEUE_MAX: int = Field(default=20, ge=1, le=1000)
    RESEARCH_AI_TIMEOUT_SECONDS: float = Field(default=600.0, gt=0)

    # --- coding agent (docs/05-agents.md, "Coding agent") ---
    # JSON: {"name": {"path": "/abs/checkout", "base": "dev01", "tests": [["npm","test"]],
    #                 "link": ["node_modules"]}}. Nothing outside these repos is readable.
    CODE_REPOS: Annotated[dict[str, Any], NoDecode] = Field(default_factory=dict)
    # Exact argv lists the agent may run (empty = the built-in safe list).
    CODE_ALLOWED_COMMANDS: Annotated[list[list[str]], NoDecode] = Field(default_factory=list)
    # Branches the agent may never write, merge into or approve onto.
    CODE_PROTECTED_BRANCHES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["main", "master", "dev01", "dev", "develop", "production"]
    )
    CODE_WORKTREE_ROOT: Path = Path("/mnt/spasta-data/code-worktrees")
    # Local Ollama coding model (e.g. qwen2.5-coder:7b); empty = tasks fail "not configured".
    CODE_MODEL: str | None = None
    CODE_MODEL_TIMEOUT_SECONDS: float = Field(default=900.0, gt=0)
    CODE_TEST_TIMEOUT_SECONDS: float = Field(default=900.0, gt=0)
    CODE_MAX_CONTEXT_BYTES: int = Field(default=120_000, ge=1000)
    CODE_MAX_FILE_BYTES: int = Field(default=400_000, ge=1000)
    CODE_MAX_PATCH_BYTES: int = Field(default=200_000, ge=1000)
    CODE_POLL_SECONDS: float = Field(default=3.0, gt=0)
    # --- coding agent git ops (clone / pull / push + PR; docs/05-agents.md "Git ops") ---
    # JSON list of GitHub "owner/repo" the coding worker may clone, pull and push feature
    # branches to. Empty = none. Each must ALSO pass the GitHub allowlist (D-15-3).
    GITHUB_CODING_REPO_ALLOWLIST: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Where clones live (inside CODE_WORKTREE_ROOT's volume): <root>/<owner>/<repo>.
    CODE_CLONE_ROOT: Path = Path("/mnt/spasta-data/code-worktrees/repos")
    # Pushes are refused unless the branch starts with one of these (comma list).
    CODE_PUSH_BRANCH_PREFIXES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["aureon/", "feat/"]
    )
    # The git host clones and pushes talk to (https only; the token goes via GIT_ASKPASS).
    GITHUB_GIT_URL: str = "https://github.com"
    CODE_GIT_TIMEOUT_SECONDS: float = Field(default=600.0, gt=0)

    # --- paper trading (Trading Phase F, docs/15-paper-trading.md) ---
    # Notional starting equity of every PAPER account, in its base currency.
    PAPER_STARTING_CAPITAL: Decimal = Field(default=Decimal("1000000"), gt=0)
    # A fill needs a reference price no older than this (Dhan tick, latest candle, MT5 bot).
    PAPER_PRICE_MAX_AGE_SECONDS: float = Field(default=180.0, gt=0)
    # Per broker: {"dhan": {"spread_bps": "2", "slippage_bps": "1"}, "mt5": {...}}.
    PAPER_COSTS: Annotated[dict[str, Any], NoDecode] = Field(
        default_factory=lambda: {
            "dhan": {"spread_bps": "2", "slippage_bps": "1"},
            "mt5": {"spread_bps": "1", "slippage_bps": "1"},
        }
    )
    # Overrides of the built-in Indian charge schedule (keys as in accounting.py).
    PAPER_DHAN_CHARGES: Annotated[dict[str, Any], NoDecode] = Field(default_factory=dict)
    PAPER_MT5_COMMISSION_PER_LOT: Decimal = Field(default=Decimal(0), ge=0)

    # --- real-time trading stream for the frontend (GET /api/trading/stream) ---
    TRADING_STREAM_POLL_SECONDS: float = Field(default=2.0, gt=0)
    TRADING_STREAM_HEARTBEAT_SECONDS: float = Field(default=15.0, gt=0)
    # The stream ends after this long; the browser's EventSource reconnects with its cursor.
    TRADING_STREAM_MAX_SECONDS: float = Field(default=1800.0, gt=0)

    # --- frontend support (docs/02-auth-and-finance.md, "Frontend support") ---
    # Receipt attachments, stored under DATA_ROOT/finance-attachments/<user>/<id>.
    FINANCE_ATTACHMENT_MAX_BYTES: int = Field(default=10 * 1024 * 1024, ge=1024)
    FINANCE_ATTACHMENTS_MAX_PER_USER: int = Field(default=2000, ge=1)
    # Delete tombstones (for GET /api/finance/changes) are pruned after this many days.
    FINANCE_TOMBSTONE_RETENTION_DAYS: int = Field(default=30, ge=1)
    # Max rows per kind returned by one GET /api/finance/changes call.
    FINANCE_CHANGES_MAX_ROWS: int = Field(default=5000, ge=10)
    # GET /api/public/* requests allowed per client IP per minute.
    PUBLIC_RATE_LIMIT_PER_MINUTE: int = Field(default=60, ge=1)
    # RSS/Atom feeds for the `news_pull` job (JSON list). Empty = the job is not scheduled.
    NEWS_FEED_URLS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    NEWS_PULL_CRON: str = "*/30 * * * *"
    NEWS_HTTP_TIMEOUT: float = Field(default=10.0, gt=0)
    NEWS_MAX_ITEMS: int = Field(default=200, ge=1, le=1000)
    NEWS_MAX_FEED_BYTES: int = Field(default=2 * 1024 * 1024, ge=1024)

    # --- GitHub (docs/18-github-foundation.md). The token comes from env only. ---
    # Fine-grained PAT. Never stored in the DB, never logged, never sent to the frontend.
    GITHUB_TOKEN: SecretStr | None = None
    GITHUB_API_URL: str = "https://api.github.com"
    # JSON list of owners (users/orgs) whose repos are visible. Empty = the token user's own.
    GITHUB_ALLOWED_OWNERS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Optional JSON list of "owner/name". When set, only these repos are visible.
    GITHUB_ALLOWED_REPOS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # In-memory cache of GitHub GET responses (branches, commits, tree, contents). 0 = off.
    GITHUB_CACHE_TTL_SECONDS: float = Field(default=60.0, ge=0)
    GITHUB_CONNECT_TIMEOUT: float = Field(default=5.0, gt=0)
    GITHUB_READ_TIMEOUT: float = Field(default=20.0, gt=0)
    GITHUB_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=3)
    GITHUB_RETRY_BACKOFF_SECONDS: float = Field(default=0.5, ge=0)
    # A 429 / secondary limit asking to wait longer than this is returned as 429, not waited.
    GITHUB_MAX_RETRY_WAIT_SECONDS: float = Field(default=20.0, ge=0)
    # Below this many remaining core requests the client refuses until the window resets.
    GITHUB_RATE_LIMIT_FLOOR: int = Field(default=50, ge=0)
    GITHUB_MAX_PAGES: int = Field(default=10, ge=1, le=100)
    # GET .../contents returns decoded text up to this size; larger or binary = metadata only.
    GITHUB_MAX_FILE_BYTES: int = Field(default=512 * 1024, ge=1024, le=1024 * 1024)
    GITHUB_MAX_TREE_ENTRIES: int = Field(default=5000, ge=10, le=100_000)
    GITHUB_SYNC_CRON: str = "*/20 * * * *"
    # The `github` agent is reported stale when no sync heartbeat arrived within this long.
    GITHUB_AGENT_STALE_SECONDS: float = Field(default=3600.0, gt=0)
    # GET .../pulls/{n}/diff returns at most this many bytes, with `truncated: true` beyond it
    # (docs/19-github-issues-prs.md).
    GITHUB_DIFF_MAX_BYTES: int = Field(default=2 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)

    # --- Actions + webhooks (docs/20-github-actions-webhooks.md). ---
    # Job / run logs are returned tail-first, at most this many bytes after sanitising.
    GITHUB_LOG_MAX_BYTES: int = Field(default=1024 * 1024, ge=1024, le=16 * 1024 * 1024)
    # At most this much is downloaded from the log blob (text job log or zipped run logs).
    GITHUB_LOG_DOWNLOAD_MAX_BYTES: int = Field(
        default=32 * 1024 * 1024, ge=1024, le=256 * 1024 * 1024
    )
    # Unzipped run logs: total size and entry count caps (zip-bomb guard).
    GITHUB_LOG_UNZIP_MAX_BYTES: int = Field(default=64 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)
    GITHUB_LOG_ZIP_MAX_ENTRIES: int = Field(default=500, ge=1, le=10_000)
    # Hosts (fnmatch patterns, https only) a log redirect may point at. Used for logs only.
    GITHUB_LOG_HOSTS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "pipelines.actions.githubusercontent.com",
            "*.actions.githubusercontent.com",
            "productionresultssa*.blob.core.windows.net",
        ]
    )
    # JSON list of "owner/repo:workflow_file" that may be dispatched / re-run. Empty = none.
    GITHUB_DISPATCH_ALLOWLIST: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Workflow names or file names that deploy to production: dispatch / re-run of these goes
    # through the ApprovalGate. Any name or file containing "deploy" or "prod" counts too.
    GITHUB_PROD_WORKFLOWS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # HMAC-SHA256 secret for X-Hub-Signature-256. Unset = the receiver answers 503.
    GITHUB_WEBHOOK_SECRET: SecretStr | None = None
    GITHUB_WEBHOOK_MAX_BYTES: int = Field(default=1024 * 1024, ge=1024, le=25 * 1024 * 1024)
    # The stored payload (github.webhook_events.payload) is trimmed to at most this size.
    GITHUB_WEBHOOK_STORE_MAX_BYTES: int = Field(default=256 * 1024, ge=1024, le=1024 * 1024)
    GITHUB_WEBHOOK_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=50)
    GITHUB_WEBHOOK_RETRY_BASE_SECONDS: float = Field(default=5.0, ge=0)
    GITHUB_WEBHOOK_POLL_SECONDS: float = Field(default=5.0, gt=0, le=300)
    GITHUB_ACTIONS_SYNC_CRON: str = "*/15 * * * *"
    GITHUB_ACTIONS_SYNC_RUNS: int = Field(default=30, ge=1, le=100)
    # Actions monitor (docs/20, "Actions monitor"): which repo + branch each host deploy
    # target (deploy_record --target) builds, as JSON {"target": "owner/repo@branch"}.
    DEPLOY_TARGET_REPOS: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=lambda: {
            "backend": "SVSKHD/Aureon_agents_Assembler@main",
            "frontend": "SVSKHD/ASTRA-FRONTEND@dev01",
        }
    )

    # --- DevOps monitoring (docs/32-devops-monitoring.md) ---
    # Environment name recorded for host deploys (deploy_record) unless --environment is given.
    DEVOPS_ENVIRONMENT: str = Field(default="production", min_length=1, max_length=100)
    # Recent GitHub activity re-read from the API after an outage (PRs and deployments per
    # repo); Actions runs are backfilled by github_actions_sync.
    DEVOPS_BACKFILL_CRON: str = "*/15 * * * *"
    DEVOPS_BACKFILL_PER_REPO: int = Field(default=30, ge=1, le=100)
    DEVOPS_BACKFILL_MAX_REPOS: int = Field(default=20, ge=1, le=200)
    # Feed events (build.*, pr.merged, ...) are only created for facts this recent, so a first
    # backfill does not flood the feed with history.
    DEVOPS_EVENT_MAX_AGE_HOURS: int = Field(default=48, ge=1, le=24 * 90)
    # Retention (the devops_purge job): feed events + repo events, collector snapshots,
    # deployment records.
    DEVOPS_PURGE_CRON: str = "41 3 * * *"
    DEVOPS_RETENTION_DAYS: int = Field(default=90, ge=1, le=3650)
    DEVOPS_SNAPSHOT_RETENTION_DAYS: int = Field(default=7, ge=1, le=365)
    DEVOPS_DEPLOYMENT_RETENTION_DAYS: int = Field(default=365, ge=1, le=3650)
    # A service the host collector has not reported for this long reads as UNKNOWN.
    DEVOPS_RUNTIME_STALE_SECONDS: float = Field(default=180.0, ge=30)
    # Compose services that are expected to exit (exit 0 = healthy).
    DEVOPS_ONESHOT_SERVICES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["mount-check", "migrate"]
    )
    # A running deploy whose host has not reported progress for this long is shown as stale
    # (docs/33-deploy-progress.md); the poller reports every few seconds and at least once a
    # minute while a deploy runs.
    DEVOPS_DEPLOY_STALE_SECONDS: float = Field(default=180.0, ge=30, le=24 * 3600)
    # Live feed (GET /api/v1/devops/stream).
    DEVOPS_STREAM_POLL_SECONDS: float = Field(default=2.0, gt=0, le=60)
    DEVOPS_STREAM_HEARTBEAT_SECONDS: float = Field(default=15.0, gt=0, le=300)
    DEVOPS_STREAM_MAX_SECONDS: float = Field(default=3600.0, ge=10, le=24 * 3600)

    # --- Approval gate (docs/19-github-issues-prs.md; grown by the Phase 20 engine). ---
    # A pending approval request expires this many minutes after it was created.
    APPROVALS_TTL_MINUTES: int = Field(default=30, ge=1, le=24 * 60)
    # The requester may approve their own request only when this is true AND they are the only
    # active admin (single-owner deployments). False: someone else must always approve.
    APPROVALS_ALLOW_SELF_APPROVAL: bool = True
    # After this many wrong re-auth passwords on one request it is denied automatically.
    APPROVALS_MAX_REAUTH_FAILURES: int = Field(default=5, ge=1, le=50)
    # --- Approval engine (docs/23-approval-engine.md). Policies live in audit.approval_policies.
    # Upper bound for a session re-auth stamp (POST /api/v1/approvals/reauth); the stamp's TTL
    # is the largest enabled policy's reauth_max_age_seconds, capped by this.
    APPROVALS_REAUTH_STAMP_MAX_SECONDS: int = Field(default=900, ge=0, le=3600)
    # Characters of an artifact's canonical text stored in audit.approval_artifacts.content.
    APPROVALS_ARTIFACT_MAX_CHARS: int = Field(default=65536, ge=0, le=1024 * 1024)
    # Scheduler sweep that marks due pending requests expired (task approval_expire).
    APPROVALS_EXPIRE_CRON: str = "* * * * *"
    APPROVALS_STREAM_POLL_SECONDS: float = Field(default=2.0, gt=0)
    APPROVALS_STREAM_HEARTBEAT_SECONDS: float = Field(default=15.0, gt=0)
    APPROVALS_STREAM_MAX_SECONDS: float = Field(default=1800.0, gt=0)
    # Base URL of the web app, used for deep links in notifications.
    PUBLIC_APP_URL: str = "https://spasta.online"
    # Discord incoming webhook (the URL is a secret). Unset = notifications disabled.
    DISCORD_WEBHOOK_URL: SecretStr | None = None
    DISCORD_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    DISCORD_REQUEST_SECONDS: float = Field(default=5.0, gt=0, le=60)
    DISCORD_RETRY_BASE_SECONDS: float = Field(default=1.0, ge=0, le=60)

    # --- Incidents + self-repair (docs/21-self-repair.md) ---
    # Model id recorded on repair tasks. Repairs have no model client since the AI stack is
    # Claude-only (docs/26): they stay queued with the reason until a Claude path is added.
    REPAIR_MODEL: str | None = None
    # The CODE_REPOS entry repairs work in, and its GitHub "owner/repo" (must also be in the
    # GitHub allowlist). Either unset = repairs disabled.
    REPAIR_REPO: str | None = None
    REPAIR_GITHUB_REPO: str | None = None
    REPAIR_MODEL_TIMEOUT_SECONDS: float = Field(default=900.0, gt=0)
    REPAIR_POLL_SECONDS: float = Field(default=10.0, gt=0)
    # Queued + running repairs at most; more are refused.
    REPAIR_QUEUE_MAX: int = Field(default=5, ge=1, le=100)
    # Repairs created per incident fingerprint per rolling day.
    REPAIR_MAX_PER_FINGERPRINT_PER_DAY: int = Field(default=2, ge=1, le=50)
    # Circuit breaker: more than this many failed repairs in an hour pauses the repair agent.
    REPAIR_BREAKER_MAX_FAILURES_PER_HOUR: int = Field(default=3, ge=1, le=100)
    REPAIR_TEST_OUTPUT_MAX_CHARS: int = Field(default=32_000, ge=1000, le=500_000)
    # Automatic recovery attempts per incident before it escalates, and the gap between them.
    INCIDENT_MAX_RECOVERY_ATTEMPTS: int = Field(default=2, ge=1, le=10)
    INCIDENT_RECOVERY_COOLDOWN_SECONDS: float = Field(default=120.0, ge=0)
    # An unhandled exception with the same fingerprint is written at most this often.
    INCIDENT_EXCEPTION_RATE_SECONDS: float = Field(default=60.0, ge=0)
    INCIDENT_SCAN_CRON: str = "* * * * *"
    # SELECT 1 slower than this is an incident.
    INCIDENT_DB_SLOW_MS: float = Field(default=1000.0, gt=0)
    # The API's readiness URL as the scheduler reaches it. Empty = not probed.
    INCIDENT_API_READY_URL: str = "http://aureon-api:8000/api/health/ready"
    # The API probes Docker (via the read-only proxy) this often; 0 = off.
    INCIDENT_DOCKER_PROBE_SECONDS: float = Field(default=60.0, ge=0)
    # Containers the repair system may restart, ONLY if the docker-proxy allows
    # POST /containers/{name}/restart (the shipped proxy does not: restarts are "manual").
    INCIDENT_DOCKER_PROXY_ALLOWS_RESTART: bool = False
    INCIDENT_RESTART_CONTAINERS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # --- Claude review (docs/22-claude-review.md). A non-Ollama AI path (the other is OpenRouter,
    # docs/25): the local Claude CLI, only when enabled, available, and the diff passes the
    # classification filter.
    CLAUDE_REVIEW_ENABLED: bool = False
    # The CLI binary: a name looked up on PATH, or an absolute path.
    CLAUDE_CLI_PATH: str = "claude"
    # JSON argv after the binary: non-interactive print mode, JSON output, no tools, no MCP,
    # no saved session. The prompt always goes on stdin, never in argv.
    CLAUDE_CLI_ARGS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--tools",
            "",
        ]
    )
    # Env var NAMES (comma list) copied into the CLI's environment for its own auth. Repo
    # secrets (GITHUB_*, DATABASE_URL, SECRET_KEY, ...) are refused even if listed.
    CLAUDE_CLI_ENV_PASSTHROUGH: Annotated[list[str], NoDecode] = Field(default_factory=list)
    CLAUDE_REVIEW_TIMEOUT_SECONDS: int = Field(default=300, ge=10, le=3600)
    # A diff larger than this is never sent: the review is REVIEW_UNAVAILABLE (too_large).
    CLAUDE_REVIEW_MAX_DIFF_BYTES: int = Field(default=200 * 1024, ge=1024, le=5 * 1024 * 1024)
    # Reviews run one at a time; at most this many may wait (more answers 429).
    CLAUDE_REVIEW_MAX_QUEUE: int = Field(default=5, ge=1, le=100)
    # Internal domains (comma list) masked as [internal-host] before anything is sent.
    CLAUDE_REVIEW_INTERNAL_DOMAINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["spasta.online"]
    )

    # --- Claude, the only AI provider (docs/26-ai-providers.md, app/ai/gateway.py) ---
    # Server-side only: never logged, never returned. Unset = every AI feature is off.
    ANTHROPIC_API_KEY: SecretStr | None = None
    AI_MODEL_MAIN: str = "claude-opus-5-5"
    # Picks the agent when a request names none, and voice navigation. Empty = off.
    AI_MODEL_ROUTER: str = "claude-haiku-5-5"
    # Thinking depth on every call (Opus 5.5 always thinks; effort is the control).
    AI_EFFORT: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    AI_MAX_OUTPUT_TOKENS: int = Field(default=16000, ge=256, le=128000)
    AI_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0, le=1800)
    # SDK retries with exponential backoff on connection errors, 408/409/429 and 5xx.
    AI_MAX_RETRIES: int = Field(default=3, ge=0, le=8)
    # Month-to-date caps (Asia/Kolkata calendar month). A call that could exceed one is refused.
    AI_MONTHLY_BUDGET_USD: Decimal = Field(default=Decimal("100"), ge=0, le=Decimal("100000"))
    AI_AGENT_BUDGETS_USD: Annotated[dict[str, Decimal], NoDecode] = Field(
        default_factory=lambda: dict(AI_AGENT_BUDGET_DEFAULTS)
    )
    # {"<model>": {"input": usd, "output": usd, "cache_read": usd}} per million tokens.
    AI_MODEL_PRICES: Annotated[dict[str, dict[str, Decimal]], NoDecode] = Field(
        default_factory=dict
    )
    AI_MAX_STEPS: int = Field(default=8, ge=1, le=20)
    # One tool result is cut to about this many tokens (4 characters each) before Claude sees it.
    AI_TOOL_RESULT_MAX_TOKENS: int = Field(default=2000, ge=100, le=20000)
    AI_CHAT_REQUESTS_PER_MINUTE: int = Field(default=10, ge=1, le=600)
    AI_CONVERSATION_MAX_MESSAGES: int = Field(default=200, ge=10, le=5000)
    # Hard limits on propose_trade, checked in Python before an approval row exists (INR).
    AI_TRADE_MAX_LOSS_INR: Decimal = Field(default=Decimal("2000"), gt=0)
    AI_TRADE_MAX_ORDER_INR: Decimal = Field(default=Decimal("100000"), gt=0)
    AI_TRADE_MAX_PER_DAY: int = Field(default=3, ge=0, le=100)
    AI_APPROVAL_TTL_HOURS: float = Field(default=24.0, gt=0, le=24 * 30)
    # Scheduled reports (scheduler timezone, Asia/Kolkata). Empty = off.
    AI_DAILY_REPORT_CRON: str = "30 8 * * *"
    AI_WEEKLY_REVIEW_CRON: str = "0 10 * * 0"
    AGENT_IMAGE_ENABLED: bool = True

    # --- Production integration (docs/24-production-integration.md) ---
    # Alerts: open incidents (severity >= warning), stale heartbeats, failed/old backups and
    # approvals about to expire, sent to DISCORD_WEBHOOK_URL. Empty cron = job not created.
    ALERTS_CRON: str = "*/5 * * * *"
    # One alert per fingerprint per this many minutes.
    ALERTS_DEDUP_MINUTES: int = Field(default=60, ge=1, le=7 * 24 * 60)
    ALERTS_MAX_PER_RUN: int = Field(default=10, ge=1, le=100)
    # Pending approvals expiring within this many minutes are announced.
    ALERTS_APPROVAL_EXPIRY_MINUTES: int = Field(default=10, ge=1, le=24 * 60)
    # Status file written by scripts/ops/backup.sh (read-only mount in the scheduler).
    BACKUP_STATUS_FILE: Path = Path("/mnt/spasta-data/ops-status/backup.json")
    # The last successful backup may be at most this old (nightly timer + slack).
    BACKUP_MAX_AGE_HOURS: float = Field(default=26.0, gt=0)
    # Cloudflare Access JWT verification in the app (off unless both are set). AUD is a comma
    # list (one tag per Access application: spasta.online and api.spasta.online).
    CF_ACCESS_TEAM_DOMAIN: str | None = None
    CF_ACCESS_AUD: Annotated[list[str], NoDecode] = Field(default_factory=list)
    CF_ACCESS_CERTS_TTL_SECONDS: float = Field(default=3600.0, ge=60)
    CF_ACCESS_LEEWAY_SECONDS: float = Field(default=30.0, ge=0, le=300)
    # Path prefixes that skip the check (Access bypass policies): the GitHub webhook receiver
    # (HMAC-verified) and the public, rate-limited endpoints. /api/health is on-box only.
    CF_ACCESS_BYPASS_PATHS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["/api/v1/github/webhooks", "/api/public/", "/api/health"]
    )

    @property
    def cf_access_enabled(self) -> bool:
        return bool((self.CF_ACCESS_TEAM_DOMAIN or "").strip() and self.CF_ACCESS_AUD)

    @property
    def github_configured(self) -> bool:
        return bool(self.GITHUB_TOKEN and self.GITHUB_TOKEN.get_secret_value().strip())

    @property
    def discord_webhook_url(self) -> str | None:
        s = self.DISCORD_WEBHOOK_URL
        url = s.get_secret_value().strip() if s else ""
        return url or None

    @property
    def anthropic_key(self) -> str:
        return _secret_text(self.ANTHROPIC_API_KEY)

    def claude_price(self, model: str) -> tuple[Decimal, Decimal, Decimal] | None:
        """(input, output, cache read) USD per million tokens, or None (never called)."""
        own = self.AI_MODEL_PRICES.get(model)
        if own is not None:
            return own["input"], own["output"], own.get("cache_read", own["input"] / 10)
        return CLAUDE_PRICES.get(model)

    @field_validator("AI_MODEL_PRICES", mode="before")
    @classmethod
    def _prices(cls, v: object) -> object:
        """`{"model": {"input": usd, "output": usd, "cache_read"?: usd}}`, JSON or a dict."""
        if v is None or v == "":
            return {}
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError as exc:
                raise ValueError("must be a JSON object") from exc
        if not isinstance(v, dict):
            raise ValueError("must be a JSON object of model -> {input, output}")
        out: dict[str, dict[str, Decimal]] = {}
        for model, price in v.items():
            if not isinstance(model, str) or not model.strip() or not isinstance(price, dict):
                raise ValueError("each entry must be model -> {input, output}")
            if "input" not in price or "output" not in price:
                raise ValueError("each price needs numeric input and output")
            try:
                keys = [k for k in ("input", "output", "cache_read") if k in price]
                pair = {k: Decimal(str(price[k])) for k in keys}
            except (ArithmeticError, ValueError) as exc:
                raise ValueError("each price needs numeric input and output") from exc
            if any(not d.is_finite() or d < 0 for d in pair.values()):
                raise ValueError("prices must be numbers >= 0")
            out[model.strip()] = pair
        return out

    @field_validator("AI_AGENT_BUDGETS_USD", mode="before")
    @classmethod
    def _agent_budgets(cls, v: object) -> object:
        """`{"investments": 40, ...}`; agents left out keep their default cap."""
        if v is None or v == "":
            return dict(AI_AGENT_BUDGET_DEFAULTS)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError as exc:
                raise ValueError("must be a JSON object") from exc
        if not isinstance(v, dict):
            raise ValueError("must be a JSON object of agent -> USD")
        out = dict(AI_AGENT_BUDGET_DEFAULTS)
        for k, amount in v.items():
            if k not in AI_AGENT_BUDGET_DEFAULTS:
                raise ValueError(f"unknown agent {k!r} ({', '.join(AI_AGENT_BUDGET_DEFAULTS)})")
            try:
                d = Decimal(str(amount))
            except ArithmeticError as exc:
                raise ValueError("budgets must be numbers") from exc
            if not d.is_finite() or d < 0:
                raise ValueError("budgets must be numbers >= 0")
            out[k] = d
        return out

    @property
    def github_webhook_configured(self) -> bool:
        s = self.GITHUB_WEBHOOK_SECRET
        return bool(s and s.get_secret_value().strip())

    @field_validator(
        "CODE_REPOS",
        "CODE_ALLOWED_COMMANDS",
        "PAPER_COSTS",
        "PAPER_DHAN_CHARGES",
        "NEWS_FEED_URLS",
        "GITHUB_ALLOWED_OWNERS",
        "GITHUB_ALLOWED_REPOS",
        "GITHUB_LOG_HOSTS",
        "GITHUB_DISPATCH_ALLOWLIST",
        "GITHUB_PROD_WORKFLOWS",
        "GITHUB_CODING_REPO_ALLOWLIST",
        "DEPLOY_TARGET_REPOS",
        "CLAUDE_CLI_ARGS",
        mode="before",
    )
    @classmethod
    def _parse_json(cls, v: object, info: ValidationInfo) -> object:
        if isinstance(v, str):
            if v.strip():
                return json.loads(v)
            lists = (
                "CODE_ALLOWED_COMMANDS",
                "NEWS_FEED_URLS",
                "GITHUB_ALLOWED_OWNERS",
                "GITHUB_ALLOWED_REPOS",
                "GITHUB_LOG_HOSTS",
                "GITHUB_DISPATCH_ALLOWLIST",
                "GITHUB_PROD_WORKFLOWS",
                "GITHUB_CODING_REPO_ALLOWLIST",
                "CLAUDE_CLI_ARGS",
            )
            return [] if info.field_name in lists else {}
        return v

    @field_validator("IMAGE_AGENT_COMMAND", mode="before")
    @classmethod
    def _parse_command(cls, v: object) -> object:
        if isinstance(v, str):
            return json.loads(v) if v.strip() else []
        return v

    @field_validator("IMAGE_MODELS", mode="before")
    @classmethod
    def _parse_image_models(cls, v: object) -> object:
        if isinstance(v, str):
            return json.loads(v) if v.strip() else {}
        return v

    @field_validator("IMAGE_MODELS")
    @classmethod
    def _check_image_models(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        for name, spec in v.items():
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,60}", name):
                raise ValueError(f"IMAGE_MODELS: bad model name {name!r}")
            path = str(spec.get("file", ""))
            if not path.startswith("/") or ".." in path:
                raise ValueError(f"IMAGE_MODELS[{name}].file must be an absolute path")
            if int(spec.get("steps", 20)) < 1 or float(spec.get("cfg", 7.0)) <= 0:
                raise ValueError(f"IMAGE_MODELS[{name}]: steps >= 1 and cfg > 0")
            if not re.fullmatch(r"[a-z0-9_+]{1,30}", str(spec.get("sampler", "euler_a"))):
                raise ValueError(f"IMAGE_MODELS[{name}]: bad sampler")
        return v

    @field_validator("DHAN_PIN")
    @classmethod
    def _check_dhan_pin(cls, v: SecretStr | None) -> SecretStr | None:
        text = _secret_text(v).strip()
        if not text:
            return None
        if not re.fullmatch(r"\d{4,8}", text):
            raise ValueError("DHAN_PIN must be your Dhan PIN: digits only")  # never echo it
        return SecretStr(text)

    @field_validator("DHAN_TOTP_SECRET")
    @classmethod
    def _check_totp_secret(cls, v: SecretStr | None) -> SecretStr | None:
        text = re.sub(r"[\s-]", "", _secret_text(v)).upper()
        if not text:
            return None
        if not re.fullmatch(r"[A-Z2-7]{16,128}=*", text):
            raise ValueError(
                "DHAN_TOTP_SECRET must be the base32 key from Dhan's authenticator setup"
            )
        return SecretStr(text.rstrip("="))

    @field_validator("DHAN_INSTRUMENT_SEGMENTS")
    @classmethod
    def _check_segments(cls, v: list[str]) -> list[str]:
        known = {
            "IDX_I", "NSE_EQ", "NSE_FNO", "NSE_CURRENCY", "BSE_EQ", "MCX_COMM",
            "BSE_CURRENCY", "BSE_FNO",
        }  # fmt: skip
        out = [s.strip().upper() for s in v if s.strip()]
        bad = [s for s in out if s not in known]
        if bad:
            raise ValueError(f"DHAN_INSTRUMENT_SEGMENTS: unknown segment(s) {bad}")
        return list(dict.fromkeys(out))

    @field_validator("DHAN_UNIVERSE_BACKFILL")
    @classmethod
    def _check_universes(cls, v: list[str]) -> list[str]:
        known = {"nse_equities", "bse_equities", "indices", "fno_stocks", "watchlists"}
        out = [s.strip().lower() for s in v if s.strip()]
        bad = [s for s in out if s not in known]
        if bad:
            raise ValueError(f"DHAN_UNIVERSE_BACKFILL: unknown universe(s) {bad}")
        return list(dict.fromkeys(out))

    @field_validator("DHAN_UNIVERSE_BACKFILL_TIMEFRAMES")
    @classmethod
    def _check_universe_timeframes(cls, v: list[str]) -> list[str]:
        ok = {"1m", "5m", "15m", "1h", "1d"}  # what Dhan's chart APIs serve
        out = [s.strip().lower() for s in v if s.strip()]
        bad = [s for s in out if s not in ok]
        if bad:
            raise ValueError(f"DHAN_UNIVERSE_BACKFILL_TIMEFRAMES: Dhan has no {bad} candles")
        return list(dict.fromkeys(out))

    @field_validator("IMAGE_PAUSE_WINDOWS")
    @classmethod
    def _check_windows(cls, v: list[str]) -> list[str]:
        for w in v:
            if not re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", w):
                raise ValueError(f"IMAGE_PAUSE_WINDOWS: {w!r} is not HH:MM-HH:MM")
        return v

    @field_validator(
        "CORS_ORIGINS",
        "IMAGE_AGENT_ENV_PASSTHROUGH",
        "IMAGE_ALLOWED_SIZES",
        "IMAGE_PAUSE_WINDOWS",
        "DHAN_FEED_INSTRUMENTS",
        "DHAN_BACKFILL_INSTRUMENTS",
        "DHAN_BACKFILL_TIMEFRAMES",
        "DHAN_MARKET_HOLIDAYS",
        "DHAN_INSTRUMENT_SEGMENTS",
        "DHAN_UNIVERSE_BACKFILL",
        "DHAN_UNIVERSE_BACKFILL_TIMEFRAMES",
        "RESEARCH_EMA_PERIODS",
        "CODE_PROTECTED_BRANCHES",
        "CODE_PUSH_BRANCH_PREFIXES",
        "INCIDENT_RESTART_CONTAINERS",
        "CLAUDE_CLI_ENV_PASSTHROUGH",
        "CLAUDE_REVIEW_INTERNAL_DOMAINS",
        "CF_ACCESS_AUD",
        "CF_ACCESS_BYPASS_PATHS",
        "DEVOPS_ONESHOT_SERVICES",
        mode="before",
    )
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("SECRET_KEY")
    @classmethod
    def _secret_len(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return v

    @model_validator(mode="after")
    def _live_needs_phase_g(self) -> Settings:
        if self.TRADING_LIVE_ENABLED and self.PHASE_G_APPROVED != "yes":
            raise ValueError(
                "TRADING_LIVE_ENABLED=true requires PHASE_G_APPROVED=yes (Phase G sign-off)"
            )
        return self

    @property
    def docs_enabled(self) -> bool:
        if self.API_DOCS_ENABLED is None:
            return self.ENV is not Environment.PROD
        return self.API_DOCS_ENABLED

    @property
    def data_marker_path(self) -> Path:
        return self.DATA_ROOT / self.DATA_ROOT_MARKER

    @property
    def session_cookie_name(self) -> str:
        return "__Host-aureon_session" if self.COOKIE_SECURE else "aureon_session"

    @property
    def csrf_cookie_name(self) -> str:
        return "__Host-aureon_csrf" if self.COOKIE_SECURE else "aureon_csrf"


@lru_cache
def get_env_settings() -> Settings:
    """The settings from the environment / .env only, ignoring core.app_settings."""
    return Settings()


_effective: Settings | None = None


def get_settings() -> Settings:
    """The process's current settings: .env overlaid with core.app_settings once the overlay
    has been loaded (app/services/app_settings.py), else .env alone."""
    return _effective if _effective is not None else get_env_settings()


def set_effective_settings(settings: Settings | None) -> None:
    """Install the overlaid settings for get_settings() (None = back to .env only)."""
    global _effective
    _effective = settings
