"""ENV=prod startup checklist. The API refuses to start while any item fails.

Checked in the API lifespan (docs/16-production-hardening.md, "Production checklist"):

1. SECRET_KEY is not the .env.example placeholder, is >= 32 chars and is not low-entropy.
2. DEBUG is off and the API docs (Swagger + /api/openapi.json) are off.
3. Cookies are Secure (COOKIE_SECURE=true, so they get the __Host- prefix).
4. CORS has no wildcard origin.
5. The 2 TB HDD is mounted (DATA_ROOT marker present and writable).
6. TRADING_LIVE_ENABLED is false unless PHASE_G_APPROVED=yes (also enforced by Settings).
7. MT5_INGEST_TOKEN, when set, is >= 32 chars, not a placeholder, and not the same value as
   another credential (MT5_STATUS_TOKEN, SECRET_KEY, GITHUB_WEBHOOK_SECRET, ...): one shared
   value would let whoever holds the other credential push as the bot, and vice versa.
"""

from __future__ import annotations

from pydantic import SecretStr

from app.core.config import Environment, Settings
from app.services.health import check_data_root

# Substrings that mark a SECRET_KEY copied from docs or examples.
PLACEHOLDER_MARKERS = ("change-me", "changeme", "placeholder", "example", "your-secret", "xxxx")
MIN_SECRET_LEN = 32
# token_urlsafe(48) has ~40 distinct characters; "k" * 48 has one.
MIN_DISTINCT_CHARS = 12


# Credentials the MT5 ingest token must never equal. MT5_STATUS_TOKEN matters most: the API
# SENDS it to the bot's /status endpoint, so a shared value would hand the inbound ingest secret
# to anyone who can read that outbound request.
INGEST_TOKEN_DISTINCT_FROM = (
    "MT5_STATUS_TOKEN",
    "SECRET_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_TOKEN",
    "SETTINGS_MASTER_KEY",
)


def ingest_token_strength_problem(token: str) -> str | None:
    if len(token) < MIN_SECRET_LEN:
        return f"MT5_INGEST_TOKEN is shorter than {MIN_SECRET_LEN} characters"
    if any(m in token.lower() for m in PLACEHOLDER_MARKERS):
        return "MT5_INGEST_TOKEN is a placeholder"
    return None


def ingest_token_reuse_problem(settings: Settings) -> str | None:
    """MT5_INGEST_TOKEN shares its value with another credential (names it, never the value)."""
    tok = settings.MT5_INGEST_TOKEN.get_secret_value() if settings.MT5_INGEST_TOKEN else ""
    if not tok:
        return None
    for name in INGEST_TOKEN_DISTINCT_FROM:
        other = getattr(settings, name, None)
        raw = other.get_secret_value() if isinstance(other, SecretStr) else other
        if not isinstance(raw, str) or not raw:
            continue
        # SETTINGS_MASTER_KEY may be a comma-separated rotation list.
        values = raw.split(",") if name == "SETTINGS_MASTER_KEY" else [raw]
        if any(v.strip() == tok for v in values):
            return f"MT5_INGEST_TOKEN must not reuse the value of {name}"
    return None


def ingest_token_problems(settings: Settings) -> list[str]:
    tok = settings.MT5_INGEST_TOKEN.get_secret_value() if settings.MT5_INGEST_TOKEN else ""
    if not tok:
        return []
    return [
        p for p in (ingest_token_strength_problem(tok), ingest_token_reuse_problem(settings)) if p
    ]


class ProductionConfigError(RuntimeError):
    """Raised at startup when ENV=prod and the configuration is unsafe."""


def secret_key_problem(secret: str) -> str | None:
    low = secret.lower()
    if len(secret) < MIN_SECRET_LEN:
        return f"SECRET_KEY is shorter than {MIN_SECRET_LEN} characters"
    if any(m in low for m in PLACEHOLDER_MARKERS):
        return "SECRET_KEY is the placeholder from .env.example"
    if len(set(secret)) < MIN_DISTINCT_CHARS:
        return "SECRET_KEY looks low-entropy (generate one with secrets.token_urlsafe(48))"
    return None


def production_problems(settings: Settings) -> list[str]:
    """Every failing checklist item, as a human-readable line (never includes a secret)."""
    problems: list[str] = []
    if msg := secret_key_problem(settings.SECRET_KEY.get_secret_value()):
        problems.append(msg)
    if settings.DEBUG:
        problems.append("DEBUG must be false")
    if settings.docs_enabled:
        problems.append("API_DOCS_ENABLED must be false (Swagger and /api/openapi.json)")
    if not settings.COOKIE_SECURE:
        problems.append("COOKIE_SECURE must be true")
    if any(o.strip() == "*" for o in settings.CORS_ORIGINS):
        problems.append("CORS_ORIGINS must not contain '*'")
    if settings.TRADING_LIVE_ENABLED and settings.PHASE_G_APPROVED != "yes":
        problems.append("TRADING_LIVE_ENABLED=true requires PHASE_G_APPROVED=yes")
    if settings.github_configured and not settings.GITHUB_API_URL.startswith("https://"):
        problems.append("GITHUB_API_URL must be https when GITHUB_TOKEN is set")
    if settings.github_configured and not settings.GITHUB_GIT_URL.startswith("https://"):
        problems.append("GITHUB_GIT_URL must be https when GITHUB_TOKEN is set")
    try:
        check_data_root(settings)
    except RuntimeError as exc:
        problems.append(f"data root: {exc}")
    problems.extend(ingest_token_problems(settings))
    if bool((settings.CF_ACCESS_TEAM_DOMAIN or "").strip()) != bool(settings.CF_ACCESS_AUD):
        problems.append("CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD must be set together")
    return problems


def assert_production_ready(settings: Settings) -> None:
    """No-op outside prod. In prod, raise listing every failing item."""
    if settings.ENV is not Environment.PROD:
        return
    problems = production_problems(settings)
    if problems:
        raise ProductionConfigError("refusing to start with ENV=prod: " + "; ".join(problems))
