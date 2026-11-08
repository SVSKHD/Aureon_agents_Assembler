"""V1.6.3 aureon/scheduler.py on the Assembler: session state, backfill, watchdog.

Operator intent lives in control.desired (RUN|PAUSE); heartbeats are status only. States:
LIVE / PAUSED / RESUMING / DEGRADED / FAILED. The Assembler never touches MT5: it requests a
collector job (`backfill`, `backfill_range`) and waits for the Windows collector's durable
ack (the job row), never for elapsed time.

Differences from V1.6.3, all about running inside a scheduled task instead of a loop:
- one `tick()` per minute; the state, the resume job and the failure count are stored in
  PostgreSQL (service_heartbeats / control), so a restart resumes where it was;
- in LIVE, gaps in the last 3 days of M30 bars become a `backfill_range` job (one at a time),
  and the window is replayed once the collector has uploaded it;
- watchdog alerts carry an hourly key, so a stale service alerts once per hour, not per minute.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.aureon import alerter, config, db, detector

SERVICES = ("collector", "detector", "alerter", "guard")
STEP = {"M30": 30, "H1": 60}
PENDING_KEY = "scheduler_pending_job"
FAILS_KEY = "scheduler_fails"
GAPFILL_KEY = "scheduler_gapfill_job"


def naive(x: Any) -> dt.datetime:
    """datetime / ISO string -> naive UTC (naive input already means UTC)."""
    t: dt.datetime = dt.datetime.fromisoformat(x) if isinstance(x, str) else x
    return t.astimezone(dt.UTC).replace(tzinfo=None) if t.tzinfo is not None else t


def _hm(s: str) -> tuple[int, int]:
    h, m = map(int, s.split(":"))
    return h, m


def expected_bars(
    start: dt.datetime, end: dt.datetime, tf: str, holidays: Any = ()
) -> list[dt.datetime]:
    """Every bar open that SHOULD exist between start and end: Mon–Fri,
    SESSION_FIRST_BAR_UTC..SESSION_LAST_BAR_UTC, minus holidays (V1.6.3, verbatim logic)."""
    step = STEP[tf]
    fh, fm = _hm(config.SESSION_FIRST_BAR_UTC)
    lh, lm = _hm(config.SESSION_LAST_BAR_UTC)
    last_h, last_m = (lh, lm) if tf == "M30" else (lh, 0)
    out = []
    day = start.date()
    while day <= end.date():
        if day.weekday() < 5 and day not in holidays:
            t = dt.datetime.combine(day, dt.time(fh, fm))
            stop = dt.datetime.combine(day, dt.time(last_h, last_m))
            while t <= stop:
                if start <= t <= end:
                    out.append(t)
                t += dt.timedelta(minutes=step)
        day += dt.timedelta(days=1)
    return out


def expected_gap(prev: dt.datetime, cur: dt.datetime, step: int) -> bool:
    """True if no expected bar lies strictly between prev and cur."""
    return not [
        t for t in expected_bars(prev, cur, "M30" if step == 30 else "H1") if prev < t < cur
    ]


async def is_holiday(s: AsyncSession, t: dt.datetime) -> bool:
    return bool(
        await db.q(
            s,
            """select 1 from aureon.market_calendar where kind='holiday'
               and start_utc<=:t and end_utc>:t""",
            {"t": db.utc(t)},
            one=True,
        )
    )


async def in_session(s: AsyncSession, t: dt.datetime) -> bool:
    t = naive(t)
    wd, hm = t.weekday(), t.strftime("%H:%M")
    if wd == 5:
        return False
    if wd == 6:
        return hm >= "23:00"
    if wd == 4 and hm >= "22:00":
        return False
    return not await is_holiday(s, t)


async def holidays_since(s: AsyncSession, start: dt.datetime) -> set[dt.date]:
    rows = await db.q(
        s,
        "select start_utc from aureon.market_calendar where kind='holiday' and start_utc>=:s",
        {"s": db.utc(start)},
    )
    return {naive(r["start_utc"]).date() for r in rows}


async def gaps(
    s: AsyncSession, tf: str = "M30", days: int = 3, now: dt.datetime | None = None
) -> list[dt.datetime]:
    end = naive(now or dt.datetime.now(dt.UTC)) - dt.timedelta(minutes=STEP[tf] * 2)
    start = end - dt.timedelta(days=days)
    hol = await holidays_since(s, start - dt.timedelta(days=1))
    have = {
        naive(r["time_utc"])
        for r in await db.q(
            s,
            """select time_utc from aureon.bars where symbol=:sym and tf=:tf
               and time_utc between :a and :b""",
            {"sym": config.SYMBOL, "tf": tf, "a": db.utc(start), "b": db.utc(end)},
        )
    }
    return [t for t in expected_bars(start, end, tf, hol) if t not in have]


async def collector_fresh(s: AsyncSession, t: dt.datetime, max_age: int = 180) -> bool:
    r = await db.q(
        s,
        "select last_beat from aureon.service_heartbeats where service='collector'",
        one=True,
    )
    return bool(r and r["last_beat"]) and (db.utc(t) - r["last_beat"]).total_seconds() <= max_age


async def resume(s: AsyncSession, t: dt.datetime) -> str | None:
    """Durable ack: the backfill job row is the evidence. One outstanding job at a time."""
    if not await collector_fresh(s, t):
        return "collector (MT5 box) not heartbeating — cannot backfill"
    pending = await db.get_control(s, PENDING_KEY, "")
    if not pending:
        jid = await db.request_job(s, "backfill", {"n": 2000})
        await db.set_control(s, PENDING_KEY, str(jid), "scheduler")
        return "backfill requested, waiting for collector ack"
    j = await db.job_status(s, int(pending))
    if not j or j["state"] in ("pending", "running"):
        return f"backfill job {pending} {j['state'] if j else '?'} — waiting"
    await db.set_control(s, PENDING_KEY, "", "scheduler")
    if j["state"] != "done":
        return f"backfill job {j['id']} {j['state']}: {j['result']}"
    g = await gaps(s, "M30", 3, now=t)
    if g:
        return f"gaps remain after backfill: {[f'{x:%d %b %H:%M}' for x in g[:5]]}"
    await detector.replay(s, naive(t) - dt.timedelta(days=3), naive(t))
    return None


async def watchdog(s: AsyncSession, t: dt.datetime) -> list[str]:
    stale = []
    for svc in SERVICES:
        r = await db.q(
            s,
            "select last_beat from aureon.service_heartbeats where service=:s",
            {"s": svc},
            one=True,
        )
        age = None if not r or not r["last_beat"] else (db.utc(t) - r["last_beat"]).total_seconds()
        if age is None or age > config.HEARTBEAT_SEC * config.HEARTBEAT_MISS_LIMIT:
            where = "(MT5 box)" if svc in ("collector", "guard") else ""
            await alerter.post(
                s,
                f"⚠️ {svc} {where}: no heartbeat for {int(age) if age else '?'}s",
                key=f"watchdog:{svc}:{naive(t):%Y%m%d%H}",
            )
            stale.append(svc)
    return stale


async def gapfill(s: AsyncSession, t: dt.datetime) -> str | None:
    """LIVE: missing M30 bars in the last 3 days -> one `backfill_range` job; when the collector
    has answered, replay the window so the indicators include the late bars."""
    pending = await db.get_control(s, GAPFILL_KEY, "")
    if pending:
        j = await db.job_status(s, int(pending))
        if j and j["state"] in ("pending", "running"):
            return f"gap backfill job {pending} {j['state']}"
        await db.set_control(s, GAPFILL_KEY, "", "scheduler")
        if j and j["state"] == "done":
            p = j["payload"] or {}
            await detector.replay(s, naive(p["start"]), naive(t))
            return f"gap backfill job {pending} done; replayed from {p['start']}"
        return f"gap backfill job {pending} {j['state'] if j else 'missing'}"
    g = await gaps(s, "M30", 3, now=t)
    if not g:
        return None
    payload = {
        "start": g[0].isoformat(),
        "end": (g[-1] + dt.timedelta(minutes=30)).isoformat(),
        "tfs": ["M30", "H1"],
    }
    if await db.open_job(s, "backfill_range", payload):
        return "gap backfill already requested"
    jid = await db.request_job(s, "backfill_range", payload)
    await db.set_control(s, GAPFILL_KEY, str(jid), "scheduler")
    return f"{len(g)} missing M30 bar(s) from {g[0]:%d %b %H:%M}: backfill_range job {jid}"


async def tick(
    maker: async_sessionmaker[AsyncSession], now: dt.datetime | None = None
) -> dict[str, Any]:
    """One scheduler iteration, then the detector (LIVE only) and the signal queue."""
    t = now or dt.datetime.now(dt.UTC)
    out: dict[str, Any] = {}
    async with maker() as s, s.begin():
        await db.ensure_rules_version(s)
        state = await db.get_state(s)
        fails = int(await db.get_control(s, FAILS_KEY, "0") or 0)
        desired = await db.get_control(s, "desired", "RUN")
        note = ""
        if desired == "PAUSE":
            if state != "PAUSED":
                await alerter.post(
                    s, "⏸ Aureon PAUSED by operator", key=f"paused-op:{naive(t):%Y%m%d%H%M}"
                )
            state, note = "PAUSED", "operator"
        elif await in_session(s, t):
            if state in ("PAUSED", "RESUMING", "FAILED"):
                state = "RESUMING"
                try:
                    err = await resume(s, t)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                if err is None:
                    state, fails = "LIVE", 0
                    await alerter.post(s, "▶️ Aureon LIVE", key=f"live:{naive(t):%Y%m%d%H}")
                elif "waiting" in err:
                    note = "backfill+replay"
                else:
                    fails += 1
                    note = "backfill+replay"
                    await alerter.post(
                        s, f"resume attempt {fails}: {err}", key=f"resume:{naive(t):%Y%m%d%H%M}"
                    )
                    if fails >= 10:
                        state = "FAILED"
                        await alerter.post(
                            s,
                            "🚨 resume FAILED 10x — operator action needed; alerts suppressed, guard still runs",
                            key=f"resumefail:{naive(t):%Y%m%d}",
                        )
                out["resume"] = err
            elif state == "LIVE":
                if not await collector_fresh(s, t):
                    state = "DEGRADED"
                    await alerter.post(
                        s,
                        "⚠️ DEGRADED: collector (MT5 box) stopped heartbeating — no new signals until it returns",
                        key=f"degraded:{naive(t):%Y%m%d%H}",
                    )
                else:
                    out["gapfill"] = await gapfill(s, t)
                out["stale"] = await watchdog(s, t)
            elif state == "DEGRADED":
                if await collector_fresh(s, t):
                    state = "RESUMING"  # re-backfill what was missed, then LIVE
        else:
            if state != "PAUSED":
                state = "PAUSED"
                await alerter.post(
                    s,
                    "⏸ Aureon PAUSED (weekend / out of session)",
                    key=f"paused:{naive(t):%Y%m%d}",
                )
        await db.set_control(s, FAILS_KEY, str(fails), "scheduler")
        await db.heartbeat(s, "scheduler", state, note)
        out["state"] = state
    # detector and the signal queue act only in LIVE (DEGRADED/FAILED stop new signals)
    async with maker() as s, s.begin():
        if state == "LIVE":
            out["new_signals"] = await detector.live_once(s)
        await db.heartbeat(s, "detector", state)
    async with maker() as s, s.begin():
        if state == "LIVE":
            out["queued"] = await alerter.queue_signals(s)
            await alerter.auto_skip(s)
    return out
