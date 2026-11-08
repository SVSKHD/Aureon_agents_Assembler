"""V1.6.3 aureon/alerter.py on the Assembler: the durable alert queue and its delivery.

Anything to send is ENQUEUED in aureon.alerts (idempotent by key: the guard on the Windows box
enqueues through POST /api/v1/ingest/alert), and the delivery task moves
PENDING -> SENDING -> DELIVERED | FAILED with bounded retries, Retry-After and jitter, to the
Assembler's existing Discord webhook (DISCORD_WEBHOOK_URL). Without a webhook the queue keeps
the alerts PENDING (nothing is dropped) until one is configured.

Signals: live tradeable signals are queued once (sent_at), and suppressed while today's risk
state is LOCKED/UNKNOWN (fail closed); a signal with no decision after 35 minutes is tagged
`auto_skip`, as in V1.6.3. Decisions are recorded through POST /api/v1/decisions (the V1.6.3
Discord bot's commands are not part of the Assembler).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.aureon import config, db
from app.core.config import Settings


def _session(h: int) -> str:
    return "Asia" if h < 7 else "London" if h < 13 else "NY" if h < 21 else "Roll"


def broker_day() -> str:
    h, m = map(int, config.SESSION_FIRST_BAR_UTC.split(":"))
    t = dt.datetime.now(dt.UTC).replace(tzinfo=None) - dt.timedelta(hours=h, minutes=m)
    return t.date().isoformat()


async def enqueue(s: AsyncSession, key: str, kind: str, text: str) -> None:
    await db.alert_enqueue(s, key[:120], kind[:30], text[:1900])


async def post(s: AsyncSession, text: str, key: str | None = None) -> None:
    """Enqueue with a generated key when none is given (never a direct, unacknowledged send)."""
    digest = int(hashlib.sha256(text.encode()).hexdigest(), 16) % 10**6
    k = key or f"adhoc:{dt.datetime.now(dt.UTC).isoformat(timespec='seconds')}:{digest}"
    await enqueue(s, k, "adhoc", text)


# ---------------- delivery ----------------
async def send(
    http: httpx.AsyncClient, url: str | None, text: str
) -> tuple[bool, float | None, str | None]:
    """One HTTP attempt. Returns (ok, retry_after_seconds | None, error)."""
    if not url:
        return False, 60.0, "DISCORD_WEBHOOK_URL not set"
    try:
        r = await http.post(url, json={"content": text[:1950]}, timeout=10)
    except httpx.TimeoutException:
        return False, 5.0, "timeout"
    except httpx.HTTPError as e:
        return False, 5.0, f"network: {type(e).__name__}"
    if 200 <= r.status_code < 300:
        return True, None, None
    if r.status_code == 429 or r.status_code >= 500:
        try:
            ra = float(r.headers.get("Retry-After") or r.json().get("retry_after", 2))
        except Exception:
            ra = 2.0
        return False, ra, f"HTTP {r.status_code}"
    return False, None, f"HTTP {r.status_code}"  # 4xx: permanent


async def deliver_pending(
    maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    http: httpx.AsyncClient,
    max_n: int = 20,
) -> dict[str, int]:
    url = settings.discord_webhook_url
    out = {"delivered": 0, "retried": 0, "failed": 0}
    for _ in range(max_n):
        async with maker() as s, s.begin():
            a = await db.alert_claim(s)
        if not a:
            break
        ok, ra, err = await send(http, url, a["payload"])
        async with maker() as s, s.begin():
            if ok:
                await db.alert_done(s, a["id"])
                out["delivered"] += 1
            elif url is None:  # nothing to deliver to: keep it pending, do not burn attempts
                await db.q(
                    s,
                    """update aureon.alerts set state='PENDING', attempts=greatest(attempts-1,0),
                       last_error=:e, next_attempt_at=now()+interval '5 minutes' where id=:i""",
                    {"e": err, "i": a["id"]},
                )
                break
            elif ra is None or a["attempts"] >= 6:
                await db.alert_failed(s, a["id"], err or "failed")
                out["failed"] += 1
            else:
                delay = min(300.0, ra * (2 ** (a["attempts"] - 1))) + random.uniform(0, 1)  # noqa: S311 - retry jitter
                await db.alert_retry(s, a["id"], err or "retry", delay)
                out["retried"] += 1
    async with maker() as s, s.begin():
        await db.heartbeat(s, "alerter", await db.get_state(s))
    return out


# ---------------- signals -> alerts ----------------
def format_signal(sig: dict[str, Any]) -> str:
    f = sig["features"]
    d = "BUY" if sig["dir"] > 0 else "SELL"
    sl_px = (
        sig["price"] - sig["suggested_sl"] if sig["dir"] > 0 else sig["price"] + sig["suggested_sl"]
    )
    return (
        f"**#{sig['id']} {d} {config.SYMBOL} M30** @ {sig['price']:.2f}  ·  {sig['time_utc']:%d %b %H:%M} UTC\n"
        f"SL {sl_px:.2f} ({sig['suggested_sl']:.1f} = 1.5 ATR)  ·  exit: opposite VWAP cross / lock ladder\n"
        f"EMA 8/32 {'with' if f['ema_agree'] else 'against'} · 200 {'with' if f['side200'] == sig['dir'] else 'against'} · "
        f"VWAP flips {f['vwap_flips8']}/8 · {_session(f['hour_utc'])} · bar {f['bar_of_day']} of day\n"
        f"Decide with POST /api/v1/decisions (take / skip …)  (no decision within 1 bar = skip)"
    )


async def risk_allows_entries(s: AsyncSession) -> bool:
    r = await db.q(
        s,
        """select state from aureon.risk_state where broker_day=:d
           order by updated_at desc limit 1""",
        {"d": dt.date.fromisoformat(broker_day())},
        one=True,
    )
    return bool(r) and r["state"] in ("UNLOCKED", "WARNING")


async def queue_signals(s: AsyncSession) -> int:
    rows = await db.q(
        s,
        """select * from aureon.signals where source='live' and sent_at is null
           and (features->>'tradeable')='true' and time_utc > now() - interval '2 hours'
           order by time_utc""",
    )
    if not rows:
        return 0
    if not await risk_allows_entries(s):  # fail closed: LOCKED or UNKNOWN -> no entry alerts
        await db.q(
            s,
            "update aureon.signals set sent_at=now() where id = any(:ids)",
            {"ids": [r["id"] for r in rows]},
        )
        await enqueue(
            s,
            f"risk-blocked:{broker_day()}",
            "risk",
            "ℹ️ entry signals suppressed: risk state is not UNLOCKED",
        )
        return 0
    for r in rows:
        await enqueue(s, f"signal:{r['id']}", "signal", format_signal(r))
        await db.q(s, "update aureon.signals set sent_at=now() where id=:i", {"i": r["id"]})
    return len(rows)


async def auto_skip(s: AsyncSession) -> None:
    await db.q(
        s,
        """insert into aureon.decisions(signal_id,tag,via,note)
           select s.id,'auto_skip','system','no reply within 1 bar' from aureon.signals s
           left join aureon.decisions d on d.signal_id=s.id
           where s.source='live' and s.sent_at is not null and d.id is null
           and s.sent_at < now() - interval '35 minutes'""",
    )
