"""Dhan API key login: pure parts (no DB). Fixtures follow dhanhq.co/docs/v2/authentication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from app.agents.trading.base import CredentialsExpired
from app.agents.trading.dhan.client import DhanClient
from app.core.config import Settings
from app.services import alerts, dhan_auth
from app.services import app_settings as svc

NOW = datetime(2026, 11, 4, 6, 0, tzinfo=UTC)
SESSION = "eyJhbGciOiJIUzUxMiJ9." + "S" * 60 + ".sig"
MANUAL = "manual-token-" + "m" * 40


def _s(settings: Settings, **kw: object) -> Settings:
    return settings.model_copy(update={"DHAN_CLIENT_ID": "1100003626", **kw})


def test_expiry_is_ist_without_offset() -> None:
    assert dhan_auth.parse_expiry("2025-09-23T12:37:23") == datetime(
        2025, 9, 23, 7, 7, 23, tzinfo=UTC
    )
    assert dhan_auth.parse_expiry("2026-01-01T00:00:00.000") == datetime(
        2025, 12, 31, 18, 30, tzinfo=UTC
    )
    assert dhan_auth.parse_expiry("2026-01-01T00:00:00+00:00") == datetime(2026, 1, 1, tzinfo=UTC)
    for bad in (None, "", "tomorrow", 123):
        with pytest.raises(dhan_auth.DhanAuthError):
            dhan_auth.parse_expiry(bad)


def test_session_token_wins_until_it_expires_then_manual(settings: Settings) -> None:
    s = _s(
        settings,
        DHAN_SESSION_TOKEN=SecretStr(SESSION),
        DHAN_SESSION_EXPIRES_AT=NOW + timedelta(hours=2),
        DHAN_ACCESS_TOKEN=SecretStr(MANUAL),
    )
    assert s.dhan_access_token(NOW).get_secret_value() == SESSION  # type: ignore[union-attr]
    later = NOW + timedelta(hours=3)
    assert s.dhan_access_token(later).get_secret_value() == MANUAL  # type: ignore[union-attr]
    none = _s(settings, DHAN_SESSION_TOKEN=SecretStr(SESSION), DHAN_SESSION_EXPIRES_AT=NOW)
    assert none.dhan_access_token(later) is None


def test_status_sources_and_staleness(settings: Settings) -> None:
    active = _s(
        settings,
        DHAN_SESSION_TOKEN=SecretStr(SESSION),
        DHAN_SESSION_EXPIRES_AT=NOW + timedelta(minutes=30),
        DHAN_SESSION_ALERT_MINUTES=60,
    )
    st = dhan_auth.status(active, NOW)
    assert st["source"] == "session" and st["session_active"] and st["expires_soon"]
    assert not st["data_stale"] and SESSION not in str(st)

    expired = _s(settings, DHAN_SESSION_TOKEN=SecretStr(SESSION), DHAN_SESSION_EXPIRES_AT=NOW)
    st = dhan_auth.status(expired, NOW + timedelta(seconds=1))
    assert st["source"] == "none" and st["session_expired"] and st["data_stale"]

    nothing = _s(settings)
    assert dhan_auth.status(nothing, NOW)["data_stale"] is True
    unconfigured = settings.model_copy(update={"DHAN_CLIENT_ID": None})
    assert dhan_auth.status(unconfigured, NOW)["data_stale"] is False


def test_alert_expired_and_expiring_keyed_by_expiry(settings: Settings) -> None:
    base = {"DHAN_API_KEY": SecretStr("k" * 30), "DHAN_SESSION_TOKEN": SecretStr(SESSION)}
    soon = _s(settings, **base, DHAN_SESSION_EXPIRES_AT=NOW + timedelta(minutes=40))
    a = alerts._dhan_session_alert(soon, NOW)
    assert a is not None and a.severity == "warning" and "40 min" in a.title
    assert SESSION not in a.title + a.detail and a.path == "/dev/settings"
    gone = _s(settings, **base, DHAN_SESSION_EXPIRES_AT=NOW - timedelta(minutes=1))
    b = alerts._dhan_session_alert(gone, NOW)
    assert b is not None and b.severity == "error" and "stale" in b.title
    later = _s(settings, **base, DHAN_SESSION_EXPIRES_AT=NOW + timedelta(hours=20))
    assert alerts._dhan_session_alert(later, NOW) is None
    # A new login (new expiry) is a new fingerprint, so it is not deduplicated away.
    other = _s(settings, **base, DHAN_SESSION_EXPIRES_AT=NOW + timedelta(minutes=41))
    assert alerts._dhan_session_alert(other, NOW).fingerprint != a.fingerprint  # type: ignore[union-attr]
    # Dhan not in use: nothing.
    assert (
        alerts._dhan_session_alert(settings.model_copy(update={"DHAN_CLIENT_ID": None}), NOW)
        is None
    )


async def test_client_reads_the_token_on_every_request() -> None:
    seen: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("access-token"))
        return httpx.Response(200, json={"dhanClientId": "1"})

    current = {"t": "first-token"}
    client = DhanClient(
        "1",
        lambda: current["t"],
        base_url="https://api.dhan.test/v2",
        limiter=None,
        transport=httpx.MockTransport(handler),
    )
    await client.profile()
    current["t"] = "second-token"
    await client.profile()
    assert seen == ["first-token", "second-token"]
    current["t"] = None  # type: ignore[assignment]
    with pytest.raises(CredentialsExpired):
        await client.profile()
    await client.aclose()


def test_managed_and_new_keys_are_registered() -> None:
    for key in ("DHAN_API_KEY", "DHAN_API_SECRET", "DHAN_SESSION_TOKEN"):
        assert svc.REGISTRY[key].secret and svc.REGISTRY[key].reauth
    assert svc.REGISTRY["DHAN_SESSION_TOKEN"].managed
    assert svc.REGISTRY["DHAN_SESSION_EXPIRES_AT"].managed
    assert not svc.REGISTRY["DHAN_API_KEY"].managed
    assert svc.REGISTRY["DHAN_AUTH_URL"].reauth  # the key + secret are sent there


def test_secrets_reach_the_scrubber(settings: Settings) -> None:
    s = _s(
        settings,
        DHAN_API_KEY=SecretStr("api-key-" + "a" * 20),
        DHAN_API_SECRET=SecretStr("api-secret-" + "b" * 20),
        DHAN_SESSION_TOKEN=SecretStr(SESSION),
    )
    values = svc.secret_values(s)
    assert {"api-key-" + "a" * 20, "api-secret-" + "b" * 20, SESSION} <= set(values)


def test_login_url_is_built_safely(settings: Settings) -> None:
    s = settings.model_copy(update={"DHAN_AUTH_URL": "https://auth.dhan.co/"})
    assert dhan_auth.login_url(s, "abc-123_x.y") == (
        "https://auth.dhan.co/login/consentApp-login?consentAppId=abc-123_x.y"
    )
    assert not dhan_auth.ID_RE.match("../../evil")
    assert not dhan_auth.ID_RE.match("a b c d e f g h")
