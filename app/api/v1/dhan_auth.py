"""Dhan API key login: /api/v1/dhan/auth (docs/11-dhan.md, "Log in with the API key").

    POST   /start            admin+CSRF  -> {login_url}: Dhan's consent login page
    GET    /callback?tokenId=  public     -> 303 to the Settings page, which POSTs /consume
    POST   /consume          admin+CSRF  {token_id} -> exchanges it, stores the session
    GET    /status           signed in   -> session state (no token)
    DELETE /session          admin+CSRF  -> log out (the manual DHAN_ACCESS_TOKEN applies)

Why the callback is a redirect and not the exchange itself: Dhan sends the browser back from
auth.dhan.co, a cross-site navigation, and the session/CSRF cookies are SameSite=Strict, so
that request arrives without a session. The SPA page it lands on is same-origin, so its POST
/consume carries the session and the CSRF header.

State check: Dhan's flow has no state parameter, so /start records a one-time pending consent
for THIS session (Redis, 10 min) and /consume needs it (GETDEL). The exchanged token must also
belong to the configured DHAN_CLIENT_ID, so a tokenId from a crafted link (someone else's Dhan
login) is refused. Every step is audited without values; no Dhan body is ever returned.
Read-only: this only obtains the data session; there is no order code anywhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.trading.ratelimit import BrokerRateLimiter, RateLimitExceeded, Window
from app.auth.deps import AdminRequired, Current, DbSession
from app.core.config import Settings
from app.services import app_settings as svc
from app.services import app_settings_runtime, audit
from app.services import dhan_auth as dhan
from app.services.audit import ClientInfo

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/v1/dhan/auth", tags=["dhan"])

# The SPA page that finishes the login (CcSettings handles ?dhan_token_id= / ?dhan_error=).
SETTINGS_PAGE = "/dev/settings"
# Dhan allows 25 consents a day; leave room for retries without burning them all.
STARTS_PER_HOUR = 8


class StartOut(BaseModel):
    login_url: str
    expires_in: int


class ConsumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_id: str = Field(min_length=8, max_length=256)


class StatusOut(BaseModel):
    client_id_set: bool
    api_key_configured: bool
    redirect_url: str | None
    session_active: bool
    session_expires_at: datetime | None
    session_expired: bool
    source: Literal["session", "manual", "none"]
    valid_until: datetime | None
    expires_soon: bool
    data_stale: bool


def _error(exc: dhan.DhanAuthError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "detail": exc.detail})


def _pending_key(admin: AdminRequired) -> str:
    return f"dhan:auth:pending:{admin.session.id}"


def _http(request: Request) -> httpx.AsyncClient:
    # Tests inject a transport; production uses a fresh client per call.
    return httpx.AsyncClient(transport=getattr(request.app.state, "dhan_auth_transport", None))


async def start_limited(request: Request, admin: AdminRequired) -> None:
    limiter = BrokerRateLimiter(
        request.app.state.redis,
        f"rl:dhan-auth:{admin.user.id}",
        {"start": [Window(3600, STARTS_PER_HOUR)]},
    )
    try:
        await limiter.acquire("start", max_wait=0)
    except RateLimitExceeded as exc:
        raise HTTPException(
            429,
            {"code": "rate_limited", "detail": "too many Dhan logins started; try later"},
            headers={"Retry-After": str(max(1, int(exc.wait_s + 0.999)))},
        ) from exc
    except Exception as exc:  # Redis down: no pending state can be kept either
        raise HTTPException(
            503, {"code": "unavailable", "detail": "Dhan login is unavailable"}
        ) from exc


@router.post("/start", response_model=StartOut, dependencies=[Depends(start_limited)])
async def start(request: Request, admin: AdminRequired, db: DbSession) -> StartOut:
    """Ask Dhan for a consent and return the login page the browser should open."""
    settings: Settings = request.app.state.settings
    if svc.cipher_for(request.app.state.env_settings) is None:
        raise HTTPException(
            503,
            {"code": "settings_key_missing", "detail": "SETTINGS_MASTER_KEY is not set"},
        )
    async with _http(request) as http:
        try:
            consent = await dhan.generate_consent(settings, http)
        except dhan.DhanAuthError as exc:
            log.warning("dhan_login_start_failed", code=exc.code)
            raise _error(exc) from None
    await request.app.state.redis.set(_pending_key(admin), "1", ex=dhan.CONSENT_TTL_S)
    audit.record(
        db,
        "dhan_login_started",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"client_id_set": True},
    )
    await db.commit()
    return StartOut(login_url=dhan.login_url(settings, consent), expires_in=dhan.CONSENT_TTL_S)


@router.get("/callback", include_in_schema=False)
@router.get("/callback/", include_in_schema=False)  # Dhan appends "/?tokenId=…"
async def callback(token_id: str | None = Query(default=None, alias="tokenId")) -> RedirectResponse:
    """Where Dhan sends the browser. Does nothing but hand the tokenId to the SPA page (see
    the module docstring); a malformed or missing one becomes an error flag instead."""
    if token_id and dhan.ID_RE.match(token_id):
        target = f"{SETTINGS_PAGE}?dhan_token_id={token_id}"
    else:
        target = f"{SETTINGS_PAGE}?dhan_error=bad_callback"
    resp = RedirectResponse(target, status_code=303)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@router.post("/consume", response_model=StatusOut)
async def consume(
    body: ConsumeIn, request: Request, admin: AdminRequired, db: DbSession
) -> StatusOut:
    """Exchange the tokenId for the session token and store it (encrypted)."""
    redis = request.app.state.redis
    try:
        pending = await redis.getdel(_pending_key(admin))
    except Exception as exc:
        raise HTTPException(
            503, {"code": "unavailable", "detail": "Dhan login is unavailable"}
        ) from exc
    if not pending:
        raise HTTPException(
            409,
            {
                "code": "no_pending_login",
                "detail": "no Dhan login was started from this session (or it timed out)",
            },
        )
    cipher = svc.cipher_for(request.app.state.env_settings)
    if cipher is None:
        raise HTTPException(
            503, {"code": "settings_key_missing", "detail": "SETTINGS_MASTER_KEY is not set"}
        )
    settings: Settings = request.app.state.settings
    async with _http(request) as http:
        try:
            session = await dhan.consume_consent(settings, http, body.token_id)
        except dhan.DhanAuthError as exc:
            log.warning("dhan_login_consume_failed", code=exc.code)
            audit.record(
                db,
                "dhan_login_failed",
                ClientInfo.from_request(request),
                actor_user_id=admin.user.id,
                details={"code": exc.code},
            )
            await db.commit()
            raise _error(exc) from None
    await dhan.store_session(db, cipher, session, admin.user.id)
    audit.record(
        db,
        "dhan_login_completed",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"expires_at": session.expires_at.isoformat()},
    )
    await db.commit()
    await app_settings_runtime.reload(request.app)
    await svc.publish(redis, list(dhan.SESSION_KEYS))
    return StatusOut.model_validate(dhan.status(request.app.state.settings))


@router.get("/status", response_model=StatusOut)
async def session_status(request: Request, _: Current) -> StatusOut:
    """For /settings and /trading: is there a valid Dhan session, until when. No token."""
    return StatusOut.model_validate(dhan.status(request.app.state.settings))


@router.delete("/session", response_model=StatusOut)
async def logout(request: Request, admin: AdminRequired, db: DbSession) -> StatusOut:
    """Forget the stored session (Dhan has no revoke call for API key tokens; it expires)."""
    removed = await dhan.clear_session(db)
    audit.record(
        db,
        "dhan_logout",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"removed": removed},
    )
    await db.commit()
    if removed:
        await app_settings_runtime.reload(request.app)
        await svc.publish(request.app.state.redis, list(dhan.SESSION_KEYS))
    return StatusOut.model_validate(dhan.status(request.app.state.settings))
