"""Stock Signals notifications (docs/37 §5): who is notified, never twice, preferences,
read state and access control, and the stream's replay from Last-Event-ID."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.deps import CurrentSession
from app.models.auth import User, UserSession
from app.models.stock_signals import StockSignalNotification
from app.services import stock_signal_notifications as notes
from tests.test_market_db import Env, EnvFactory, make_env  # noqa: F401
from tests.test_stock_signals_db import (  # noqa: F401
    DAYS,
    SIGNALS,
    clean_runs,
    ist,
    pending_then_confirmed,
    run_at,
)

pytestmark = pytest.mark.db


async def watch(env: Env, who: str, iid: int) -> None:
    r = await env.send("user" if who == "user" else who, "POST", "/api/trading/watchlists",
                       json={"name": "Mine"})  # fmt: skip
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    r = await env.send(who, "POST", f"/api/trading/watchlists/{wid}/items",
                       json={"instrument_id": iid})  # fmt: skip
    assert r.status_code == 201, r.text


async def mine(env: Env, who: str) -> list[StockSignalNotification]:
    async with env.maker() as db:
        return list(
            (
                await db.scalars(
                    select(StockSignalNotification)
                    .where(StockSignalNotification.user_id == env.ids[who])
                    .order_by(StockSignalNotification.id)
                )
            ).all()
        )


async def test_watchers_and_all_market_users_are_notified_once(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS) as env:
        iid = await env.instrument("NOTIFY")
        await pending_then_confirmed(env, iid)
        await watch(env, "user", iid)
        # admin asks for every crossover in the market, but only confirmed ones.
        r = await env.send("admin", "PUT", "/api/trading/stock-signals/notifications/preferences",
                           json={"all_market": True, "confirmed_only": True})  # fmt: skip
        assert r.status_code == 200 and r.json()["all_market"] is True

        first = await run_at(env, ist(DAYS[3], 10, 0))  # pending bullish cross
        assert await notes.notify_events(env.maker, first.new_events, []) == 1
        assert await notes.notify_events(env.maker, first.new_events, []) == 0  # a retry: none
        got = await mine(env, "user")
        assert [n.kind for n in got] == ["bullish_cross"]
        p = got[0].payload
        assert p["signal_id"] == first.new_events[0] and p["timeframe"] == "15m"
        assert p["instrument"]["symbol"] == "NOTIFY" and p["prices"]["last_price"] == "152"
        assert p["crossed_at"] and p["confirmed_at"] is None
        assert p["deep_link"] == f"/trading/signals/{first.new_events[0]}"
        assert await mine(env, "admin") == []  # confirmed only
        assert await mine(env, "other") == []  # not watching, not all-market

        second = await run_at(env, ist(DAYS[3], 10, 15))  # VWAP confirms it
        assert await notes.notify_events(env.maker, [], second.confirmed_events) == 2
        assert [n.kind for n in await mine(env, "user")] == ["bullish_cross", "vwap_confirmed"]
        assert [n.kind for n in await mine(env, "admin")] == ["vwap_confirmed"]

        # Turned off: nothing more.
        r = await env.send("user", "PUT", "/api/trading/stock-signals/notifications/preferences",
                           json={"enabled": False})  # fmt: skip
        assert r.status_code == 200
        await notes.notify_events(env.maker, [], second.confirmed_events)
        assert len(await mine(env, "user")) == 2


async def test_read_state_and_access(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS) as env:
        iid = await env.instrument("READ")
        await pending_then_confirmed(env, iid)
        await watch(env, "user", iid)
        out = await run_at(env, ist(DAYS[3], 10, 0))
        await notes.notify_events(env.maker, out.new_events, [])
        base = "/api/trading/stock-signals/notifications"

        page = (await env.c["user"].get(base)).json()
        assert page["unread_count"] == 1 and len(page["items"]) == 1
        nid = page["items"][0]["id"]
        assert (await env.c["other"].get(base)).json()["items"] == []  # only your own
        assert (await env.c["anon"].get(base)).status_code == 401

        # Another user's notification is "not found"; no CSRF token is refused.
        assert (await env.send("other", "PATCH", f"{base}/{nid}/read")).status_code == 404
        no_csrf = await env.c["user"].patch(f"{base}/{nid}/read")
        assert no_csrf.status_code == 403
        r = await env.send("user", "PATCH", f"{base}/{nid}/read")
        assert r.status_code == 200 and r.json()["read_at"] is not None
        assert (await env.c["user"].get(base)).json()["unread_count"] == 0
        assert (await env.c["user"].get(base, params={"unread_only": True})).json()["items"] == []

        # Polling fallback: after_id returns newer ones, oldest first.
        poll = (await env.c["user"].get(base, params={"after_id": 0})).json()
        assert [i["id"] for i in poll["items"]] == [nid] and poll["last_id"] == nid
        r = await env.send("user", "POST", f"{base}/read-all", json={})
        assert r.status_code == 200 and r.json() == {"unread_count": 0}


async def test_stream_replays_from_the_last_event_id(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env(**SIGNALS) as env:
        iid = await env.instrument("STREAM")
        await pending_then_confirmed(env, iid)
        await watch(env, "user", iid)
        first = await run_at(env, ist(DAYS[3], 10, 0))
        await notes.notify_events(env.maker, first.new_events, [])
        second = await run_at(env, ist(DAYS[3], 10, 15))
        await notes.notify_events(env.maker, [], second.confirmed_events)
        ids = [n.id for n in await mine(env, "user")]
        async with env.maker() as db:
            user = await db.get(User, env.ids["user"])
            sess = await db.scalar(select(UserSession).where(UserSession.user_id == user.id))  # type: ignore[union-attr]
        assert user is not None and sess is not None
        current = CurrentSession(user=user, session=sess, token="")

        async def events(last: str | None) -> list[tuple[str, dict[str, Any], str | None]]:
            out = []
            gen = notes.stream(
                env.maker,
                current,
                last_event_id=last,
                poll_seconds=0.01,
                heartbeat_seconds=60,
                max_seconds=0.2,
            )
            async for chunk in gen:
                if not chunk.startswith(("id:", "event:")):
                    continue
                lines = dict(ln.split(": ", 1) for ln in chunk.strip().split("\n"))
                out.append((lines["event"], json.loads(lines["data"]), lines.get("id")))
            return out

        # Reconnecting after the first notification replays only the second.
        got = await events(str(ids[0]))
        names = [e for e, _, _ in got]
        assert names[0] == "hello" and names[-1] == "bye"
        sent = [(d["id"], d["kind"], i) for e, d, i in got if e == "notification"]
        assert sent == [(ids[1], "vwap_confirmed", str(ids[1]))]
        # No cursor: starts after the newest (history comes from GET /notifications).
        assert [e for e, _, _ in await events(None) if e == "notification"] == []
