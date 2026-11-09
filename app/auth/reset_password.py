"""Reset an EXISTING admin's password (operator CLI; never run by a deploy).

    python -m app.auth.reset_password --email you@example.com      # prompts twice, no echo
    printf '%s\\n' "$NEW" | python -m app.auth.reset_password --email you@example.com --stdin

In one transaction: lock the admin's row, set a new Argon2id hash (security.hash_password),
revoke every open session of that admin, and write an `admin_password_reset` audit event.
Any failure rolls the whole transaction back: the old password and sessions stay as they
were. The user row is updated in place, so its UUID, email, role, sessions history and every
row that references it are kept. It never creates, deletes or re-roles a user, and refuses a
non-admin or deactivated account. The password is read from the terminal (getpass) or, with
--stdin, from one line of standard input; it is never echoed, logged, stored in plain text or
put in the audit event.

Row lock: `User.role` is `lazy="joined"`, so `select(User).with_for_update()` renders
`LEFT OUTER JOIN core.roles ... FOR UPDATE`, which PostgreSQL rejects ("FOR UPDATE cannot be
applied to the nullable side of an outer join"). The lock is therefore `FOR UPDATE OF users`
(`with_for_update(of=User)`): only the user row is locked, the role is still loaded.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.security import MIN_PASSWORD_LENGTH, hash_password
from app.auth.service import normalize_email
from app.models.auth import ROLE_ADMIN, User, UserSession
from app.services import audit
from app.services.audit import ClientInfo


class ResetRefused(RuntimeError):
    """The account cannot be reset by this command (not found, not admin, inactive)."""


@dataclass(frozen=True)
class ResetResult:
    user_id: uuid.UUID
    email: str
    sessions_revoked: int


def check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if password != password.strip():
        raise ValueError("password must not start or end with whitespace")


async def reset_admin_password(
    maker: async_sessionmaker[AsyncSession], email: str, password: str
) -> ResetResult:
    check_password(password)
    email = normalize_email(email)
    async with maker() as db, db.begin():
        user = await db.scalar(select(User).where(User.email == email).with_for_update(of=User))
        if user is None:
            raise ResetRefused(f"no user with email {email}")
        if user.role.name != ROLE_ADMIN:
            raise ResetRefused(f"{email} is not an admin; this command resets admins only")
        if not user.is_active:
            raise ResetRefused(f"{email} is deactivated; reactivate it deliberately first")
        user.password_hash = hash_password(password)
        result = await db.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        revoked = int(result.rowcount)  # type: ignore[attr-defined]
        audit.record(
            db,
            "admin_password_reset",
            ClientInfo(user_agent="cli:app.auth.reset_password"),
            target_user_id=user.id,
            details={"sessions_revoked": revoked, "method": "cli"},
        )
        await db.flush()
        out = ResetResult(user_id=user.id, email=user.email, sessions_revoked=revoked)
    return out


def read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    if not sys.stdin.isatty():
        raise SystemExit("no terminal: run with `docker compose run --rm -it ...`, or pass --stdin")
    pw = getpass.getpass("New admin password: ")
    if pw != getpass.getpass("Repeat password: "):
        raise SystemExit("passwords do not match")
    return pw


async def _main(email: str, from_stdin: bool) -> int:
    from app.core.config import get_settings
    from app.db.session import build_engine, build_sessionmaker

    password = read_password(from_stdin)
    engine = build_engine(get_settings())
    try:
        res = await reset_admin_password(build_sessionmaker(engine), email, password)
    except (ResetRefused, ValueError) as exc:
        print(f"error: {exc}; nothing was changed", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(
        f"password reset for admin {res.email} ({res.user_id}); "
        f"{res.sessions_revoked} session(s) revoked; audit event admin_password_reset recorded"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--email", required=True, help="the existing admin's email")
    parser.add_argument(
        "--stdin", action="store_true", help="read the password from one stdin line"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.email, args.stdin)))
