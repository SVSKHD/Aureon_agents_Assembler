"""Cloudflare Access JWT middleware (Phase 22): a locally generated RSA key, a JWKS in
Cloudflare's certs format, and every way a token can be wrong. No network, no DB."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import jwt
import pytest
import respx
from asgi_lifespan import LifespanManager
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import RSAAlgorithm

from app.core.cf_access import AccessDenied, AccessVerifier, is_bypassed, team_origin
from app.core.config import Settings
from app.core.prod_checks import production_problems
from app.main import create_app

TEAM = "spasta.cloudflareaccess.com"
ISS = f"https://{TEAM}"
CERTS = f"{ISS}/cdn-cgi/access/certs"
AUD_WEB = "a" * 64
AUD_API = "b" * 64


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


KEY = _key()
OTHER = _key()


def jwks(*pairs: tuple[str, rsa.RSAPrivateKey]) -> dict[str, Any]:
    """Cloudflare's /cdn-cgi/access/certs shape: `keys` (JWKs) plus `public_cert(s)`."""
    keys = []
    for kid, k in pairs:
        jwk = json.loads(RSAAlgorithm.to_jwk(k.public_key()))
        keys.append({**jwk, "kid": kid, "alg": "RS256", "use": "sig"})
    return {
        "keys": keys,
        "public_cert": {"kid": pairs[0][0], "cert": "-----BEGIN CERTIFICATE-----\n…"},
        "public_certs": [{"kid": kid, "cert": "…"} for kid, _ in pairs],
    }


def token(
    *,
    key: rsa.RSAPrivateKey = KEY,
    kid: str = "k1",
    aud: Any = (AUD_API,),
    iss: str = ISS,
    exp_in: float = 300,
    alg: str = "RS256",
    **extra: Any,
) -> str:
    now = int(time.time())
    claims = {"aud": list(aud), "iss": iss, "iat": now, "nbf": now, "exp": now + exp_in,
              "email": "owner@spasta.online", "sub": "u-1", "type": "app", **extra}  # fmt: skip
    if alg == "HS256":
        return jwt.encode(claims, "x" * 32, algorithm="HS256", headers={"kid": kid})
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def cf_settings(settings: Settings) -> Settings:
    extra = {"CF_ACCESS_TEAM_DOMAIN": TEAM, "CF_ACCESS_AUD": f"{AUD_WEB},{AUD_API}"}
    return Settings.model_validate({**settings.model_dump(), **extra})


@pytest.fixture
async def cf_client(cf_settings: Settings) -> AsyncIterator[tuple[AsyncClient, respx.Route]]:
    app = create_app(cf_settings)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(CERTS).mock(return_value=httpx.Response(200, json=jwks(("k1", KEY))))
        async with LifespanManager(app) as mgr:
            async with AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://t") as c:
                yield c, route


def H(tok: str) -> dict[str, str]:
    return {"Cf-Access-Jwt-Assertion": tok}


async def test_valid_token_passes_and_missing_one_is_refused(
    cf_client: tuple[AsyncClient, respx.Route],
) -> None:
    c, route = cf_client
    r = await c.get("/api/auth/me", headers=H(token()))
    assert r.status_code == 401  # past Access; the app's own session check answers
    r = await c.get("/api/auth/me", headers=H(token(aud=(AUD_WEB,))))  # the other app's AUD
    assert r.status_code == 401
    r = await c.get("/api/auth/me")
    assert r.status_code == 403 and r.json()["detail"]["code"] == "cf_access_required"
    assert r.headers["x-content-type-options"] == "nosniff"  # still inside security headers
    assert route.call_count == 1  # the certs are cached


@pytest.mark.parametrize(
    "bad",
    [
        lambda: token(exp_in=-120),  # expired (beyond the 30 s leeway)
        lambda: token(aud=("c" * 64,)),  # another Access application
        lambda: token(iss="https://evil.cloudflareaccess.com"),
        lambda: token(key=OTHER),  # right kid, wrong signature
        lambda: token(alg="HS256"),  # algorithm confusion
        lambda: token(kid="nope"),  # unknown key
        lambda: "not-a-jwt",
        lambda: token() + "x",
    ],
)
async def test_bad_tokens_are_refused(
    cf_client: tuple[AsyncClient, respx.Route], bad: Callable[[], str]
) -> None:
    c, _ = cf_client
    r = await c.get("/api/auth/me", headers=H(bad()))
    assert r.status_code == 403


async def test_bypass_paths_need_no_token(cf_client: tuple[AsyncClient, respx.Route]) -> None:
    c, route = cf_client
    assert (await c.get("/api/health")).status_code == 200
    r = await c.post("/api/v1/github/webhooks", content=b"{}")
    assert r.status_code != 403  # reaches the HMAC receiver (unconfigured here)
    r = await c.get("/api/public/no-such-thing")
    assert r.status_code == 404  # reached the router
    # Prefix tricks are not bypasses.
    assert (await c.get("/api/healthz")).status_code == 403
    assert (await c.post("/api/v1/github/webhooks-evil")).status_code == 403
    assert (await c.get("/api/publicity")).status_code == 403
    assert route.call_count == 0  # no token was needed, so no certs were fetched


async def test_off_by_default(settings: Settings) -> None:
    assert not settings.cf_access_enabled
    app = create_app(settings)
    async with LifespanManager(app) as mgr:
        async with AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://t") as c:
            assert (await c.get("/api/auth/me")).status_code == 401  # no Access check at all


async def test_key_rotation_refetches_once_and_ttl_expires() -> None:
    calls: list[str] = []
    current = {"doc": jwks(("k1", KEY))}

    async def fetch(url: str) -> dict[str, Any]:
        calls.append(url)
        return current["doc"]

    clock = {"t": 1000.0}
    v = AccessVerifier(TEAM, [AUD_API], ttl_s=3600, fetcher=fetch, clock=lambda: clock["t"])
    await v.verify(token())
    await v.verify(token())
    assert calls == [CERTS]
    # Rotation: a new kid appears. The first unknown kid within a minute of the last fetch is
    # refused without hammering the endpoint; after a minute it refetches and succeeds.
    current["doc"] = jwks(("k2", OTHER), ("k1", KEY))
    with pytest.raises(AccessDenied):
        await v.verify(token(key=OTHER, kid="k2"))
    assert len(calls) == 1
    clock["t"] += 61
    claims = await v.verify(token(key=OTHER, kid="k2"))
    assert claims["email"] == "owner@spasta.online" and len(calls) == 2
    clock["t"] += 3601  # TTL
    await v.verify(token())
    assert len(calls) == 3


async def test_jwks_outage_keeps_cached_keys_and_fails_closed_without_any() -> None:
    async def down(url: str) -> dict[str, Any]:
        raise httpx.ConnectError("down")

    v = AccessVerifier(TEAM, [AUD_API], fetcher=down)
    with pytest.raises(AccessDenied) as e:
        await v.verify(token())
    assert e.value.reason == "jwks_unavailable"

    ok = {"n": 0}

    async def flaky(url: str) -> dict[str, Any]:
        ok["n"] += 1
        if ok["n"] > 1:
            raise httpx.ConnectError("down")
        return jwks(("k1", KEY))

    clock = {"t": 0.0}
    v = AccessVerifier(TEAM, [AUD_API], ttl_s=60, fetcher=flaky, clock=lambda: clock["t"])
    await v.verify(token())
    clock["t"] += 120  # TTL passed and the endpoint is down: cached keys still verify
    await v.verify(token())


def test_helpers_and_prod_check(settings: Settings) -> None:
    assert team_origin("https://spasta.cloudflareaccess.com/") == ISS
    assert team_origin(TEAM) == ISS
    prefixes = ["/api/v1/github/webhooks", "/api/public/", "/api/health"]
    assert is_bypassed("/api/v1/github/webhooks", prefixes)
    assert is_bypassed("/api/public/forex", prefixes)
    assert is_bypassed("/api/health/ready", prefixes)
    assert not is_bypassed("/api/public", ["/api/publicx"])
    assert not is_bypassed("/api/v1/github/webhooksx", prefixes)
    half = Settings.model_validate({**settings.model_dump(), "CF_ACCESS_TEAM_DOMAIN": TEAM})
    assert "CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD must be set together" in production_problems(
        half
    )
