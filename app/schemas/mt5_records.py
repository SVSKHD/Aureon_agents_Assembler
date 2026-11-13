"""MT5 records API schemas (docs/36-mt5-records.md).

Wire contracts written by the bot: aureon.mt5.records/v1 (POST /api/trading/mt5/records) and
aureon.mt5.bars/v1 (POST /api/trading/mt5/bars). Their bodies are validated row by row in
app/services/mt5_records.py (a bad row never fails the batch), so only the responses and the
reads are modelled here. Bar prices serialize as JSON strings, as everywhere in the trading API.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class Mt5Rejected(BaseModel):
    """One row the server did not store, and why (the batch itself still succeeds)."""

    record_id: str | None = None
    index: int
    reason: str


class Mt5RecordsAck(BaseModel):
    accepted: int
    duplicate: int
    rejected: list[Mt5Rejected]


class Mt5BarRejected(BaseModel):
    ts: str | None = None
    index: int
    reason: str


class Mt5BarsAck(BaseModel):
    inserted: int
    updated: int
    unchanged: int
    rejected: list[Mt5BarRejected]


class Mt5RecordOut(BaseModel):
    record_id: uuid.UUID
    kind: str
    symbol: str | None
    account_login: int | None
    utc_time: datetime
    server_time: datetime | None
    bot_version: str
    bot_id: str
    payload: dict[str, Any]
    batch_id: uuid.UUID
    received_at: datetime


class Mt5RecordsOut(BaseModel):
    rows: list[Mt5RecordOut]
    next_cursor: str | None


class Mt5KindStats(BaseModel):
    kind: str
    today: int
    days_7: int
    total: int
    newest_utc_time: datetime | None
    newest_received_at: datetime | None


class Mt5RecordStatsOut(BaseModel):
    as_of: datetime
    kinds: list[Mt5KindStats]
    total: int
    newest_utc_time: datetime | None
    newest_received_at: datetime | None
    # Seconds between the newest record's utc_time and now (None before the first record).
    lag_seconds: float | None
    # Distinct bot_version values received in the last 24 hours.
    bot_versions_24h: list[str]


class Mt5BarOut(BaseModel):
    symbol: str
    timeframe: str
    ts: datetime
    server_ts: datetime | None
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int | None
    spread: int | None
    ema8: Decimal | None
    ema32: Decimal | None
    ema200: Decimal | None
    atr14: Decimal | None
    source: str
    received_at: datetime


class Mt5BarsOut(BaseModel):
    symbol: str
    timeframe: str
    count: int
    rows: list[Mt5BarOut]


class Mt5CrossesOut(BaseModel):
    """The EMA 8/32 cross training set (one row per completed cross). Outcome columns carry the
    `y_` prefix: they are known only after the cross and must never be used as features."""

    definitions_version: str
    symbol: str
    timeframe: str
    # The window the crosses were taken from, and the first bar loaded (EMA/ATR warm-up).
    date_from: datetime | None
    date_to: datetime | None
    bars_from: datetime | None
    bars_used: int
    row_count: int
    feature_columns: list[str]
    outcome_columns: list[str]
    rows: list[dict[str, Any]]
