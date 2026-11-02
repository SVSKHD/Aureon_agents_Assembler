"""Aureon MT5 bot integration (Trading Phase C): read-only, via the bot's status export.
See docs/12-mt5-adapter.md."""

from __future__ import annotations

from app.agents.trading.mt5.adapter import MT5Adapter
from app.agents.trading.mt5.status import StatusSource
from app.core.config import Settings


def build_mt5_adapter(settings: Settings) -> MT5Adapter | None:
    """The adapter for the env-configured export, or None if neither file nor URL is set."""
    if not settings.mt5_configured:
        return None
    token = settings.MT5_STATUS_TOKEN.get_secret_value() if settings.MT5_STATUS_TOKEN else None
    source = StatusSource(
        path=settings.MT5_STATUS_FILE,
        url=None if settings.MT5_STATUS_FILE else settings.MT5_STATUS_URL,
        token=token,
        timeout=settings.MT5_HTTP_TIMEOUT,
    )
    return MT5Adapter(
        source,
        account_id=settings.MT5_ACCOUNT_ID,
        currency=settings.MT5_ACCOUNT_CURRENCY.upper(),
        stale_after=settings.MT5_STALE_SECONDS,
    )
