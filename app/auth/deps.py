"""FastAPI dependencies: current user, CSRF check, admin guard."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import csrf_token_for
from app.auth.service import AuthService
from app.core.config import Settings
from app.db.session import get_session
from app.models.auth import ROLE_ADMIN, User, UserSession

CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

DbSession = Annotated[AsyncSession, Depends(get_session)]


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


def get_auth_service(db: DbSession, settings: SettingsDep) -> AuthService:
    return AuthService(db, timedelta(hours=settings.SESSION_TTL_HOURS))


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


@dataclass(slots=True)
class CurrentSession:
    user: User
    session: UserSession
    token: str


async def get_current(
    request: Request, svc: AuthServiceDep, settings: SettingsDep
) -> CurrentSession:
    token = request.cookies.get(settings.session_cookie_name)
    resolved = await svc.resolve(token) if token else None
    if resolved is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
    session, user = resolved
    return CurrentSession(user=user, session=session, token=token or "")


Current = Annotated[CurrentSession, Depends(get_current)]


async def require_csrf(request: Request, current: Current, settings: SettingsDep) -> CurrentSession:
    """Double-submit check for state-changing cookie-auth requests.

    The header must equal the CSRF cookie, and both must equal HMAC(SECRET_KEY, session token),
    so a token planted by a sibling subdomain or copied from another session is rejected.
    """
    if request.method in SAFE_METHODS:
        return current
    header = request.headers.get(CSRF_HEADER, "")
    cookie = request.cookies.get(settings.csrf_cookie_name, "")
    expected = csrf_token_for(current.token, settings.SECRET_KEY.get_secret_value())
    if not (
        header and hmac.compare_digest(header, cookie) and hmac.compare_digest(header, expected)
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")
    return current


CsrfProtected = Annotated[CurrentSession, Depends(require_csrf)]


async def require_admin(current: CsrfProtected) -> CurrentSession:
    if current.user.role.name != ROLE_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
    return current


AdminRequired = Annotated[CurrentSession, Depends(require_admin)]
