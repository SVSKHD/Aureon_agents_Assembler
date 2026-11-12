"""The Claude gateway end to end against the test database (docs/26-ai-providers.md):
chat over SSE, conversations, approvals (only the backend executes; paper only), usage and
budgets under concurrency. Claude itself is a scripted fake: no network, no key."""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.ai.budget import BudgetExceeded, PgUsageStore
from app.ai.gateway import ClaudeGateway
from app.models.ai import Approval, Message, ToolCall, Usage
from app.models.auth import User
from app.models.trading import BrokerAccount, Instrument, OrderIntent
from tests.fake_claude import FakeClaude, text_reply, tool_reply
from tests.test_agents import PASSWORD, Env, EnvFactory, make_env  # noqa: F401

pytestmark = pytest.mark.db

KEY = "sk-ant-api03-" + "d" * 40


def _install(env: Env, replies: list[Any]) -> FakeClaude:
    """Swap a scripted Claude into the running app, keyed so the routes accept it."""
    fake = FakeClaude(replies)
    s = env.app.state.settings.model_copy(update={"ANTHROPIC_API_KEY": SecretStr(KEY)})
    env.app.state.settings = s
    env.app.state.ai = ClaudeGateway(s, PgUsageStore(env.maker), client=fake)
    return fake


def _events(body: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in body.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


async def _seed_trading(env: Env) -> int:
    async with env.maker() as db, db.begin():
        uid = await db.scalar(select(User.id).where(User.email == "admin@spasta.online"))
        inst = Instrument(
            broker="dhan", broker_instrument_id="NSE_EQ:2885", symbol="RELIANCE",
            trading_symbol="RELIANCE", segment="NSE_EQ", instrument_type="EQUITY",
            currency="INR", lot_size=Decimal(1), tick_size=Decimal("0.05"),
        )  # fmt: skip
        db.add_all(
            [
                inst,
                BrokerAccount(
                    broker="dhan",
                    account_id="1100099999",
                    base_currency="INR",
                    owner_user_id=uid,
                    mode="PAPER",
                ),
            ]
        )
        await db.flush()
        return int(inst.id)


async def test_chat_streams_saves_and_replays(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env() as env:
        fake = _install(
            env,
            [
                tool_reply([("get_portfolio_summary", {})]),
                text_reply("No accounts yet."),
                text_reply("Still none."),
            ],
        )
        r = await env.post("admin", "/api/ai/chat", json={"message": "how am I doing?",
                                                          "agent": "investments"})  # fmt: skip
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        ev = _events(r.text)
        kinds = [k for k, _ in ev]
        assert kinds[0] == "meta" and kinds[-1] == "done"
        assert ("tool", {"name": "get_portfolio_summary", "status": "ok"}) in ev
        # The answer streams as text events, and comes once more whole in `done`.
        assert "".join(d["delta"] for k, d in ev if k == "text") == "No accounts yet."
        done = ev[-1][1]
        assert done["text"] == "No accounts yet."
        conv = done["conversation_id"]
        assert Decimal(done["cost_usd"]) > 0 and done["steps"] == 2

        detail = (await env.admin.get(f"/api/ai/conversations/{conv}")).json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
        assert (
            detail["messages"][1]["content"] == "No accounts yet."
            and detail["agent"] == "investments"
        )
        # Another user cannot read it.
        assert (await env.user.get(f"/api/ai/conversations/{conv}")).status_code == 404

        # The next turn replays the stored blocks exactly (tool_use + tool_result included).
        r2 = await env.post("admin", "/api/ai/chat", json={"conversation_id": conv,
                                                           "message": "and now?"})  # fmt: skip
        assert _events(r2.text)[-1][0] == "done"
        replay = fake.calls[-1]["messages"]
        assert [m["role"] for m in replay] == ["user", "assistant", "user", "assistant", "user"]
        assert replay[1]["content"][0]["type"] == "tool_use"

        assert await env.scalar(select(func.count()).select_from(ToolCall)) == 1
        rows = (await env.scalar(select(func.count()).select_from(Usage).where(
            Usage.outcome == "ok")))  # fmt: skip
        assert rows == 3
        msgs = await env.scalar(select(func.count()).select_from(Message))
        assert (
            msgs == 6
        )  # 2 turns: user, assistant(tool_use), user(tool_result), assistant; user, assistant
        listing = (await env.admin.get("/api/ai/conversations")).json()
        assert listing["total"] == 1


async def test_chat_needs_csrf_and_a_key(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env() as env:
        body = {"message": "hi", "agent": "general"}
        assert (await env.admin.post("/api/ai/chat", json=body)).status_code == 403  # no CSRF
        r = await env.post("admin", "/api/ai/chat", json=body)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "ai_not_configured"
        _install(env, [])
        missing = {"message": "hi", "conversation_id": str(uuid.uuid4())}
        assert (await env.post("admin", "/api/ai/chat", json=missing)).status_code == 404


async def test_budget_refusal_is_an_sse_error(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env() as env:
        fake = _install(env, [text_reply("never")])
        env.app.state.ai.settings = env.app.state.ai.settings.model_copy(
            update={"AI_AGENT_BUDGETS_USD": {"chat": Decimal("0.0001")}}
        )
        r = await env.post("admin", "/api/ai/chat", json={"message": "hi", "agent": "general"})
        kind, data = _events(r.text)[-1]
        assert kind == "error" and data["code"] == "budget_exceeded"
        assert fake.calls == []


async def test_proposal_waits_for_approval_then_becomes_a_paper_intent(
    make_env: EnvFactory,  # noqa: F811
) -> None:
    async with make_env() as env:
        await _seed_trading(env)
        args = {"symbol": "RELIANCE", "side": "BUY", "qty": 5, "entry": 2850, "stop": 2830,
                "target": 2900, "reason": "test"}  # fmt: skip
        _install(env, [tool_reply([("propose_trade", args)]), text_reply("Filed.")])
        r = await env.post("admin", "/api/ai/chat", json={"message": "buy", "agent": "investments"})
        approvals = [d for k, d in _events(r.text) if k == "approval"]
        assert len(approvals) == 1
        aid = approvals[0]["id"]
        # Nothing executed yet.
        assert await env.scalar(select(func.count()).select_from(OrderIntent)) == 0
        pending = (await env.admin.get("/api/ai/approvals")).json()
        assert [a["id"] for a in pending] == [aid] and pending[0]["status"] == "pending"
        assert (await env.user.get("/api/ai/approvals")).status_code == 403

        url = f"/api/ai/approvals/{aid}"
        assert (await env.post("admin", url, json={"action": "approve"})).status_code == 403
        bad = await env.post("admin", url, json={"action": "approve", "password": "wrong"})
        assert bad.status_code == 403
        ok = await env.post("admin", url, json={"action": "approve", "password": PASSWORD})
        assert ok.status_code == 200, ok.text
        row = ok.json()
        assert row["status"] == "executed", row
        intent = await env.scalar(
            select(OrderIntent).where(OrderIntent.correlation_id == f"ai-approval-{aid}")
        )
        assert intent is not None and intent.mode == "PAPER"
        # Single use.
        again = await env.post("admin", url, json={"action": "approve", "password": PASSWORD})
        assert again.status_code == 409


async def test_risk_limits_stop_a_proposal_before_any_approval(
    make_env: EnvFactory,  # noqa: F811
) -> None:
    async with make_env() as env:
        await _seed_trading(env)
        big = {"symbol": "RELIANCE", "side": "BUY", "qty": 500, "entry": 2850, "stop": 2700,
               "target": 3000, "reason": "too big"}  # fmt: skip
        fake = _install(env, [tool_reply([("propose_trade", big)]), text_reply("Rejected.")])
        await env.post("admin", "/api/ai/chat", json={"message": "buy", "agent": "investments"})
        result = fake.calls[1]["messages"][-1]["content"][0]
        assert result["is_error"] and "max loss per trade" in result["content"]
        assert await env.scalar(select(func.count()).select_from(Approval)) == 0


async def test_reject_and_usage(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env() as env:
        _install(env, [text_reply("hello")])
        await env.post("admin", "/api/ai/chat", json={"message": "hi", "agent": "general"})
        usage = (await env.admin.get("/api/ai/usage")).json()
        assert usage["configured"] is True and usage["calls"] == 1
        chat = next(b for b in usage["budgets"] if b["budget"] == "chat")
        assert Decimal(chat["spent_usd"]) > 0 and Decimal(chat["limit_usd"]) == 20
        assert Decimal(usage["limit_usd"]) == 100
        assert (await env.user.get("/api/ai/usage")).status_code == 403


async def test_pg_reservations_never_overspend(make_env: EnvFactory) -> None:  # noqa: F811
    async with make_env() as env:
        s = env.settings.model_copy(update={"AI_AGENT_BUDGETS_USD": {"chat": Decimal("1")}})
        store = PgUsageStore(env.maker)

        async def one() -> bool:
            try:
                await store.reserve(s, agent="general", budget="chat", model="m",
                                    estimate=Decimal("0.30"), conversation_id=None)  # fmt: skip
                return True
            except BudgetExceeded:
                return False

        results = await asyncio.gather(*(one() for _ in range(8)))
        assert results.count(True) == 3
        assert (await store.spend()).by_budget["chat"] == Decimal("0.90")


async def test_attachments_upload_send_and_stay_private(make_env: EnvFactory) -> None:  # noqa: F811
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    async with make_env() as env:
        fake = _install(env, [text_reply("A blank image.")])
        upload = {"file": ("chart.png", png, "application/octet-stream")}
        assert (await env.user.post("/api/ai/attachments", files=upload)).status_code == 403
        r = await env.post("user", "/api/ai/attachments", files=upload)
        assert r.status_code == 201, r.text
        att = r.json()
        assert (att["kind"], att["media_type"], att["name"]) == ("image", "image/png", "chart.png")
        bad = {"file": ("x.exe", b"MZ\x90\x00" * 4, "image/png")}
        assert (await env.post("user", "/api/ai/attachments", files=bad)).status_code == 415

        # Another user cannot send it.
        other = {"message": "what is this?", "agent": "general", "attachments": [att["id"]]}
        r2 = await env.post("admin", "/api/ai/chat", json=other)
        assert r2.status_code == 404
        assert r2.json()["detail"]["code"] == "attachment_not_found"

        r3 = await env.post("user", "/api/ai/chat", json=other)
        assert _events(r3.text)[-1][0] == "done"
        sent = fake.calls[-1]["messages"][-1]["content"]
        assert [b["type"] for b in sent] == ["text", "image"]
        # The stored row keeps a reference, not the file.
        row = await env.scalar(select(Message).where(Message.role == "user"))
        assert row is not None and row.blocks is not None
        assert row.blocks[1]["type"] == "aureon_attachment" and "source" not in row.blocks[1]
