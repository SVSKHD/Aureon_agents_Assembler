"""POST /api/v1/voice/intent: a transcript -> a navigation intent (docs/23-approval-engine.md).

Admin only (with CSRF). Navigation only: the answer is `{intent, route, params, source, note}`
and nothing is read or written besides an optional local Ollama call. It never approves,
denies, merges or executes anything, whatever the words.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.ui import ui_module
from app.auth.deps import AdminRequired, SettingsDep
from app.core.logging import get_logger
from app.integrations import voice

UI_MODULE = ui_module("voice", label="Voice", group="ai", icon="mic", order=44)
router = APIRouter(prefix="/v1/voice", tags=["voice"])
log = get_logger("api.voice")


class VoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript: str = Field(min_length=1, max_length=voice.MAX_TRANSCRIPT)


class VoiceOut(BaseModel):
    intent: str
    route: str | None
    params: dict[str, str]
    source: Literal["grammar", "ollama", "none"]
    note: str | None


@router.post("/intent", response_model=VoiceOut)
async def voice_intent(
    _admin: AdminRequired, request: Request, settings: SettingsDep, body: VoiceIn
) -> VoiceOut:
    ollama: Any = (
        getattr(request.app.state, "ollama", None)
        if settings.VOICE_OLLAMA_MODEL and settings.AI_LOCAL_ENABLED
        else None
    )
    result = await voice.resolve(body.transcript, ollama=ollama, model=settings.VOICE_OLLAMA_MODEL)
    log.info("voice_intent", intent=result.intent, source=result.source)  # never the text
    return VoiceOut(**result.as_dict())
