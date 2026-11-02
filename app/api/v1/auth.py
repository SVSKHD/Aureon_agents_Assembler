"""Cookie-session auth endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.auth.deps import AuthServiceDep, CsrfProtected, Current, DbSession, SettingsDep
from app.auth.rate_limit import LoginRateLimiter
from app.auth.security import csrf_token_for
from app.auth.service import normalize_email
from app.core.config import Settings
from app.models.auth import User
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    RevokeAllResponse,
    SessionOut,
    UserOut,
)
from app.services import audit
from app.services.audit import ClientInfo

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_CREDENTIALS = "invalid email or password"


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id, email=user.email, role=user.role.name, last_login_at=user.last_login_at
    )


def _set_cookies(response: Response, settings: Settings, token: str, csrf: str) -> None:
    max_age = settings.SESSION_TTL_HOURS * 3600
    common = {
        "max_age": max_age,
        "path": "/",
        "secure": settings.COOKIE_SECURE,
        "samesite": "strict",
    }
    response.set_cookie(settings.session_cookie_name, token, httponly=True, **common)  # type: ignore[arg-type]
    # Readable by the SPA so it can echo the value in X-CSRF-Token.
    response.set_cookie(settings.csrf_cookie_name, csrf, httponly=False, **common)  # type: ignore[arg-type]


def _clear_cookies(response: Response, settings: Settings) -> None:
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(name, path="/", secure=settings.COOKIE_SECURE, samesite="strict")


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    svc: AuthServiceDep,
    settings: SettingsDep,
) -> LoginResponse:
    client = ClientInfo.from_request(request)
    email = normalize_email(body.email)
    ip = client.ip or "unknown"
    limiter = LoginRateLimiter(request.app.state.redis, settings)

    retry = await limiter.retry_after(email, ip)
    if retry is not None:
        audit.record(db, "login_rate_limited", client, details={"email": email})
        await db.commit()
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "too many failed login attempts",
            headers={"Retry-After": str(retry)},
        )

    user = await svc.authenticate(email, body.password)
    if user is None:
        await limiter.record_failure(email, ip)
        audit.record(db, "login_failed", client, details={"email": email})
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_CREDENTIALS)

    token = await svc.create_session(user, client)
    audit.record(db, "login", client, actor_user_id=user.id, target_user_id=user.id)
    await db.commit()
    await limiter.reset(email, ip)

    csrf = csrf_token_for(token, settings.SECRET_KEY.get_secret_value())
    _set_cookies(response, settings, token, csrf)
    return LoginResponse(
        user=_user_out(user),
        csrf_token=csrf,
        expires_at=datetime.now(UTC) + timedelta(hours=settings.SESSION_TTL_HOURS),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    current: CsrfProtected,
    db: DbSession,
    svc: AuthServiceDep,
    settings: SettingsDep,
) -> None:
    await svc.revoke(current.session.id)
    audit.record(
        db,
        "logout",
        ClientInfo.from_request(request),
        actor_user_id=current.user.id,
        target_user_id=current.user.id,
    )
    await db.commit()
    _clear_cookies(response, settings)


@router.get("/me", response_model=UserOut)
async def me(current: Current) -> UserOut:
    return _user_out(current.user)


@router.post("/sessions/revoke-all", response_model=RevokeAllResponse)
async def revoke_all(
    request: Request,
    response: Response,
    current: CsrfProtected,
    db: DbSession,
    svc: AuthServiceDep,
    settings: SettingsDep,
) -> RevokeAllResponse:
    """Sign out every session of the current user, including this one."""
    count = await svc.revoke_all(current.user.id)
    audit.record(
        db,
        "sessions_revoked_all",
        ClientInfo.from_request(request),
        actor_user_id=current.user.id,
        target_user_id=current.user.id,
        details={"count": count},
    )
    await db.commit()
    _clear_cookies(response, settings)
    return RevokeAllResponse(revoked=count)


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(current: Current, svc: AuthServiceDep) -> list[SessionOut]:
    """The current user's active (not revoked, not expired) sessions."""
    return [
        SessionOut(
            id=row.id,
            created_at=row.created_at,
            expires_at=row.expires_at,
            ip=row.ip,
            user_agent=row.user_agent,
            current=row.id == current.session.id,
        )
        for row in await svc.active_sessions(current.user.id)
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID,
    request: Request,
    response: Response,
    current: CsrfProtected,
    db: DbSession,
    svc: AuthServiceDep,
    settings: SettingsDep,
) -> None:
    """Sign out one of your own sessions. Another user's (or an unknown) id is 404."""
    if not await svc.revoke_own(current.user.id, session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "session not found")
    audit.record(
        db,
        "session_revoked",
        ClientInfo.from_request(request),
        actor_user_id=current.user.id,
        target_user_id=current.user.id,
        details={"session_id": str(session_id), "current": session_id == current.session.id},
    )
    await db.commit()
    if session_id == current.session.id:
        _clear_cookies(response, settings)
