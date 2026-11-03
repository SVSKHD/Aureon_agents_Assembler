"""Application settings, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import json
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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
    # http://<ollama-container>:11434 over the private Docker network
    OLLAMA_BASE_URL: str = "http://ollama:11434"
    IMAGE_AGENT_PATH: Path = Path("/opt/image-agent")
    DATA_ROOT: Path = Path("/mnt/spasta-data")
    # File that must exist on the HDD; proves the 2 TB disk is mounted, not an empty mountpoint.
    DATA_ROOT_MARKER: str = ".spasta-data-mounted"

    SECRET_KEY: SecretStr
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

    # --- ollama / chat ---
    # Simultaneous generations this API sends to Ollama (CPU-only box: keep at 1).
    OLLAMA_MAX_CONCURRENT: int = Field(default=1, ge=1)
    # Requests allowed to wait for a slot; beyond this, POST /api/chat returns 429.
    OLLAMA_MAX_QUEUE: int = Field(default=3, ge=0)
    OLLAMA_QUEUE_RETRY_AFTER: int = Field(default=15, ge=1)
    OLLAMA_CONNECT_TIMEOUT: float = Field(default=5.0, gt=0)
    # Max silence between streamed chunks; covers a cold model load on CPU.
    OLLAMA_READ_TIMEOUT: float = Field(default=300.0, gt=0)
    OLLAMA_MODEL_CACHE_SECONDS: float = Field(default=60.0, ge=0)
    # Whole chat request budget, including time spent queued.
    CHAT_TIMEOUT_SECONDS: float = Field(default=900.0, gt=0)
    # SSE keep-alive comment interval (Cloudflare drops idle streams after ~100 s).
    CHAT_HEARTBEAT_SECONDS: float = Field(default=15.0, gt=0)
    CHAT_DEFAULT_NUM_CTX: int = Field(default=4096, ge=256)
    CHAT_MAX_NUM_CTX: int = Field(default=8192, ge=256)
    # Model the SPA preselects (listed first in GET /api/models, `default_model`) and the API
    # loads at startup (CHAT_WARMUP). Empty = none: the first installed model.
    CHAT_DEFAULT_MODEL: str = ""
    CHAT_WARMUP: bool = True
    # CPU threads per generation (Ollama `num_thread`); 0 = Ollama decides.
    CHAT_NUM_THREAD: int = Field(default=0, ge=0, le=256)
    # Stored turns re-sent per request: system messages plus the newest N others (the new
    # turns are always sent). 0 = the whole conversation. Less history = less prompt to
    # process on a CPU before the first token.
    CHAT_HISTORY_MAX_MESSAGES: int = Field(default=0, ge=0)
    # Ollama `keep_alive` for API chats ("24h", "30m", "-1" = forever); empty = Ollama default.
    OLLAMA_KEEP_ALIVE: str = Field(default="", pattern=r"^(|-1|0|[0-9]{1,6}[smh]?)$")

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
    # The static IP you whitelisted on Dhan (only scripts/dhan_preflight.py reads it).
    DHAN_STATIC_IP: str | None = None

    @property
    def dhan_configured(self) -> bool:
        return bool(self.DHAN_CLIENT_ID and self.DHAN_ACCESS_TOKEN)

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

    # --- research agents (Trading Phase D, docs/13-research-agents.md) ---
    RESEARCH_TICK_SECONDS: float = Field(default=60.0, ge=5)
    RESEARCH_QUEUE_MAX: int = Field(default=200, ge=1, le=10_000)
    # Candles read per (instrument, timeframe) for indicators and signals.
    RESEARCH_LOOKBACK_BARS: int = Field(default=500, ge=50, le=20_000)
    # Extra EMA periods always computed (strategies add their own).
    RESEARCH_EMA_PERIODS: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [9, 21])
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
    # AI research agent: Ollama only, explanation only.
    RESEARCH_AI_MODEL: str | None = None
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
    # Voice navigation (POST /api/v1/voice/intent): an optional local Ollama model used only
    # when the deterministic grammar finds no intent. Unset = grammar only.
    VOICE_OLLAMA_MODEL: str | None = None

    # --- Incidents + self-repair (docs/21-self-repair.md) ---
    # Local Ollama model for repair patches. Unset / empty = repairs disabled (tasks stay
    # queued with the reason). .env.example suggests a Qwen coder model.
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
    # --- Claude review (docs/22-claude-review.md). The one non-Ollama AI path: the local
    # Claude CLI, only when enabled, available, and the diff passes the classification filter.
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

    @field_validator(
        "CORS_ORIGINS",
        "IMAGE_AGENT_ENV_PASSTHROUGH",
        "IMAGE_ALLOWED_SIZES",
        "DHAN_FEED_INSTRUMENTS",
        "DHAN_BACKFILL_INSTRUMENTS",
        "DHAN_BACKFILL_TIMEFRAMES",
        "RESEARCH_EMA_PERIODS",
        "CODE_PROTECTED_BRANCHES",
        "CODE_PUSH_BRANCH_PREFIXES",
        "INCIDENT_RESTART_CONTAINERS",
        "CLAUDE_CLI_ENV_PASSTHROUGH",
        "CLAUDE_REVIEW_INTERNAL_DOMAINS",
        "CF_ACCESS_AUD",
        "CF_ACCESS_BYPASS_PATHS",
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
def get_settings() -> Settings:
    return Settings()
