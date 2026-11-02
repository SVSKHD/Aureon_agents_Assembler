"""The bot's read-only status export (schema "aureon.mt5.status/v1").

Contract: docs/STATUS_EXPORT.md in SVSKHD/Aureon_agentic_bot. Validation is strict about the
parts Aureon relies on and keeps everything else as-is. Prices arrive as JSON numbers; they are
parsed to Decimal from their text (json.loads(parse_float=Decimal)) so no float ever appears.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.trading.base import BrokerError
from app.agents.trading.types import Timeframe

SCHEMA = "aureon.mt5.status/v1"
MAX_BYTES = 8 * 1024 * 1024

# The bot's MT5-style timeframe names → ours.
TIMEFRAMES = {
    "M1": Timeframe.M1,
    "M5": Timeframe.M5,
    "M15": Timeframe.M15,
    "M30": Timeframe.M30,
    "H1": Timeframe.H1,
    "H4": Timeframe.H4,
    "D1": Timeframe.D1,
}


class StatusInvalid(BrokerError):
    """The export is unreadable or not schema v1."""


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class BotPosition(_Open):
    ticket: int
    symbol: str
    direction: str  # "long" | "short"
    price_open: Decimal
    volume: Decimal
    current: Decimal | None = None
    sl: Decimal | None = None
    tp: Decimal | None = None
    time: int | None = None  # MT5 server-clock epoch seconds
    points: Decimal | None = None


class BotAgent(_Open):
    symbol: str
    strategy: str | None = None
    exec_tf: str | None = None
    alive: bool | None = None
    snapshot_at: datetime | None = None
    market_open: bool | None = None
    connected: bool | None = None
    mt5: str | None = None
    tick_age: Decimal | None = None
    positions: list[BotPosition] = Field(default_factory=list)
    today: dict[str, Any] = Field(default_factory=dict)
    health: dict[str, Any] = Field(default_factory=dict)
    offset_h: Decimal | None = None
    latest_signal: dict[str, Any] | None = None


class BotInfo(_Open):
    version: str | None = None
    mode: str | None = None
    symbols: list[str] = Field(default_factory=list)
    exec_tf: str | None = None
    dry: bool = False
    server_utc_offset: Decimal | None = None
    uptime_s: int | None = None
    pid: int | None = None


class BotStatus(_Open):
    schema_: str = Field(alias="schema")
    generated_at: datetime
    generated_ts: int | None = None
    bot: BotInfo
    agents: dict[str, BotAgent] = Field(default_factory=dict)
    halt: dict[str, Any] | None = None
    console: dict[str, Any] | None = None
    ema921e: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    ema921e_trades: list[dict[str, Any]] = Field(default_factory=list)
    backtests: list[dict[str, Any]] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or datetime.now(UTC)) - self.generated_at

    def offset_hours(self, symbol: str | None) -> Decimal:
        """Server-clock offset from UTC for this symbol's agent (falls back to the bot's)."""
        agent = self.agents.get(symbol or "")
        if agent is not None and agent.offset_h is not None:
            return agent.offset_h
        return self.bot.server_utc_offset or Decimal(0)

    def server_to_utc(self, server_epoch: int, symbol: str | None) -> datetime:
        offset = self.offset_hours(symbol)
        return datetime.fromtimestamp(server_epoch, UTC) - timedelta(seconds=int(offset * 3600))

    def timeframe(self, symbol: str | None) -> Timeframe | None:
        agent = self.agents.get(symbol or "")
        raw = (agent.exec_tf if agent and agent.exec_tf else self.bot.exec_tf) or ""
        return TIMEFRAMES.get(raw.strip().upper())


def parse_status(data: bytes) -> BotStatus:
    if len(data) > MAX_BYTES:
        raise StatusInvalid("mt5 status export is too large")
    try:
        doc = json.loads(data, parse_float=Decimal)
    except ValueError as exc:
        raise StatusInvalid("mt5 status export is not valid JSON") from exc
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise StatusInvalid(f"mt5 status export is not {SCHEMA}")
    try:
        status = BotStatus.model_validate(doc)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})[:5]
        raise StatusInvalid(f"mt5 status export failed validation: {', '.join(fields)}") from exc
    if status.generated_at.tzinfo is None:
        raise StatusInvalid("mt5 status export generated_at has no timezone")
    return status


class StatusSource:
    """Reads the export from a file (bind mount) or the bot's GET /status endpoint."""

    def __init__(
        self,
        *,
        path: str | None = None,
        url: str | None = None,
        token: str | None = None,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not path and not url:
            raise ValueError("MT5 status source needs a file path or a URL")
        self.path = Path(path) if path else None
        self.url = url
        self._token = token
        self._client = (
            httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
            if url
            else None
        )

    @property
    def kind(self) -> str:
        return "file" if self.path else "http"

    async def read(self) -> BotStatus:
        return parse_status(await self._read_bytes())

    async def _read_bytes(self) -> bytes:
        if self.path is not None:
            try:
                return await asyncio.to_thread(self.path.read_bytes)
            except FileNotFoundError as exc:
                raise BrokerError("mt5 status file not found (is the bot export on?)") from exc
            except OSError as exc:
                raise BrokerError(f"mt5 status file unreadable: {type(exc).__name__}") from exc
        assert self._client is not None and self.url
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            r = await self._client.get(self.url, headers=headers)
        except httpx.HTTPError as exc:
            raise BrokerError(f"mt5 status endpoint unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise BrokerError(f"mt5 status endpoint returned HTTP {r.status_code}")
        return r.content

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
