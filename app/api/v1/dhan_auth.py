"""Dhan API key login: /api/v1/dhan/auth (docs/11-dhan.md, "Log in with the API key").

    POST   /start            admin+CSRF  -> {login_url}: Dhan's consent login page; sets the
                                         one-time login cookie (app/services/dhan_login_handoff.py)
    GET    /callback?tokenId=  public     -> keeps the tokenId server-side for that login, then
                                         303 to the Settings page (?dhan_login=ready)
    POST   /consume          admin+CSRF  -> exchanges it, stores the session
    GET    /status           signed in   -> session state (no token)
    GET    /connection       signed in   -> persisted connection state (dhan.connections)
    POST   /verify           admin+CSRF  -> verify the credential in use (GET /profile) now
    DELETE /session          admin+CSRF  -> log out (the manual DHAN_ACCESS_TOKEN applies)

Every new session token is verified with Dhan's Profile API before it is stored: a token
Dhan refuses, or one for another client id, is never stored. If Dhan cannot be reached, the
session is stored as `unverified` (the tokenId is single-use; refusing would waste the login)
and the scheduled check verifies it later (docs/28-dhan-operations.md).

Why the callback is a redirect and not the exchange itself: Dhan sends the browser back from
auth.dhan.co, a cross-site navigation, and the session/CSRF cookies are SameSite=Strict, so
that request arrives without a session. The SPA page it lands on is same-origin, so its POST
/consume carries the session and the CSRF header.

State check: Dhan's flow has no state parameter, so /start creates a random one-time state
bound to the admin's USER id (Redis, 10 min) and hands it to the browser as an HttpOnly,
SameSite=Lax cookie scoped to /api/v1/dhan/auth. The callback accepts a tokenId only for a live
state from that cookie; /consume claims it atomically, once, and only for the same user. So the
login survives the admin signing in to Spasta again (a new session id), while another user, a
replay, an expired login or an unsolicited callback is refused. The exchanged token must also
belong to the configured DHAN_CLIENT_ID (and pass the Profile API), so a tokenId from someone
else's Dhan login is refused. Every step is audited without values; no Dhan body, state,
tokenId or consent id is ever returned or logged.
Read-only: this only obtains the data session; there is no order code anywhere.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agents.trading.ratelimit import BrokerRateLimiter, RateLimitExceeded, Window
from app.api.ui import ui_module
from app.auth.deps import AdminRequired, Current, DbSession
from app.core.config import Settings
from app.models.brokers import DhanConnection
from app.services import app_settings as svc
from app.services import app_settings_runtime, audit
from app.services import dhan_auth as dhan
from app.services import dhan_connection as conn
from app.services import dhan_login_handoff as handoff
from app.services.audit import ClientInfo

log = structlog.get_logger(__name__)
UI_MODULE = ui_module("dhan", label="Dhan", group="markets", icon="landmark", order=26)
router = APIRouter(prefix="/v1/dhan/auth", tags=["dhan"])

# The SPA page that finishes the login (CcSettings handles ?dhan_token_id= / ?dhan_error=).
SETTINGS_PAGE = "/dev/settings"
# Dhan allows 25 consents a day; leave room for retries without burning them all.
STARTS_PER_HOUR = 8
# Manual verifications per admin (each is one GET /profile).
VERIFY_PER_10_MIN = 10


class StartOut(BaseModel):
    login_url: str
    expires_in: int


class ConsumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Optional: the callback keeps the tokenId server-side now. When sent (older pages), it
    # must equal the one Dhan returned for this login.
    token_id: str | None = Field(default=None, min_length=8, max_length=256)


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
    # Additive: the result of verifying the new token with Dhan's Profile API (consume only).
    verification: Literal["ok", "unavailable"] | None = None


class DhanConnectionOut(BaseModel):
    """dhan.connections, safe fields only (docs/28-dhan-operations.md)."""

    client_id: str | None
    status: Literal[
        "connected", "unverified", "expired", "rejected", "mismatch", "disconnected",
        "not_configured",
    ]  # fmt: skip
    token_source: Literal["session", "manual", "none"]
    expires_at: datetime | None
    connected_at: datetime | None
    disconnected_at: datetime | None
    data_plan: str | None
    data_valid_until: str | None
    active_segments: list[str]
    last_verified_at: datetime | None
    last_check_at: datetime | None
    last_check_outcome: str | None
    last_error_code: str | None
    consecutive_failures: int
    renewal: Literal["manual"]
    action: str | None
    version: int


def _error(exc: dhan.DhanAuthError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "detail": exc.detail})


# How long a claimed login is remembered as "used" (so a replay says so, not "expired").
USED_TTL_S = 24 * 3600

# What the browser is told when the callback cannot be used (?dhan_error=<code>).
CALLBACK_ERRORS = {
    "missing": "login_expired",
    "duplicate": "duplicate_callback",
}


def _login_cookie(response: Response, settings: Settings, state: str | None) -> None:
    """Set (state) or clear (None) the one-time login cookie."""
    name = handoff.cookie_name(settings)
    if state is None:
        response.delete_cookie(
            name, path=handoff.COOKIE_PATH, secure=settings.COOKIE_SECURE, httponly=True,
            samesite="lax",
        )  # fmt: skip
        return
    response.set_cookie(
        name, state, max_age=dhan.CONSENT_TTL_S, path=handoff.COOKIE_PATH,
        secure=settings.COOKIE_SECURE, httponly=True, samesite="lax",
    )  # fmt: skip


def _cleared_cookie_header(settings: Settings) -> dict[str, str]:
    r = Response()
    _login_cookie(r, settings, None)
    return {"set-cookie": r.headers["set-cookie"]}


def _refusal(
    status: int, code: str, detail: str, settings: Settings, *, clear: bool
) -> HTTPException:
    return HTTPException(
        status,
        {"code": code, "detail": detail},
        headers=_cleared_cookie_header(settings) if clear else None,
    )


def _transport(request: Request) -> httpx.AsyncBaseTransport | None:
    # Tests inject a transport; production uses a fresh client per call.
    t: httpx.AsyncBaseTransport | None = getattr(request.app.state, "dhan_auth_transport", None)
    return t


def _http(request: Request) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_transport(request))


async def _connection(db: DbSession, settings: Settings) -> DhanConnectionOut:
    cid = (settings.DHAN_CLIENT_ID or "").strip()
    row = await db.get(DhanConnection, cid) if cid else None
    return DhanConnectionOut.model_validate(conn.view(row, settings))


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
async def start(
    request: Request, response: Response, admin: AdminRequired, db: DbSession
) -> StartOut:
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
    try:
        state = await handoff.begin(request.app.state.redis, str(admin.user.id), dhan.CONSENT_TTL_S)
    except Exception as exc:  # Redis down: the login could not be finished, so do not start
        raise HTTPException(
            503, {"code": "unavailable", "detail": "Dhan login is unavailable"}
        ) from exc
    _login_cookie(response, settings, state)
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
async def callback(
    request: Request,
    token_id: str | None = Query(default=None, alias="tokenId"),
) -> RedirectResponse:
    """Where Dhan sends the browser (cross-site: no Spasta session). It only accepts a tokenId
    for a login THIS browser started (the login cookie) that is still pending, keeps it
    server-side (encrypted), and sends the browser to the Settings page, which finishes the
    login once the admin is signed in. The tokenId never appears in a URL we create."""
    settings: Settings = request.app.state.settings
    state = request.cookies.get(handoff.cookie_name(settings))
    error: str | None = None
    clear = False
    if not (token_id and dhan.ID_RE.match(token_id)):
        error = "bad_callback"
    elif not handoff.valid_state(state):
        error = "no_pending_login"  # unsolicited: no login was started from this browser
    else:
        cipher = svc.cipher_for(request.app.state.env_settings)
        if cipher is None:
            error = "settings_key_missing"
        else:
            try:
                result = await handoff.record_return(
                    request.app.state.redis, cipher, str(state), token_id
                )
            except Exception:
                result = None
            if result is None:
                error = "unavailable"
            elif result in CALLBACK_ERRORS:
                error = CALLBACK_ERRORS[result]
                clear = result == "missing"
    log.info("dhan_login_callback", outcome=error or "ready")
    target = f"{SETTINGS_PAGE}?dhan_error={error}" if error else f"{SETTINGS_PAGE}?dhan_login=ready"
    resp = RedirectResponse(target, status_code=303)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    if clear:
        _login_cookie(resp, settings, None)
    return resp


@router.post("/consume", response_model=StatusOut)
async def consume(
    request: Request,
    response: Response,
    admin: AdminRequired,
    db: DbSession,
    body: ConsumeIn | None = None,
) -> StatusOut:
    """Finish the login this admin started: claim the tokenId Dhan returned (once, for this
    user only), exchange it for the session token and store it (encrypted). Works after the
    admin signed in to Spasta again: the login is bound to the user, not the session."""
    settings: Settings = request.app.state.settings
    redis = request.app.state.redis
    client = ClientInfo.from_request(request)
    state = request.cookies.get(handoff.cookie_name(settings))
    if not handoff.valid_state(state):
        raise _refusal(
            409, "no_pending_login",
            "no Dhan login was started from this browser; start it again", settings, clear=False,
        )  # fmt: skip
    cipher = svc.cipher_for(request.app.state.env_settings)
    if cipher is None:
        raise HTTPException(
            503, {"code": "settings_key_missing", "detail": "SETTINGS_MASTER_KEY is not set"}
        )
    try:
        claimed = await handoff.claim(redis, cipher, str(state), str(admin.user.id), USED_TTL_S)
    except Exception as exc:
        raise HTTPException(
            503, {"code": "unavailable", "detail": "Dhan login is unavailable"}
        ) from exc

    def refused(code: str) -> None:
        log.warning("dhan_login_consume_refused", code=code)
        audit.record(db, "dhan_login_failed", client, actor_user_id=admin.user.id,
                     details={"code": code, "step": "handoff"})  # fmt: skip

    if claimed == "other_user":
        refused("dhan_login_other_user")
        await db.commit()
        # The pending login stays for the admin who started it; this user cannot claim it.
        raise _refusal(
            403, "dhan_login_other_user",
            "this Dhan login was started by another account; sign in as that admin to finish it",
            settings, clear=False,
        )  # fmt: skip
    if claimed == "not_returned":
        raise _refusal(
            409, "login_not_returned",
            "Dhan has not sent this login back yet; finish it on Dhan's page", settings,
            clear=False,
        )  # fmt: skip
    if claimed == "used":
        refused("login_already_used")
        await db.commit()
        raise _refusal(
            409, "login_already_used", "this Dhan login was already completed", settings,
            clear=True,
        )  # fmt: skip
    if claimed == "missing":
        raise _refusal(
            409, "login_expired", "the Dhan login timed out; start it again", settings,
            clear=True,
        )  # fmt: skip
    assert isinstance(claimed, handoff.Claimed)
    if body is not None and body.token_id is not None and body.token_id != claimed.token_id:
        refused("token_mismatch")
        await db.commit()
        raise _refusal(
            409, "token_mismatch", "that is not the login Dhan returned; start it again",
            settings, clear=True,
        )  # fmt: skip
    async with _http(request) as http:
        try:
            session = await dhan.consume_consent(settings, http, claimed.token_id)
        except dhan.DhanAuthError as exc:
            log.warning("dhan_login_consume_failed", code=exc.code)
            retry = False
            if exc.code == "dhan_unavailable":  # never reached Dhan: the tokenId is unspent
                try:
                    retry = await handoff.restore(redis, str(state), claimed)
                except Exception:
                    retry = False
            audit.record(
                db,
                "dhan_login_failed",
                client,
                actor_user_id=admin.user.id,
                details={"code": exc.code, "retry": retry},
            )
            await db.commit()
            raise HTTPException(
                exc.status,
                {"code": exc.code, "detail": exc.detail, "retry": retry},
                headers=None if retry else _cleared_cookie_header(settings),
            ) from None
    # Before storing: ask Dhan's Profile API whose token this is. Refused or foreign = never
    # stored; Dhan unreachable = stored as unverified (the scheduled check finishes it).
    verified = await conn.verify_token(
        settings, session.access_token, transport=_transport(request)
    )
    if verified.outcome in ("rejected", "mismatch", "not_configured"):
        refusal = (
            dhan.DhanAuthError(
                403, "dhan_client_mismatch", "the Dhan login was for a different client id"
            )
            if verified.outcome == "mismatch"
            else dhan.DhanAuthError(
                502, "dhan_token_rejected", "Dhan did not accept the new session token"
            )
        )
        log.warning("dhan_login_verify_failed", code=refusal.code)
        audit.record(
            db,
            "dhan_login_failed",
            ClientInfo.from_request(request),
            actor_user_id=admin.user.id,
            details={"code": refusal.code, "step": "profile"},
        )
        await db.commit()
        raise HTTPException(
            refusal.status,
            {"code": refusal.code, "detail": refusal.detail},
            headers=_cleared_cookie_header(settings),
        )
    await dhan.store_session(db, cipher, session, admin.user.id)
    await conn.mark_login(db, settings, verified, session.expires_at, admin.user.id)
    audit.record(
        db,
        "dhan_login_completed",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={
            "expires_at": session.expires_at.isoformat(),
            "verification": verified.outcome,
            **({"error_code": verified.error_code} if verified.error_code else {}),
        },
    )
    await db.commit()
    await app_settings_runtime.reload(request.app)
    await svc.publish(redis, list(dhan.SESSION_KEYS))
    out = StatusOut.model_validate(dhan.status(request.app.state.settings))
    out.verification = "ok" if verified.outcome == "ok" else "unavailable"
    _login_cookie(response, settings, None)
    return out


@router.get("/status", response_model=StatusOut)
async def session_status(request: Request, _: Current) -> StatusOut:
    """For /settings and /trading: is there a valid Dhan session, until when. No token."""
    return StatusOut.model_validate(dhan.status(request.app.state.settings))


@router.delete("/session", response_model=StatusOut)
async def logout(request: Request, admin: AdminRequired, db: DbSession) -> StatusOut:
    """Forget the stored session (Dhan has no revoke call for API key tokens; it expires)."""
    removed = await dhan.clear_session(db)
    # This request's settings still hold the session; the logout is recorded "without it".
    after: Settings = request.app.state.settings.model_copy(
        update={"DHAN_SESSION_TOKEN": None, "DHAN_SESSION_EXPIRES_AT": None}
    )
    row = await conn.mark_logout(db, after, admin.user.id)
    audit.record(
        db,
        "dhan_logout",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={"removed": removed, "status": row.status if row is not None else None},
    )
    await db.commit()
    if removed:
        await app_settings_runtime.reload(request.app)
        await svc.publish(request.app.state.redis, list(dhan.SESSION_KEYS))
    return StatusOut.model_validate(dhan.status(request.app.state.settings))


@router.get("/connection", response_model=DhanConnectionOut)
async def connection(request: Request, db: DbSession, _: Current) -> DhanConnectionOut:
    """The persisted connection state: verified when, by which source, what to do next."""
    return await _connection(db, request.app.state.settings)


async def verify_limited(request: Request, admin: AdminRequired) -> None:
    limiter = BrokerRateLimiter(
        request.app.state.redis,
        f"rl:dhan-verify:{admin.user.id}",
        {"verify": [Window(600, VERIFY_PER_10_MIN)]},
    )
    try:
        await limiter.acquire("verify", max_wait=0)
    except RateLimitExceeded as exc:
        raise HTTPException(
            429,
            {"code": "rate_limited", "detail": "too many Dhan verifications; try later"},
            headers={"Retry-After": str(max(1, int(exc.wait_s + 0.999)))},
        ) from exc
    except Exception as exc:  # Redis down: fail closed
        raise HTTPException(
            503, {"code": "unavailable", "detail": "Dhan verification is unavailable"}
        ) from exc


@router.post("/verify", response_model=DhanConnectionOut, dependencies=[Depends(verify_limited)])
async def verify(request: Request, admin: AdminRequired, db: DbSession) -> DhanConnectionOut:
    """Verify the credential every Dhan call uses right now (one GET /profile) and store the
    result. Dhan being unreachable is a 200 with `last_check_outcome: unavailable`: the
    previous verdict is kept, never downgraded."""
    settings: Settings = request.app.state.settings
    if not settings.DHAN_CLIENT_ID:
        raise HTTPException(
            409, {"code": "dhan_not_configured", "detail": "set DHAN_CLIENT_ID on /settings first"}
        )
    v = await conn.verify_current(settings, transport=_transport(request))
    rec = await conn.record(db, settings, v)
    audit.record(
        db,
        "dhan_credentials_verified",
        ClientInfo.from_request(request),
        actor_user_id=admin.user.id,
        details={
            "outcome": v.outcome,
            "source": v.source,
            "status": rec.row.status if rec else None,
            **({"error_code": v.error_code} if v.error_code else {}),
        },
    )
    await db.commit()
    return await _connection(db, settings)
