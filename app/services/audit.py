"""Append-only audit log writer."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import AuditEvent


@dataclass(frozen=True, slots=True)
class ClientInfo:
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None

    @classmethod
    def from_request(cls, request: Request) -> ClientInfo:
        ua = request.headers.get("user-agent")
        return cls(
            ip=request.client.host if request.client else None,
            user_agent=ua[:512] if ua else None,
            request_id=structlog.contextvars.get_contextvars().get("request_id"),
        )


def record(
    db: AsyncSession,
    event_type: str,
    client: ClientInfo,
    *,
    actor_user_id: uuid.UUID | None = None,
    target_user_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add an audit row to the current transaction (the caller commits)."""
    db.add(
        AuditEvent(
            event_type=event_type,
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            ip=client.ip,
            user_agent=client.user_agent,
            request_id=client.request_id,
            details=details or {},
        )
    )
