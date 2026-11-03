"""The bot's push document (schema "aureon.mt5.push/v1"), a superset of status/v1.

Contract: the bot pushes one document per status poll to POST /api/trading/mt5/ingest.
Everything v1 had is validated by `BotStatus` (so the pull path's sync code is reused as is);
the push adds push_id/seq, health, guardian, signals_latest, events with ids, tracebacks,
strategy and candles. Numbers are parsed to Decimal from their text, never float.

Also here: the request-body limits (raw and gzip-decompressed), so a gzip bomb is cut off
after MT5_INGEST_MAX_DECOMPRESSED_BYTES of output.
"""

from __future__ import annotations

import json
import uuid
import zlib
from decimal import Decimal
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.agents.trading.mt5.status import BotStatus, _Open

PUSH_SCHEMA = "aureon.mt5.push/v1"
MAX_CANDLES_PER_TF = 300
MAX_TRACEBACKS = 20
MAX_TRACEBACK_TEXT = 8 * 1024
MAX_EVENTS = 5000


class BodyTooLarge(Exception):
    """413: the raw or the decompressed body is over its cap."""


class BodyUnsupported(Exception):
    """415: a Content-Encoding other than identity / gzip."""


class PushInvalid(Exception):
    """422: not JSON, not push/v1, or failed validation. `fields` never echoes values."""

    def __init__(self, detail: str, fields: list[str] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.fields = fields or []


def decode_body(raw: bytes, encoding: str | None, max_decompressed: int) -> bytes:
    """Undo Content-Encoding (identity or gzip), never producing more than max_decompressed."""
    enc = (encoding or "identity").strip().lower()
    if enc in ("", "identity"):
        if len(raw) > max_decompressed:
            raise BodyTooLarge("body too large")
        return raw
    if enc not in ("gzip", "x-gzip"):
        raise BodyUnsupported(f"unsupported Content-Encoding {enc[:20]!r}")
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = d.decompress(raw, max_decompressed + 1)
        if len(out) > max_decompressed or d.unconsumed_tail:
            raise BodyTooLarge("decompressed body too large")
        out += d.flush(max_decompressed + 1 - len(out))
    except zlib.error as exc:
        raise PushInvalid("body is not valid gzip") from exc
    if len(out) > max_decompressed:
        raise BodyTooLarge("decompressed body too large")
    if not d.eof:
        raise PushInvalid("gzip body is truncated")
    return out


# --- schema -------------------------------------------------------------------------------


class MarketHealth(_Open):
    open: bool | None = None
    tick_age_s: Decimal | None = None
    session: str | None = None


class Mt5Health(_Open):
    connected: bool | None = None
    terminal: str | None = None
    last_error: str | None = None


class DiscordHealth(_Open):
    ok: bool | None = None
    last_error: str | None = None
    last_error_at: Any = None


def _nulls_to_empty(data: Any, keys: tuple[str, ...], empty: Any = None) -> Any:
    """A block sent as null (or missing) is treated as empty: the bot may omit any of them."""
    if isinstance(data, dict):
        for k in keys:
            if k in data and data[k] is None:
                data = {**data, k: {} if empty is None else empty}
    return data


class PushHealth(_Open):
    status: str | None = None
    mt5: Mt5Health = Field(default_factory=Mt5Health)
    discord: DiscordHealth = Field(default_factory=DiscordHealth)
    market: dict[str, MarketHealth] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _nulls(cls, data: Any) -> Any:
        return _nulls_to_empty(data, ("mt5", "discord", "market"))


class GuardianEntry(_Open):
    """Bot PR #38: `phase` is the guardian's own word (pre|protect|secured|ride, any string is
    kept), the A/B leg is `leg_phase`; secured_level / peak are prices, sl is the broker stop."""

    ticket: int
    phase: str | None = None
    leg_phase: str | None = None
    profile: str | None = None
    secured_pts: Decimal | None = None
    peak_pts: Decimal | None = None
    bars: Any = None
    frozen: Any = None
    sl: Decimal | None = None
    secured_level: Decimal | None = None
    peak: Decimal | None = None
    direction: str | None = None
    entry: Decimal | None = None
    updated_at: str | None = None


class Traceback(_Open):
    id: str = Field(min_length=1, max_length=128)
    t: int | None = None
    where: str = Field(default="main", max_length=128)
    type: str = Field(default="Exception", max_length=128)
    message: str = ""
    traceback: str = ""

    @field_validator("message", "traceback", "where", "type", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return "" if v is None else str(v)

    @field_validator("message", "traceback")
    @classmethod
    def _cap(cls, v: str) -> str:
        return v[:MAX_TRACEBACK_TEXT]


class Candle(BaseModel):
    model_config = ConfigDict(extra="ignore")
    t: int  # MT5 server-clock epoch seconds
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741  (the contract's key)
    c: Decimal
    v: Decimal | None = None
    # Keys are the strategy's periods as strings ({"50": .., "80": ..} for ema5080).
    ema: dict[str, Any] = Field(default_factory=dict)


class PushStrategy(_Open):
    mode: str | None = None
    symbols: list[str] = Field(default_factory=list)
    exec_tf: str | None = None
    timeframes: list[str] = Field(default_factory=list)
    ema: dict[str, Any] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)


class PushDoc(BotStatus):
    push_id: uuid.UUID
    seq: int | None = None
    health: PushHealth = Field(default_factory=PushHealth)
    guardian: dict[str, list[GuardianEntry]] = Field(default_factory=dict)
    # Kinds pass through as the bot names them (P, cross, reentry, protected, partial, ...).
    signals_latest: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tracebacks: list[Traceback] = Field(default_factory=list, max_length=MAX_TRACEBACKS)
    strategy: PushStrategy | None = None
    candles: dict[str, dict[str, list[Candle]]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _nulls(cls, data: Any) -> Any:
        data = _nulls_to_empty(data, ("health", "guardian", "signals_latest", "candles"))
        return _nulls_to_empty(data, ("tracebacks",), [])

    @field_validator("events")
    @classmethod
    def _events(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(v) > MAX_EVENTS:
            raise ValueError(f"at most {MAX_EVENTS} events per push")
        for ev in v:
            if "id" in ev and (not isinstance(ev["id"], str) or not 0 < len(ev["id"]) <= 128):
                raise ValueError("event id must be a string of 1-128 characters")
        return v

    @field_validator("candles")
    @classmethod
    def _candles(cls, v: dict[str, dict[str, list[Candle]]]) -> dict[str, dict[str, list[Candle]]]:
        # The contract says max 300 per timeframe; keep the newest rather than refuse.
        return {
            s: {tf: bars[-MAX_CANDLES_PER_TF:] for tf, bars in tfs.items()} for s, tfs in v.items()
        }


def parse_push(data: bytes) -> PushDoc:
    try:
        doc = json.loads(data, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PushInvalid("body is not valid JSON") from exc
    if not isinstance(doc, dict) or doc.get("schema") != PUSH_SCHEMA:
        raise PushInvalid(f"schema must be {PUSH_SCHEMA}", ["schema"])
    try:
        push = PushDoc.model_validate(doc)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})[:10]
        raise PushInvalid("push failed validation", fields) from exc
    if push.generated_at.tzinfo is None:
        raise PushInvalid("generated_at has no timezone", ["generated_at"])
    return push
