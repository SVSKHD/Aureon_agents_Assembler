"""Auth flows against the isolated test database + Redis (run via `make test-db`)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth.bootstrap import AdminExistsError, bootstrap_admin
from app.auth.security import hash_password
from app.auth.service import AuthService
from app.core.config import Settings
from app.main import create_app
from app.models.auth import AuditEvent, User, UserSession
from app.services.audit import ClientInfo

pytestmark = pytest.mark.db

EMAIL = "owner@spasta.online"
PASSWORD = "correct horse battery staple"


@pytest.fixture
async def maker(db_clean: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_clean)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def admin(maker: async_sessionmaker[AsyncSession]) -> User:
    return await bootstrap_admin(maker, EMAIL, PASSWORD)


async def _user(maker: async_sessionmaker[AsyncSession], email: str, **kw: object) -> User:
    async with maker() as db, db.begin():
        u = User(email=email, password_hash=hash_password(PASSWORD), role_id=2, **kw)
        db.add(u)
    return u


async def _events(maker: async_sessionmaker[AsyncSession]) -> list[str]:
    async with maker() as db:
        rows = await db.scalars(select(AuditEvent.event_type).order_by(AuditEvent.id))
        return list(rows)


async def _login(c: AsyncClient, email: str = EMAIL, password: str = PASSWORD) -> str:
    r = await c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return str(r.json()["csrf_token"])


@pytest.fixture
async def second_client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with LifespanManager(app) as mgr:
        async with AsyncClient(transport=ASGITransport(app=mgr.app), base_url="https://test") as c:
            yield c


# --- login / me -------------------------------------------------------------------------


async def test_login_sets_hardened_cookies_and_me_works(
    admin: User, client: AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    r = await client.post(
        "/api/auth/login", json={"email": " Owner@Spasta.online ", "password": PASSWORD}
    )
    assert r.status_code == 200
    cookies = r.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("__Host-aureon_session="))
    csrf_cookie = next(c for c in cookies if c.startswith("__Host-aureon_csrf="))
    for c in (session_cookie, csrf_cookie):
        assert "Secure" in c and "SameSite=strict" in c and "Path=/" in c
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    assert r.json()["user"]["role"] == "admin"

    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL
    assert (await _events(maker))[-1] == "login"

    async with maker() as db:
        stored = (await db.scalars(select(UserSession.token_hash))).one()
    raw = session_cookie.split(";")[0].split("=", 1)[1]
    assert raw.encode() not in stored  # only the hash is stored


async def test_me_requires_session(client: AsyncClient, db_clean: str) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_wrong_password_and_unknown_email_look_identical(
    admin: User, client: AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    bad_pw = await client.post("/api/auth/login", json={"email": EMAIL, "password": "nope"})
    unknown = await client.post(
        "/api/auth/login", json={"email": "x@spasta.online", "password": PASSWORD}
    )
    assert bad_pw.status_code == unknown.status_code == 401
    assert bad_pw.json() == unknown.json()
    assert (await _events(maker)).count("login_failed") == 2


async def test_inactive_user_cannot_log_in(
    client: AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _user(maker, "off@spasta.online", is_active=False)
    r = await client.post(
        "/api/auth/login", json={"email": "off@spasta.online", "password": PASSWORD}
    )
    assert r.status_code == 401


# --- rate limit ---------------------------------------------------------------------------


async def test_rate_limit_trips_and_blocks_even_correct_password(
    admin: User, client: AsyncClient, settings: Settings, maker: async_sessionmaker[AsyncSession]
) -> None:
    for _ in range(settings.LOGIN_MAX_FAILURES):
        r = await client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong"})
        assert r.status_code == 401
    r = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 429
    assert 0 < int(r.headers["retry-after"]) <= settings.LOGIN_WINDOW_SECONDS
    assert "login_rate_limited" in await _events(maker)


async def test_successful_login_resets_pair_counter(
    admin: User, client: AsyncClient, settings: Settings
) -> None:
    for _ in range(settings.LOGIN_MAX_FAILURES - 1):
        await client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong"})
    await _login(client)
    for _ in range(settings.LOGIN_MAX_FAILURES - 1):
        r = await client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong"})
        assert r.status_code == 401


# --- CSRF ---------------------------------------------------------------------------------


async def test_logout_rejects_missing_or_wrong_csrf(admin: User, client: AsyncClient) -> None:
    csrf = await _login(client)
    assert (await client.post("/api/auth/logout")).status_code == 403
    r = await client.post("/api/auth/logout", headers={"x-csrf-token": "0" * 64})
    assert r.status_code == 403
    # Header matches a forged cookie but not HMAC(session): still rejected.
    client.cookies.set("__Host-aureon_csrf", "f" * 64)
    r = await client.post("/api/auth/logout", headers={"x-csrf-token": "f" * 64})
    assert r.status_code == 403
    client.cookies.set("__Host-aureon_csrf", csrf)
    assert (await client.get("/api/auth/me")).status_code == 200  # still logged in


async def test_csrf_token_from_another_session_rejected(
    admin: User, client: AsyncClient, second_client: AsyncClient
) -> None:
    await _login(client)
    other = await _login(second_client)
    client.cookies.set("__Host-aureon_csrf", other)
    r = await client.post("/api/auth/logout", headers={"x-csrf-token": other})
    assert r.status_code == 403


async def test_logout_with_csrf_revokes_session(
    admin: User, client: AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    csrf = await _login(client)
    jar = dict(client.cookies)
    r = await client.post("/api/auth/logout", headers={"x-csrf-token": csrf})
    assert r.status_code == 204
    # Replaying the old cookie after logout fails: revoked server-side, not just cleared.
    for k, v in jar.items():
        client.cookies.set(k, v)
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await _events(maker))[-1] == "logout"


# --- session revocation / expiry ---------------------------------------------------------


async def test_revoke_all_ends_every_session(
    admin: User,
    client: AsyncClient,
    second_client: AsyncClient,
    maker: async_sessionmaker[AsyncSession],
) -> None:
    csrf = await _login(client)
    await _login(second_client)
    r = await client.post("/api/auth/sessions/revoke-all", headers={"x-csrf-token": csrf})
    assert r.status_code == 200
    assert r.json() == {"revoked": 2}
    assert (await second_client.get("/api/auth/me")).status_code == 401
    assert (await client.get("/api/auth/me")).status_code == 401
    assert "sessions_revoked_all" in await _events(maker)


async def test_expired_session_rejected(
    admin: User, client: AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _login(client)
    async with maker() as db, db.begin():
        await db.execute(update(UserSession).values(expires_at=text("now() - interval '1 s'")))
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_deactivating_user_ends_access(
    admin: User, client: AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await _login(client)
    async with maker() as db, db.begin():
        await db.execute(update(User).values(is_active=False))
    assert (await client.get("/api/auth/me")).status_code == 401


# --- roles / bootstrap --------------------------------------------------------------------


async def test_role_change_is_audited_and_revokes_sessions(
    admin: User, client: AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    target = await _user(maker, "member@spasta.online")
    await _login(client, "member@spasta.online")
    async with maker() as db, db.begin():
        actor = await db.get(User, admin.id)
        assert actor is not None
        svc = AuthService(db, session_ttl=__import__("datetime").timedelta(hours=1))
        await svc.change_role(actor, target.id, "admin", ClientInfo(ip="127.0.0.1"))
    async with maker() as db:
        ev = (
            await db.scalars(select(AuditEvent).where(AuditEvent.event_type == "role_changed"))
        ).one()
        assert ev.actor_user_id == admin.id and ev.target_user_id == target.id
        assert ev.details == {"from": "user", "to": "admin"}
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_bootstrap_refuses_second_admin(
    admin: User, maker: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(AdminExistsError):
        await bootstrap_admin(maker, "second@spasta.online", PASSWORD)


async def test_bootstrap_rejects_short_password(maker: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(ValueError, match="at least"):
        await bootstrap_admin(maker, EMAIL, "short")
    async with maker() as db:
        assert (await db.scalar(select(User.id))) is None
