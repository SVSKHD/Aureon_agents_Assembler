"""Authentication, session lifecycle and role changes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import security
from app.models.auth import Role, User, UserSession
from app.services import audit
from app.services.audit import ClientInfo


def normalize_email(email: str) -> str:
    return email.strip().lower()


class AuthService:
    def __init__(self, db: AsyncSession, session_ttl: timedelta) -> None:
        self.db = db
        self.session_ttl = session_ttl

    async def authenticate(self, email: str, password: str) -> User | None:
        user = await self.db.scalar(select(User).where(User.email == normalize_email(email)))
        if user is None:
            security.burn_verify_time(password)
            return None
        if not security.verify_password(user.password_hash, password) or not user.is_active:
            return None
        if security.needs_rehash(user.password_hash):
            user.password_hash = security.hash_password(password)
        return user

    async def create_session(self, user: User, client: ClientInfo) -> str:
        token = security.new_session_token()
        now = datetime.now(UTC)
        self.db.add(
            UserSession(
                user_id=user.id,
                token_hash=security.token_hash(token),
                expires_at=now + self.session_ttl,
                ip=client.ip,
                user_agent=client.user_agent,
            )
        )
        user.last_login_at = now
        return token

    async def resolve(self, token: str) -> tuple[UserSession, User] | None:
        row = (
            await self.db.execute(
                select(UserSession, User)
                .join(User, User.id == UserSession.user_id)
                .where(
                    UserSession.token_hash == security.token_hash(token),
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > func.now(),
                    User.is_active.is_(True),
                )
            )
        ).one_or_none()
        return None if row is None else (row[0], row[1])

    async def revoke(self, session_id: uuid.UUID) -> None:
        await self.db.execute(
            update(UserSession)
            .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )

    async def active_sessions(self, user_id: uuid.UUID) -> list[UserSession]:
        """The user's sessions that are neither revoked nor expired, newest first."""
        rows = await self.db.scalars(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > func.now(),
            )
            .order_by(UserSession.created_at.desc(), UserSession.id)
        )
        return list(rows.all())

    async def revoke_own(self, user_id: uuid.UUID, session_id: uuid.UUID) -> bool:
        """Revoke one of the user's own active sessions. False if it is not theirs, unknown,
        or already revoked / expired (the API answers 404 for all of those)."""
        result = await self.db.execute(
            update(UserSession)
            .where(
                UserSession.id == session_id,
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > func.now(),
            )
            .values(revoked_at=func.now())
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def revoke_all(self, user_id: uuid.UUID) -> int:
        result = await self.db.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def change_role(
        self, actor: User, target_id: uuid.UUID, role_name: str, client: ClientInfo
    ) -> User:
        role = await self.db.scalar(select(Role).where(Role.name == role_name))
        if role is None:
            raise ValueError(f"unknown role {role_name!r}")
        target = await self.db.get(User, target_id)
        if target is None:
            raise LookupError("user not found")
        old = target.role.name
        target.role_id = role.id
        # Role changes end the target's sessions so new permissions apply immediately.
        await self.revoke_all(target.id)
        audit.record(
            self.db,
            "role_changed",
            client,
            actor_user_id=actor.id,
            target_user_id=target.id,
            details={"from": old, "to": role_name},
        )
        return target
