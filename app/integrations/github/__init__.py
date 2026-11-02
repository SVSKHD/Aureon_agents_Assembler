"""GitHub REST integration (docs/18-github-foundation.md)."""

from __future__ import annotations

from app.core.config import Settings
from app.integrations.github.client import GitHubClient


def build_github_client(settings: Settings) -> GitHubClient | None:
    """The client for GITHUB_TOKEN, or None when no token is configured."""
    if not settings.github_configured or settings.GITHUB_TOKEN is None:
        return None
    return GitHubClient(
        settings.GITHUB_TOKEN.get_secret_value(),
        settings.GITHUB_API_URL,
        connect_timeout=settings.GITHUB_CONNECT_TIMEOUT,
        read_timeout=settings.GITHUB_READ_TIMEOUT,
        max_attempts=settings.GITHUB_MAX_ATTEMPTS,
        backoff_base=settings.GITHUB_RETRY_BACKOFF_SECONDS,
        max_retry_wait=settings.GITHUB_MAX_RETRY_WAIT_SECONDS,
        rate_limit_floor=settings.GITHUB_RATE_LIMIT_FLOOR,
        max_pages=settings.GITHUB_MAX_PAGES,
        cache_ttl=settings.GITHUB_CACHE_TTL_SECONDS,
    )
