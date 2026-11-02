"""GET /api/v1/approvals/stream: Server-Sent Events of approval events for the inbox
(docs/23-approval-engine.md). Same pattern as the trading stream (app/services/
trading_stream.py):

- `hello` once, then one `approval` event per audit.approval_events row with the request's
  current state (kind, target, status, approvals received/required, risk, summary). Never the
  payload or the artifacts.
- every data event carries `id:` = the approval event id. A reconnecting EventSource sends
  it back as `Last-Event-ID` (or `?last_event_id=` when headers cannot be set) and the stream
  resumes right after it. Rows are emitted LAG after they were written, so a transaction
  that commits a little late is not skipped.
- `: ping` heartbeats while idle, `bye` at the max duration or when the session ends.

Read-only, no DB connection held between polls, and the login session (and admin role) is
re-checked every poll.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.approvals import ApprovalEvent, ApprovalRequest
from app.models.auth import ROLE_ADMIN, Role, User, UserSession
from app.services.trading_stream import PING, sse

LAG = timedelta(seconds=1)
BATCH = 200


def parse_cursor(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        v = int(raw.strip())
    except ValueError:
        return None
    return v if v >= 0 else None


async def _session_ok(db: AsyncSession, session_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    s = await db.get(UserSession, session_id)
    if not (s and s.revoked_at is None and s.expires_at > datetime.now(UTC)):
        return False
    role = await db.scalar(
        select(Role.name)
        .join(User, User.role_id == Role.id)
        .where(User.id == user_id, User.is_active.is_(True))
    )
    return role == ROLE_ADMIN


async def poll(db: AsyncSession, cursor: int) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    rows = (
        await db.execute(
            select(ApprovalEvent, ApprovalRequest)
            .join(ApprovalRequest, ApprovalRequest.id == ApprovalEvent.request_id)
            .where(ApprovalEvent.id > cursor, ApprovalEvent.at <= datetime.now(UTC) - LAG)
            .order_by(ApprovalEvent.id)
            .limit(BATCH)
        )
    ).all()
    out: list[tuple[str, dict[str, Any]]] = []
    for ev, req in rows:
        cursor = max(cursor, ev.id)
        out.append(
            (
                "approval",
                {
                    "event_id": ev.id,
                    "event": ev.event,
                    "at": ev.at,
                    "actor": ev.actor,
                    "approval_id": req.id,
                    "kind": req.kind,
                    "target": req.target,
                    "status": req.status,
                    "required_approvals": req.required_approvals,
                    "received_approvals": req.received_approvals,
                    "risk_level": req.risk_level,
                    "summary": req.summary,
                    "expires_at": req.expires_at,
                },
            )
        )
    return out, cursor


async def events(
    maker: async_sessionmaker[AsyncSession],
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    last_event_id: str | None,
    poll_seconds: float,
    heartbeat_seconds: float,
    max_seconds: float,
) -> AsyncIterator[str]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    cursor = parse_cursor(last_event_id)
    if cursor is None:
        async with maker() as db:
            cursor = int(await db.scalar(select(func.max(ApprovalEvent.id))) or 0)
    yield "retry: 3000\n\n"
    yield sse("hello", {"server_time": datetime.now(UTC), "poll_seconds": poll_seconds})
    last_sent = loop.time()
    while True:
        async with maker() as db:
            if not await _session_ok(db, session_id, user_id):
                yield sse("bye", {"reason": "session_ended"})
                return
            batch, cursor = await poll(db, cursor)
        for name, data in batch:
            yield sse(name, data, str(data["event_id"]))
        if batch:
            last_sent = loop.time()
        elif loop.time() - last_sent >= heartbeat_seconds:
            yield PING
            last_sent = loop.time()
        if loop.time() - started >= max_seconds:
            yield sse("bye", {"reason": "max_duration"}, str(cursor))
            return
        await asyncio.sleep(poll_seconds)
