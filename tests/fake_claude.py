"""A scripted stand-in for the Anthropic SDK client: no network, no key.

    fake = FakeClaude([text_reply("hi"), tool_reply([("get_positions", {})]), ...])
    gw = ClaudeGateway(settings_with_key, MemoryUsageStore(), client=fake)

Each `messages.stream(**params)` call pops the next scripted reply (an anthropic.types.Message,
or an exception to raise) and records the params, so tests can check what was sent:
caching, effort, tools, redaction.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator, Iterable
from typing import Any

from anthropic.types import Message

_ids = itertools.count(1)


def message(
    content: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read: int = 0,
    cache_write: int = 0,
) -> Message:
    return Message.model_validate(
        {
            "id": f"msg_{next(_ids)}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        }
    )


def text_reply(text: str, **kw: Any) -> Message:
    return message([{"type": "text", "text": text}], **kw)


def tool_reply(calls: Iterable[tuple[str, dict[str, Any]]], text: str = "", **kw: Any) -> Message:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    blocks += [
        {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": args}
        for name, args in calls
    ]
    return message(blocks, stop_reason="tool_use", **kw)


class _Stream:
    def __init__(self, reply: Message) -> None:
        self.reply = reply

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            for b in self.reply.content:
                if b.type == "text":
                    yield b.text

        return gen()

    async def get_final_message(self) -> Message:
        return self.reply


class _Messages:
    def __init__(self, owner: FakeClaude) -> None:
        self.owner = owner

    def stream(self, **params: Any) -> _Stream:
        self.owner.calls.append(params)
        if not self.owner.replies:
            raise AssertionError("FakeClaude: no scripted reply left")
        reply = self.owner.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return _Stream(reply)


class _Model:
    def __init__(self, mid: str) -> None:
        self.id = mid


class _Models:
    def __init__(self, ids: list[str]) -> None:
        self.ids = ids

    def list(self) -> AsyncIterator[_Model]:
        async def gen() -> AsyncIterator[_Model]:
            for i in self.ids:
                yield _Model(i)

        return gen()


class FakeClaude:
    def __init__(
        self,
        replies: Iterable[Message | BaseException] = (),
        models: Iterable[str] = ("claude-opus-5-5", "claude-haiku-5-5"),
    ) -> None:
        self.replies: list[Message | BaseException] = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.messages = _Messages(self)
        self.models = _Models(list(models))
        self.closed = False

    async def close(self) -> None:
        self.closed = True
