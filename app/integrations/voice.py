"""Voice navigation (docs/23-approval-engine.md): a transcript in, a navigation intent out.

Speech-to-text happens on the client (or anywhere else); this module only sees text. A
deterministic grammar maps it to one of five navigation intents:

    open_approvals_inbox   /dev/approvals
    open_approval          /dev/approvals/{ref}
    open_repairs           /dev/repairs
    open_pr                /dev/prs/{number}
    show_agents            /dev/agents

When the grammar finds nothing and a model client is given (the API passes a small Claude
model, AI_MODEL_ROUTER), the model may pick
one of the same intents; its answer is validated against the fixed list and the route is
always built here, never taken from the model.

Navigation only, by construction: this module has no database, approval, GitHub or trading
import and returns a route string. "approve request 123" opens request 123; the approval
itself still needs the admin's re-auth in the app.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Intent = Literal[
    "open_approvals_inbox",
    "open_approval",
    "open_repairs",
    "open_pr",
    "show_agents",
    "unknown",
]
INTENTS: tuple[str, ...] = (
    "open_approvals_inbox",
    "open_approval",
    "open_repairs",
    "open_pr",
    "show_agents",
)
MAX_TRANSCRIPT = 500
ACTION_WORDS = (
    "approve", "deny", "reject", "merge", "execute", "run", "deploy", "dispatch",
    # trading and writes: still only ever navigation (or unknown), never an order or a write
    "buy", "sell", "trade", "order", "close", "cancel", "delete", "create", "write", "push",
)  # fmt: skip
NAV_ONLY_NOTE = "navigation only: review and act in the app"

_REF = r"([0-9a-f]{1,8}(?:-[0-9a-f]{1,12}){0,4}|\d{1,12})"
_APPROVAL = re.compile(
    r"\b(?:approv\w*|requests?|den(?:y|ied)|reject\w*)\s+"
    r"(?:(?:the\s+)?(?:request|approval)\s+)?(?:number\s+|no\.?\s*|#\s*|id\s+)?" + _REF + r"\b"
)
_PR = re.compile(r"\b(?:pr|pull\s+request|pull)\s*(?:number\s+|no\.?\s*|#\s*)?(\d{1,7})\b")
_REF_OK = re.compile(r"^" + _REF + r"$")


@dataclass(frozen=True, slots=True)
class VoiceResult:
    intent: Intent
    route: str | None
    params: dict[str, str]
    source: Literal["grammar", "model", "none"]
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "route": self.route,
            "params": self.params,
            "source": self.source,
            "note": self.note,
        }


def normalize(text: str) -> str:
    t = text[:MAX_TRANSCRIPT].lower()
    t = re.sub(r"[^\w#\-\s.]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def route_for(intent: str, ref: str | None = None) -> str | None:
    if intent == "open_approvals_inbox":
        return "/dev/approvals"
    if intent == "open_approval" and ref and _REF_OK.match(ref):
        return f"/dev/approvals/{ref}"
    if intent == "open_repairs":
        return "/dev/repairs"
    if intent == "open_pr" and ref and ref.isdigit():
        return f"/dev/prs/{int(ref)}"
    if intent == "show_agents":
        return "/dev/agents"
    return None


def _note(text: str) -> str | None:
    words = set(text.split())
    return NAV_ONLY_NOTE if any(w.startswith(a) for w in words for a in ACTION_WORDS) else None


def parse(transcript: str) -> VoiceResult:
    """The deterministic grammar."""
    text = normalize(transcript)
    note = _note(text)
    if m := _PR.search(text):
        n = m.group(1)
        return VoiceResult("open_pr", route_for("open_pr", n), {"number": n}, "grammar", note)
    if m := _APPROVAL.search(text):
        ref = m.group(1)
        return VoiceResult(
            "open_approval", route_for("open_approval", ref), {"ref": ref}, "grammar", note
        )
    if re.search(r"\brepair", text):
        return VoiceResult("open_repairs", "/dev/repairs", {}, "grammar", note)
    if re.search(r"\bagents?\b", text):
        return VoiceResult("show_agents", "/dev/agents", {}, "grammar", note)
    if re.search(r"\b(?:approv\w*|inbox|pending)\b", text):
        return VoiceResult("open_approvals_inbox", "/dev/approvals", {}, "grammar", note)
    return VoiceResult("unknown", None, {}, "none", note)


class ChatClient(Protocol):
    def chat_stream(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any]
    ) -> Any: ...


SYSTEM_PROMPT = (
    "You map a spoken command to app navigation. Answer with JSON only: "
    '{"intent": one of ' + ", ".join(INTENTS) + ' or "unknown", "ref": string or null}. '
    "ref is the approval id for open_approval or the PR number for open_pr. "
    "You can only navigate; you never approve, deny, merge or run anything."
)


async def _ask_model(client: ChatClient, model: str, text: str) -> tuple[str, str | None]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    parts: list[str] = []
    async for chunk in client.chat_stream(model, messages, {"temperature": 0, "num_predict": 64}):
        parts.append(str((chunk.get("message") or {}).get("content") or ""))
        if chunk.get("done") or sum(map(len, parts)) > 1000:
            break
    raw = "".join(parts)
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0)) if m else {}
    intent = str(data.get("intent") or "unknown")
    ref = data.get("ref")
    return intent, (str(ref)[:36].lower() if ref is not None else None)


async def resolve(
    transcript: str,
    *,
    client: ChatClient | None = None,
    model: str | None = None,
    wait_seconds: float = 10.0,
) -> VoiceResult:
    result = parse(transcript)
    if result.intent != "unknown" or not (client and model):
        return result
    text = normalize(transcript)
    try:
        intent, ref = await asyncio.wait_for(_ask_model(client, model, text), wait_seconds)
    except Exception:  # model down, slow, over budget or nonsense: the grammar's answer stands
        return result
    if intent not in INTENTS:
        return result
    route = route_for(intent, ref)
    if route is None:
        return result
    params: dict[str, str] = {}
    if intent == "open_approval" and ref:
        params["ref"] = ref
    if intent == "open_pr" and ref:
        params["number"] = str(int(ref))
    return VoiceResult(intent, route, params, "model", result.note)  # type: ignore[arg-type]


__all__ = ["INTENTS", "VoiceResult", "normalize", "parse", "resolve", "route_for"]
