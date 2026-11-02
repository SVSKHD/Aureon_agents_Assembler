"""One-time admin bootstrap.

    python -m app.auth.bootstrap --email you@example.com   # password read from stdin

Refuses to run if any admin already exists.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import MIN_PASSWORD_LENGTH, hash_password
from app.auth.service import normalize_email
from app.models.auth import ROLE_ADMIN, Role, User
from app.services import audit
from app.services.audit import ClientInfo


class AdminExistsError(RuntimeError):
    pass


async def bootstrap_admin(
    maker: async_sessionmaker[AsyncSession], email: str, password: str
) -> User:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    email = normalize_email(email)
    if "@" not in email:
        raise ValueError("invalid email")
    async with maker() as db, db.begin():
        # Serialize concurrent bootstrap runs.
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('aureon.bootstrap_admin'))"))
        admin_role = await db.scalar(select(Role).where(Role.name == ROLE_ADMIN))
        if admin_role is None:
            raise RuntimeError("roles not seeded; run `alembic upgrade head` first")
        exists = await db.scalar(select(User.id).where(User.role_id == admin_role.id).limit(1))
        if exists is not None:
            raise AdminExistsError("an admin already exists; refusing to create another")
        user = User(email=email, password_hash=hash_password(password), role_id=admin_role.id)
        db.add(user)
        await db.flush()
        audit.record(
            db,
            "admin_bootstrapped",
            ClientInfo(user_agent="cli"),
            target_user_id=user.id,
        )
    return user


def _read_password() -> str:
    if sys.stdin.isatty():
        pw = getpass.getpass("Admin password: ")
        if pw != getpass.getpass("Repeat password: "):
            raise SystemExit("passwords do not match")
        return pw
    return sys.stdin.readline().rstrip("\n")


async def _main(email: str) -> int:
    from app.core.config import get_settings
    from app.db.session import build_engine, build_sessionmaker

    password = _read_password()
    engine = build_engine(get_settings())
    try:
        user = await bootstrap_admin(build_sessionmaker(engine), email, password)
    except (AdminExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"created admin {user.email} ({user.id})")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    raise SystemExit(asyncio.run(_main(parser.parse_args().email)))
