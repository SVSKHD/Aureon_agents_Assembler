"""Phase 20 unit tests (no DB): policy resolution, the hash chain, the Discord adapter, the
voice grammar and the repair-PR policy subject."""

from __future__ import annotations

import ast
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.core.config import Settings
from app.integrations import discord, voice
from app.models.approvals import ApprovalEvent
from app.services.approvals import (
    Policy,
    combined_sha256,
    event_hash,
    pattern_specificity,
    resolve_policy,
    verify_chain,
)
from app.services.github_ops import REPAIR_MERGE_SUBJECT, merge_policy_subject

ROOT = Path(__file__).resolve().parents[1]
URL = "https://discord.test/api/webhooks/1/tok"


def P(pattern: str, pid: int, **kw: Any) -> Policy:
    return Policy(id=pid, kind_pattern=pattern, **kw)


def test_most_specific_policy_wins() -> None:
    pols = [
        P("*", 1),
        P("github.*", 2),
        P("github.pr.merge", 3),
        P("github.branch_protection.*", 4),
        P("repair.*", 5),
    ]
    assert resolve_policy(pols, "github.pr.merge").kind_pattern == "github.pr.merge"  # type: ignore[union-attr]
    assert resolve_policy(pols, "github.branch.delete").kind_pattern == "github.*"  # type: ignore[union-attr]
    assert (
        resolve_policy(pols, "github.branch_protection.update").kind_pattern  # type: ignore[union-attr]
        == "github.branch_protection.*"
    )
    assert resolve_policy(pols, "repair.pr_merge").kind_pattern == "repair.*"  # type: ignore[union-attr]
    assert resolve_policy(pols, "trading.paper_intent").kind_pattern == "*"  # type: ignore[union-attr]
    assert resolve_policy(pols[1:], "trading.paper_intent") is None
    assert pattern_specificity("a.b") > pattern_specificity("a.*") > pattern_specificity("*")


def test_policy_snapshot_round_trip_and_legacy() -> None:
    p = P("x.*", 9, required_approvals=2, notify_channels=("discord",))
    assert Policy.from_snapshot(p.snapshot()) == p
    legacy = Policy.from_snapshot(None)
    assert legacy.required_approvals == 1 and legacy.require_reauth


def test_combined_sha_keeps_the_phase16_value_for_one_artifact() -> None:
    assert combined_sha256({"primary": "abc"}) == "abc"
    assert combined_sha256({}) is None
    assert combined_sha256({"a": "1", "b": "2"}) != combined_sha256({"a": "1", "b": "3"})


def _chain(n: int) -> list[ApprovalEvent]:
    rid = uuid.uuid4()
    prev = None
    out = []
    for i in range(n):
        at = datetime(2026, 11, 2, 10, 0, i, 123456, tzinfo=UTC)
        h = event_hash(prev, rid, f"e{i}", None, {"i": i}, at)
        out.append(
            ApprovalEvent(
                id=i + 1, request_id=rid, event=f"e{i}", actor=None, details={"i": i},
                at=at, prev_hash=prev, hash=h,
            )
        )  # fmt: skip
        prev = h
    return out


def test_hash_chain_verification() -> None:
    evs = _chain(3)
    assert verify_chain(evs, evs[-1].hash).status == "ok"
    evs[1].details = {"i": 99}
    st = verify_chain(evs, evs[-1].hash)
    assert st.status == "broken" and st.broken_at == 2
    evs = _chain(3)
    assert verify_chain(evs[:2], evs[-1].hash).reason == "head hash mismatch"
    assert verify_chain([evs[0], evs[2]], evs[2].hash).reason == "prev_hash mismatch"
    legacy = ApprovalEvent(id=0, request_id=evs[0].request_id, event="old", details={}, hash=None)
    assert verify_chain([legacy, *evs], evs[-1].hash).status == "ok"
    assert verify_chain([legacy], None).status == "legacy"
    assert verify_chain([], None).status == "empty"
    assert verify_chain([*evs, legacy], evs[-1].hash).status == "broken"


# --- Discord ----------------------------------------------------------------------------------


NOTICE = {
    "event": "requested",
    "approval_id": "11111111-1111-1111-1111-111111111111",
    "kind": "github.pr.merge",
    "target": "me/app pr#1 ghp_ABCDEFGHIJKLMNOPQRSTUVWX0123",
    "summary": "@everyone Bearer abcdefghijklmnop",
    "risk_level": "high",
    "required_approvals": 2,
    "received_approvals": 0,
    "payload": {"token": "should-never-appear"},
}


def test_discord_payload_is_an_allowlist_with_a_deep_link() -> None:
    body = discord.build_payload(NOTICE, "https://spasta.online/")
    raw = json.dumps(body)
    assert (
        "should-never-appear" not in raw and "ghp_ABCD" not in raw and "abcdefghijklmnop" not in raw
    )
    assert "@everyone" not in raw and body["allowed_mentions"] == {"parse": []}
    emb = body["embeds"][0]
    assert emb["url"] == "https://spasta.online/dev/approvals/11111111-1111-1111-1111-111111111111"
    assert emb["title"] == "Approval requested: github.pr.merge"
    assert {f["name"] for f in emb["fields"]} == {"Kind", "Risk", "Target", "Approvals"}
    assert "components" not in body


@respx.mock
async def test_discord_post_retries_with_a_cap_and_never_raises() -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, json={"retry_after": 0}),
            httpx.Response(502),
            httpx.Response(204),
        ]
    )
    ok = await discord.post(URL, {"x": 1}, attempts=3, request_seconds=1, retry_base_seconds=0)
    assert ok and route.call_count == 3
    route.side_effect = None
    route.return_value = httpx.Response(500)
    route.reset()
    ok = await discord.post(URL, {"x": 1}, attempts=2, request_seconds=1, retry_base_seconds=0)
    assert not ok and route.call_count == 2
    route.return_value = httpx.Response(404)
    route.reset()
    ok = await discord.post(URL, {"x": 1}, attempts=5, request_seconds=1, retry_base_seconds=0)
    assert not ok and route.call_count == 1  # a 4xx is not retried


async def test_discord_disabled_without_url(settings: Settings) -> None:
    assert settings.discord_webhook_url is None
    discord.notify_background(settings, [NOTICE])  # no-op, no task
    assert await discord.send_notices(settings, [NOTICE]) == []


# --- voice ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("words", "intent", "route"),
    [
        ("open the approvals inbox", "open_approvals_inbox", "/dev/approvals"),
        ("show pending approvals", "open_approvals_inbox", "/dev/approvals"),
        ("Approve request 123", "open_approval", "/dev/approvals/123"),
        ("deny request #abc12", "open_approval", "/dev/approvals/abc12"),
        ("open approval number 7", "open_approval", "/dev/approvals/7"),
        ("open repairs", "open_repairs", "/dev/repairs"),
        ("merge pull request 42", "open_pr", "/dev/prs/42"),
        ("open PR #9", "open_pr", "/dev/prs/9"),
        ("show agents", "show_agents", "/dev/agents"),
        ("what's the weather", "unknown", None),
        ("execute rm -rf /", "unknown", None),
    ],
)
def test_voice_grammar(words: str, intent: str, route: str | None) -> None:
    r = voice.parse(words)
    assert (r.intent, r.route) == (intent, route)


def test_voice_flags_action_words_as_navigation_only() -> None:
    assert voice.parse("approve request 5").note == voice.NAV_ONLY_NOTE
    assert voice.parse("open repairs").note is None


@pytest.mark.parametrize(
    "words",
    [
        "buy 100 shares of reliance",
        "sell everything",
        "place a market order",
        "close all positions",
        "cancel order 5",
        "delete repo",
        "approve all",
        "approve request 5 and merge pr 7 and deploy",
        "create an issue",
        "push to main",
        "trade nifty",
    ],
)
def test_voice_never_does_more_than_navigate(words: str) -> None:
    # Whatever is said, the answer is one of five read-only screens or nothing, and an
    # action word is flagged "navigation only". There is no trading or write intent at all.
    r = voice.parse(words)
    assert r.intent in (*voice.INTENTS, "unknown")
    assert r.route is None or re.fullmatch(
        r"/dev/(approvals(/[0-9a-f-]+)?|repairs|agents|prs/\d+)", r.route
    )
    assert r.note == voice.NAV_ONLY_NOTE


class FakeOllama:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def chat_stream(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any]
    ) -> Any:
        self.calls += 1
        yield {"message": {"content": self.reply}, "done": True}


async def test_voice_ollama_fallback_is_validated() -> None:
    ok = FakeOllama('{"intent": "open_pr", "ref": "17"}')
    r = await voice.resolve("take me to seventeen", ollama=ok, model="m")
    assert (r.intent, r.route, r.source) == ("open_pr", "/dev/prs/17", "ollama")
    # the model cannot invent an intent or a route
    bad = FakeOllama('{"intent": "approve", "ref": "1", "route": "/api/v1/approvals/1/approve"}')
    r = await voice.resolve("do the thing", ollama=bad, model="m")
    assert r.intent == "unknown" and r.route is None
    evil = FakeOllama('{"intent": "open_approval", "ref": "../../api"}')
    assert (await voice.resolve("hmm", ollama=evil, model="m")).route is None
    # the grammar answers first: no model call
    unused = FakeOllama("{}")
    await voice.resolve("show agents", ollama=unused, model="m")
    assert unused.calls == 0


def test_voice_module_cannot_reach_state() -> None:
    for path in (ROOT / "app/integrations/voice.py", ROOT / "app/api/v1/voice.py"):
        tree = ast.parse(path.read_text())
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        forbidden = ("app.services", "app.models", "sqlalchemy", "app.db")
        assert not [m for m in mods if m.startswith(forbidden)], (path, mods)
        src = path.read_text()
        assert "DbSession" not in src and "approve(" not in src


# --- repair PRs -------------------------------------------------------------------------------


def test_repair_prs_resolve_the_repair_policy() -> None:
    kind = "github.pr.merge"
    assert merge_policy_subject(kind, {"head_ref": "repair/abc123"}) == REPAIR_MERGE_SUBJECT
    assert merge_policy_subject(kind, {"head_ref": "x", "labels": ["Repair"]}) == "repair.pr_merge"
    assert merge_policy_subject(kind, {"head_ref": "feature/repair"}) == kind
    assert merge_policy_subject(kind, {}) == kind
