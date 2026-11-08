"""Server side of V1.6.3 aureon/risk.py: the broker day and the admin lock clear.

The risk ENGINE (baseline, drawdown, LOCKED/WARNING) runs in the guard on the Windows box and
writes its state through POST /api/v1/ingest/risk; it stays there. The guard cannot unlock a
LOCKED day (409); only an admin can, here, audited in aureon.risk_events (and the
Assembler's audit log by the route).
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.aureon import config, db


def broker_day(t: dt.datetime | None = None) -> dt.date:
    t = (t or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).replace(tzinfo=None)
    h, m = map(int, config.SESSION_FIRST_BAR_UTC.split(":"))
    return (t - dt.timedelta(hours=h, minutes=m)).date()


async def admin_clear(db_: AsyncSession, account_id: str, actor: str, reason: str) -> bool:
    """Only valid where the funding provider allows re-entry after a daily lock — Aureon
    cannot know that. Returns False when today's row is not LOCKED."""
    day = broker_day()
    row = await db.risk_get(db_, day, str(account_id))
    if not row or row["state"] != "LOCKED":
        return False
    await db.risk_upsert(
        db_,
        {**row, "state": "UNLOCKED", "cleared_at": dt.datetime.now(dt.UTC),
         "cleared_by": actor, "clear_reason": reason},
    )  # fmt: skip
    await db.risk_event(db_, day, str(account_id), "admin_clear", {"reason": reason}, actor)
    return True
