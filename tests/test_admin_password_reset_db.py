"""Admin password reset (app/auth/reset_password.py) against PostgreSQL + Redis.

The production failure (`FOR UPDATE cannot be applied to the nullable side of an outer join`)
and its fix; a reset that keeps the admin's UUID, role and data, uses Argon2id, revokes every
session and records an audit event; login through the real API with the new password only;
rollback on any failure; refusals; serialized concurrent resets; and the CLI (stdin, exit
codes, no password in any output).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import reset_password as rp
from app.auth.bootstrap import bootstrap_admin
from app.auth.security import hash_password, verify_password
from app.models.auth import AuditEvent, User, UserSession
from app.services import audit

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[1]
EMAIL = "owner@spasta.online"
OLD = "correct horse battery staple"
NEW = "a different, longer passphrase 42"


@pytest.fixture
async def maker(db_clean: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(db_clean)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def admin(maker: async_sessionmaker[AsyncSession]) -> User:
    return await bootstrap_admin(maker, EMAIL, OLD)


async def _sessions(maker: async_sessionmaker[AsyncSession], user_id: Any, n: int) -> None:
    async with maker() as db, db.begin():
        for i in range(n):
            db.add(UserSession(user_id=user_id, token_hash=os.urandom(32),
                               expires_at=datetime.now(UTC) + timedelta(days=1), ip=f"10.0.0.{i}"))  # fmt: skip


async def _row(maker: async_sessionmaker[AsyncSession], email: str = EMAIL) -> User:
    async with maker() as db:
        u = await db.scalar(select(User).where(User.email == email))
        assert u is not None
        return u


async def _open_sessions(maker: async_sessionmaker[AsyncSession], user_id: Any) -> int:
    async with maker() as db:
        return int(await db.scalar(select(func.count()).select_from(UserSession).where(
            UserSession.user_id == user_id, UserSession.revoked_at.is_(None))) or 0)  # fmt: skip


async def _events(maker: async_sessionmaker[AsyncSession], kind: str) -> list[AuditEvent]:
    async with maker() as db:
        return list(await db.scalars(select(AuditEvent).where(AuditEvent.event_type == kind)))


# ---------------- the production error and the fix ----------------
async def test_plain_for_update_fails_on_the_joined_role_and_of_user_works(
    maker: async_sessionmaker[AsyncSession], admin: User
) -> None:
    async with maker() as db, db.begin():
        with pytest.raises(DBAPIError, match="nullable side of an outer join"):
            await db.scalar(select(User).where(User.email == EMAIL).with_for_update())
    async with maker() as db, db.begin():
        sql = str(select(User).with_for_update(of=User).compile(db.bind))
        assert "FOR UPDATE OF users" in sql or "FOR UPDATE OF core.users" in sql
        u = await db.scalar(select(User).where(User.email == EMAIL).with_for_update(of=User))
        assert u is not None and u.role.name == "admin"  # role still loaded


# ---------------- the reset ----------------
async def test_reset_keeps_identity_rehashes_revokes_and_audits(
    maker: async_sessionmaker[AsyncSession], admin: User
) -> None:
    await _sessions(maker, admin.id, 3)
    before = await _row(maker)
    async with maker() as db:
        users_before = await db.scalar(select(func.count()).select_from(User))
    res = await rp.reset_admin_password(maker, " Owner@Spasta.online ", NEW)
    after = await _row(maker)
    assert res.user_id == admin.id and res.email == EMAIL and res.sessions_revoked == 3
    assert (after.id, after.email, after.role_id, after.created_at, after.is_active) == (
        before.id, before.email, before.role_id, before.created_at, before.is_active)  # fmt: skip
    assert (
        after.password_hash.startswith("$argon2id$") and after.password_hash != before.password_hash
    )
    assert verify_password(after.password_hash, NEW) and not verify_password(
        after.password_hash, OLD
    )
    assert await _open_sessions(maker, admin.id) == 0
    ev = await _events(maker, "admin_password_reset")
    assert len(ev) == 1 and ev[0].target_user_id == admin.id and ev[0].actor_user_id is None
    assert ev[0].details == {"sessions_revoked": 3, "method": "cli"}
    async with maker() as db:
        assert (
            await db.scalar(select(func.count()).select_from(User)) == users_before
        )  # no new admin
    dump = repr([(e.details, e.user_agent) for e in ev]) + repr(res)
    assert NEW not in dump and OLD not in dump


async def test_login_after_reset_through_the_api(
    maker: async_sessionmaker[AsyncSession], admin: User, client: AsyncClient
) -> None:
    r = await client.post("/api/auth/login", json={"email": EMAIL, "password": OLD})
    assert r.status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 200
    await rp.reset_admin_password(maker, EMAIL, NEW)
    assert (await client.get("/api/auth/me")).status_code == 401  # the old session is revoked
    client.cookies.clear()
    assert (
        await client.post("/api/auth/login", json={"email": EMAIL, "password": OLD})
    ).status_code == 401
    r = await client.post("/api/auth/login", json={"email": EMAIL, "password": NEW})
    assert r.status_code == 200, r.text
    me = await client.get("/api/auth/me")
    assert (
        me.status_code == 200 and me.json()["id"] == str(admin.id) and me.json()["role"] == "admin"
    )


# ---------------- rollback ----------------
async def test_failure_after_the_hash_rolls_everything_back(
    maker: async_sessionmaker[AsyncSession], admin: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _sessions(maker, admin.id, 2)
    before = (await _row(maker)).password_hash

    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(audit, "record", boom)
    with pytest.raises(RuntimeError, match="audit write failed"):
        await rp.reset_admin_password(maker, EMAIL, NEW)
    assert (await _row(maker)).password_hash == before  # old password still works
    assert await _open_sessions(maker, admin.id) == 2  # sessions untouched
    assert await _events(maker, "admin_password_reset") == []


async def test_refusals_change_nothing(
    maker: async_sessionmaker[AsyncSession], admin: User
) -> None:
    async with maker() as db, db.begin():
        db.add(User(email="user@spasta.online", password_hash=hash_password(OLD), role_id=2))
        db.add(
            User(
                email="off@spasta.online",
                password_hash=hash_password(OLD),
                role_id=1,
                is_active=False,
            )
        )
    before = {
        u: (await _row(maker, u)).password_hash
        for u in (EMAIL, "user@spasta.online", "off@spasta.online")
    }
    with pytest.raises(ValueError, match="at least 12"):
        await rp.reset_admin_password(maker, EMAIL, "short")
    with pytest.raises(ValueError, match="whitespace"):
        await rp.reset_admin_password(maker, EMAIL, " padded passphrase ")
    with pytest.raises(rp.ResetRefused, match="no user"):
        await rp.reset_admin_password(maker, "nobody@spasta.online", NEW)
    with pytest.raises(rp.ResetRefused, match="not an admin"):
        await rp.reset_admin_password(maker, "user@spasta.online", NEW)
    with pytest.raises(rp.ResetRefused, match="deactivated"):
        await rp.reset_admin_password(maker, "off@spasta.online", NEW)
    for u, h in before.items():
        assert (await _row(maker, u)).password_hash == h
    assert await _events(maker, "admin_password_reset") == []


async def test_concurrent_resets_serialize_on_the_row_lock(
    maker: async_sessionmaker[AsyncSession], admin: User
) -> None:
    await _sessions(maker, admin.id, 2)
    a, b = NEW, "yet another passphrase 77"
    res = await asyncio.gather(
        rp.reset_admin_password(maker, EMAIL, a), rp.reset_admin_password(maker, EMAIL, b)
    )
    assert sorted(r.sessions_revoked for r in res) == [0, 2]  # the second waited for the first
    h = (await _row(maker)).password_hash
    assert verify_password(h, a) != verify_password(h, b)  # exactly one of them is final
    assert len(await _events(maker, "admin_password_reset")) == 2


# ---------------- the CLI ----------------
def _cli(db_url: str, email: str, stdin: str, *extra: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": db_url, "SECRET_KEY": "t" * 40, "ENV": "test"}
    return subprocess.run(
        [sys.executable, "-m", "app.auth.reset_password", "--email", email, *extra],
        input=stdin, capture_output=True, text=True, cwd=ROOT, env=env, timeout=120, check=False,
    )  # fmt: skip


async def test_cli_resets_with_stdin_and_never_prints_the_password(
    maker: async_sessionmaker[AsyncSession], admin: User, db_clean: str
) -> None:
    p = await asyncio.to_thread(_cli, db_clean, EMAIL, NEW + "\n", "--stdin")
    assert p.returncode == 0, p.stderr
    assert f"password reset for admin {EMAIL} ({admin.id})" in p.stdout
    assert NEW not in p.stdout + p.stderr
    assert verify_password((await _row(maker)).password_hash, NEW)
    bad = await asyncio.to_thread(_cli, db_clean, "nobody@spasta.online", NEW + "\n", "--stdin")
    assert (
        bad.returncode == 1
        and "nothing was changed" in bad.stderr
        and NEW not in bad.stdout + bad.stderr
    )
    short = await asyncio.to_thread(_cli, db_clean, EMAIL, "short\n", "--stdin")
    assert short.returncode == 1 and "at least 12" in short.stderr
    # without --stdin and without a terminal it refuses rather than reading a piped secret silently
    notty = await asyncio.to_thread(_cli, db_clean, EMAIL, NEW + "\n")
    assert notty.returncode != 0 and "no terminal" in (notty.stderr + notty.stdout)
    assert verify_password((await _row(maker)).password_hash, NEW)


async def test_reset_is_not_wired_into_deploys() -> None:
    """Deploy/migration code never calls the reset (it is an operator action only)."""
    for f in [
        *(ROOT / "scripts").rglob("*.sh"),
        ROOT / "app" / "workers" / "migrate.py",
        ROOT / "docker-compose.yml",
    ]:
        if f.exists():
            assert "reset_password" not in f.read_text(), f
