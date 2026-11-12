"""Voice navigation's model fallback over the Claude gateway.

app/integrations/voice.py asks a `ChatClient` (`chat_stream(model, messages, options)`) only
when its grammar finds no intent; this adapter answers that with one small Claude call
(AI_MODEL_ROUTER, charged to the chat budget). The voice module validates whatever comes
back against its fixed intents and routes, so the model can only pick a navigation target.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from app.ai.agents import BUDGET_OF
from app.ai.gateway import ClaudeGateway


class ClaudeVoiceClient:
    def __init__(self, gateway: ClaudeGateway) -> None:
        self.gateway = gateway

    async def chat_stream(
        self, model: str, messages: list[dict[str, str]], options: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [m for m in messages if m["role"] != "system"]
        res = await self.gateway.call(
            agent="voice",
            budget=BUDGET_OF["voice"],
            system=system,
            messages=turns,
            model=model,
            max_tokens=1024,
        )
        yield {"message": {"content": res.text}, "done": True}


__all__ = ["ClaudeVoiceClient"]
