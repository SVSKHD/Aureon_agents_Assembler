"""MT5 records API (docs/36-mt5-records.md): storage and reads for mt5.records and mt5.bars.

Writers (machine routes, bearer MT5_RECORDS_TOKEN):

- POST /api/trading/mt5/records, schema aureon.mt5.records/v1: rows exactly as the bot's
  store.py builds them. The seven envelope keys become columns; every other key is `payload`.
  Each row is validated on its own (a bad row is listed in `rejected`, the rest are stored).
  INSERT ... ON CONFLICT (record_id) DO NOTHING: a resend counts as `duplicate`.
- POST /api/trading/mt5/bars, schema aureon.mt5.bars/v1: closed candles, upsert on
  (symbol, timeframe, ts). A resend with different values updates the row (the bot's weekly
  refetch can correct bars); identical values are `unchanged`.

Read-only towards the bot and the broker: nothing here can place, modify or close an order.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy import (
    Numeric,
    Select,
    and_,
    case,
    cast,
    delete,
    func,
    literal,
    literal_column,
    or_,
    select,
    tuple_,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.trading.mt5.push import PushInvalid
from app.core.config import Settings
from app.core.logging import get_logger
from app.models.brokers import Mt5Bar, Mt5Record
from app.services import mt5_dataset

log = get_logger(__name__)

RECORDS_SCHEMA = "aureon.mt5.records/v1"
BARS_SCHEMA = "aureon.mt5.bars/v1"
ENVELOPE = ("record_id", "kind", "utc_time", "server_time", "version", "account_login", "symbol")
KIND_RE = re.compile(r"^[a-z_]{1,32}$")
SYMBOL_RE = re.compile(r"^[A-Za-z0-9._#+-]{1,64}$")
TIMEFRAME_RE = re.compile(r"^[A-Z0-9]{1,4}$")
SOURCE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
MAX_FUTURE = timedelta(hours=24)
MAX_BARS = 5000
MAX_PAYLOAD_DEPTH = 16
MAX_TEXT = 64
BIGINT_MAX = 2**63 - 1
INT_MAX = 2**31 - 1
# numeric(18,6): at most 12 digits before the point.
PRICE_LIMIT = Decimal(10) ** 12
DEFAULT_TIMEFRAME = "M30"
# Bars loaded before `from` so the EMA 200 and the ATR are warmed up for the dataset.
DATASET_WARMUP_BARS = 600
DATASET_MAX_BARS = 250_000


class RowInvalid(ValueError):
    """One row (or bar) is not acceptable; the message is safe to return (never the value)."""


# --- body ------------------------------------------------------------------------------------


def _no_constants(name: str) -> Any:
    raise PushInvalid(f"JSON constant {name} is not allowed")


def parse_json(body: bytes, *, decimals: bool = False) -> Any:
    """JSON with NaN / Infinity refused (Postgres JSONB and numeric reject them anyway)."""
    try:
        return json.loads(
            body,
            parse_constant=_no_constants,
            parse_float=Decimal if decimals else float,
        )
    except PushInvalid:
        raise
    except (ValueError, RecursionError) as exc:
        raise PushInvalid("body is not valid JSON") from exc


def check_envelope(doc: Any, schema: str) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise PushInvalid("body must be a JSON object", ["$"])
    if doc.get("schema") != schema:
        raise PushInvalid(f"schema must be {schema!r}", ["schema"])
    return doc


# --- records: row splitting ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordRow:
    record_id: uuid.UUID
    kind: str
    symbol: str | None
    account_login: int | None
    utc_time: datetime
    server_time: datetime | None
    bot_version: str
    payload: dict[str, Any]

    def values(self, batch_id: uuid.UUID) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "symbol": self.symbol,
            "account_login": self.account_login,
            "utc_time": self.utc_time,
            "server_time": self.server_time,
            "bot_version": self.bot_version,
            "payload": self.payload,
            "batch_id": batch_id,
        }


@dataclass(frozen=True, slots=True)
class Rejection:
    index: int
    reason: str
    record_id: str | None = None


def parse_utc(v: Any, name: str) -> datetime:
    """ISO 8601. A value without a zone is taken as UTC (the field names say UTC)."""
    if not isinstance(v, str) or not v.strip() or len(v) > 40:
        raise RowInvalid(f"{name} must be an ISO 8601 time")
    try:
        t = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RowInvalid(f"{name} must be an ISO 8601 time") from exc
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def parse_server_time(v: Any, name: str) -> datetime | None:
    """The broker server's wall clock ("2026-11-13 11:30:03"); stored without a zone."""
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip() or len(v) > 40:
        raise RowInvalid(f"{name} must be a time or null")
    try:
        t = datetime.fromisoformat(v.strip())
    except ValueError as exc:
        raise RowInvalid(f"{name} must be a time or null") from exc
    return t.replace(tzinfo=None)


def _text(v: Any, name: str, *, required: bool, limit: int = MAX_TEXT) -> str | None:
    if v is None:
        if required:
            raise RowInvalid(f"{name} is required")
        return None
    if not isinstance(v, str) or not v.strip():
        raise RowInvalid(f"{name} must be a non-empty string")
    out: str = v.strip()
    if len(out) > limit:
        raise RowInvalid(f"{name} is longer than {limit} characters")
    if "\x00" in out:
        raise RowInvalid(f"{name} contains a NUL character")
    return out


def _login(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        raise RowInvalid("account_login must be an integer or null")
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if not isinstance(v, int) or not 0 <= v <= BIGINT_MAX:
        raise RowInvalid("account_login must be an integer or null")
    return v


def _check_payload(v: Any, depth: int = 0) -> None:
    """JSONB-safe: no NUL in any string or key, not absurdly deep."""
    if depth > MAX_PAYLOAD_DEPTH:
        raise RowInvalid("payload is nested too deeply")
    if isinstance(v, str):
        if "\x00" in v:
            raise RowInvalid("payload contains a NUL character")
    elif isinstance(v, float):
        if not math.isfinite(v):
            raise RowInvalid("payload contains a non-finite number")
    elif isinstance(v, dict):
        for k, x in v.items():
            if "\x00" in k:
                raise RowInvalid("payload contains a NUL character")
            _check_payload(x, depth + 1)
    elif isinstance(v, list):
        for x in v:
            _check_payload(x, depth + 1)


def _given_id(row: Any) -> str | None:
    rid = row.get("record_id") if isinstance(row, dict) else None
    return rid[:64] if isinstance(rid, str) else None


def split_row(row: Any, now: datetime) -> RecordRow:
    """One wire row → envelope columns + payload, or RowInvalid with the reason."""
    if not isinstance(row, dict):
        raise RowInvalid("row must be a JSON object")
    rid = row.get("record_id")
    if not isinstance(rid, str):
        raise RowInvalid("record_id is required (uuid4 hex)")
    try:
        record_id = uuid.UUID(rid.strip())
    except ValueError as exc:
        raise RowInvalid("record_id must be a uuid (32 hex characters)") from exc
    kind = row.get("kind")
    if not isinstance(kind, str) or not KIND_RE.fullmatch(kind):
        raise RowInvalid("kind must match ^[a-z_]{1,32}$")
    utc_time = parse_utc(row.get("utc_time"), "utc_time")
    if utc_time > now + MAX_FUTURE:
        raise RowInvalid("utc_time is more than 24 hours in the future")
    server_time = parse_server_time(row.get("server_time"), "server_time")
    version = _text(row.get("version"), "version", required=True)
    assert version is not None
    symbol = _text(row.get("symbol"), "symbol", required=False)
    login = _login(row.get("account_login"))
    payload = {k: v for k, v in row.items() if k not in ENVELOPE}
    _check_payload(payload)
    return RecordRow(record_id, kind, symbol, login, utc_time, server_time, version, payload)


@dataclass
class SplitBatch:
    rows: list[RecordRow] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    # Rows whose record_id appeared earlier in the same batch (counted as duplicates).
    repeated: int = 0


def split_batch(rows: list[Any], now: datetime) -> SplitBatch:
    out = SplitBatch()
    seen: set[uuid.UUID] = set()
    for i, raw in enumerate(rows):
        try:
            r = split_row(raw, now)
        except RowInvalid as exc:
            out.rejected.append(Rejection(i, str(exc), _given_id(raw)))
            continue
        if r.record_id in seen:
            out.repeated += 1
            continue
        seen.add(r.record_id)
        out.rows.append(r)
    return out


def parse_records_doc(doc: Any, max_batch: int) -> tuple[uuid.UUID, list[Any]]:
    """The batch envelope: schema, batch_id, rows (≤ max_batch). PushInvalid → 422."""
    d = check_envelope(doc, RECORDS_SCHEMA)
    try:
        batch_id = uuid.UUID(str(d.get("batch_id") or ""))
    except ValueError as exc:
        raise PushInvalid("batch_id must be a uuid", ["batch_id"]) from exc
    rows = d.get("rows")
    if not isinstance(rows, list):
        raise PushInvalid("rows must be a list", ["rows"])
    if len(rows) > max_batch:
        raise PushInvalid(f"more than {max_batch} rows (MT5_RECORDS_MAX_BATCH)", ["rows"])
    return batch_id, rows


# --- bars: validation ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BarsDoc:
    symbol: str
    timeframe: str
    source: str
    bars: list[Any]


@dataclass(frozen=True, slots=True)
class BarRejection:
    index: int
    reason: str
    ts: str | None = None


def parse_bars_doc(doc: Any) -> BarsDoc:
    d = check_envelope(doc, BARS_SCHEMA)
    symbol = d.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        raise PushInvalid("symbol must match ^[A-Za-z0-9._#+-]{1,64}$", ["symbol"])
    tf = d.get("timeframe")
    if not isinstance(tf, str) or not TIMEFRAME_RE.fullmatch(tf.strip().upper()):
        raise PushInvalid("timeframe must be like M30, H1, D1", ["timeframe"])
    source = d.get("source", "bot")
    if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
        raise PushInvalid("source must match ^[a-z0-9_-]{1,32}$", ["source"])
    bars = d.get("bars")
    if not isinstance(bars, list):
        raise PushInvalid("bars must be a list", ["bars"])
    if len(bars) > MAX_BARS:
        raise PushInvalid(f"more than {MAX_BARS} bars", ["bars"])
    return BarsDoc(symbol, tf.strip().upper(), source, bars)


def _price(v: Any, name: str, *, required: bool, positive: bool) -> Decimal | None:
    if v is None:
        if required:
            raise RowInvalid(f"{name} is required")
        return None
    if isinstance(v, bool):
        raise RowInvalid(f"{name} must be a number")
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v))
    except InvalidOperation as exc:
        raise RowInvalid(f"{name} must be a number") from exc
    if not d.is_finite():
        raise RowInvalid(f"{name} must be a finite number")
    if positive and d <= 0:
        raise RowInvalid(f"{name} must be positive")
    if abs(d) >= PRICE_LIMIT:
        raise RowInvalid(f"{name} is out of range")
    return d


def _int(v: Any, name: str, limit: int) -> int | None:
    if v is None:
        return None
    if isinstance(v, Decimal) and v == v.to_integral_value():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or not -limit <= v <= limit:
        raise RowInvalid(f"{name} must be an integer or null")
    return v


def bar_values(raw: Any, doc: BarsDoc) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise RowInvalid("bar must be a JSON object")
    ts = parse_utc(raw.get("ts"), "ts")
    o = _price(raw.get("open"), "open", required=True, positive=True)
    h = _price(raw.get("high"), "high", required=True, positive=True)
    lo = _price(raw.get("low"), "low", required=True, positive=True)
    c = _price(raw.get("close"), "close", required=True, positive=True)
    assert h is not None and lo is not None
    if h < lo:
        raise RowInvalid("high is below low")
    vol = _int(raw.get("tick_volume"), "tick_volume", BIGINT_MAX)
    if vol is not None and vol < 0:
        raise RowInvalid("tick_volume must not be negative")
    return {
        "symbol": doc.symbol,
        "timeframe": doc.timeframe,
        "ts": ts,
        "server_ts": parse_server_time(raw.get("server_ts"), "server_ts"),
        "open": o,
        "high": h,
        "low": lo,
        "close": c,
        "tick_volume": vol,
        "spread": _int(raw.get("spread"), "spread", INT_MAX),
        "ema8": _price(raw.get("ema8"), "ema8", required=False, positive=False),
        "ema32": _price(raw.get("ema32"), "ema32", required=False, positive=False),
        "ema200": _price(raw.get("ema200"), "ema200", required=False, positive=False),
        "atr14": _price(raw.get("atr14"), "atr14", required=False, positive=False),
        "source": doc.source,
    }


def split_bars(doc: BarsDoc) -> tuple[list[dict[str, Any]], list[BarRejection]]:
    """Valid bars (a ts sent twice keeps the last one) and the rejected ones."""
    good: dict[datetime, dict[str, Any]] = {}
    bad: list[BarRejection] = []
    for i, raw in enumerate(doc.bars):
        try:
            v = bar_values(raw, doc)
        except RowInvalid as exc:
            ts = raw.get("ts") if isinstance(raw, dict) else None
            bad.append(BarRejection(i, str(exc), ts[:40] if isinstance(ts, str) else None))
            continue
        good[v["ts"]] = v
    return list(good.values()), bad


# --- storage ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BarsResult:
    inserted: int
    updated: int
    unchanged: int


class RecordsStore(Protocol):
    async def store_records(self, rows: list[RecordRow], batch_id: uuid.UUID) -> int:
        """Insert; return how many were new (the rest were already stored)."""
        ...

    async def store_bars(self, bars: list[dict[str, Any]]) -> BarsResult: ...


BAR_VALUE_COLUMNS = (
    "server_ts", "open", "high", "low", "close", "tick_volume", "spread",
    "ema8", "ema32", "ema200", "atr14", "source",
)  # fmt: skip


class DbRecordsStore:
    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self.maker = maker

    async def store_records(self, rows: list[RecordRow], batch_id: uuid.UUID) -> int:
        if not rows:
            return 0
        new = 0
        async with self.maker() as db, db.begin():
            for i in range(0, len(rows), 1000):
                chunk = rows[i : i + 1000]
                res = await db.scalars(
                    insert(Mt5Record)
                    .values([r.values(batch_id) for r in chunk])
                    .on_conflict_do_nothing(index_elements=[Mt5Record.record_id])
                    .returning(Mt5Record.record_id)
                )
                new += len(res.all())
        return new

    async def store_bars(self, bars: list[dict[str, Any]]) -> BarsResult:
        inserted = updated = 0
        async with self.maker() as db, db.begin():
            for i in range(0, len(bars), 1000):
                ins = insert(Mt5Bar).values(bars[i : i + 1000])
                changed = or_(
                    *(
                        getattr(Mt5Bar, c).is_distinct_from(ins.excluded[c])
                        for c in BAR_VALUE_COLUMNS
                    )
                )
                stmt: Any = ins.on_conflict_do_update(
                    constraint="pk_bars",
                    set_={c: ins.excluded[c] for c in BAR_VALUE_COLUMNS}
                    | {"received_at": func.now()},
                    where=changed,
                ).returning(literal_column("(xmax = 0)").label("inserted"))
                flags = (await db.scalars(stmt)).all()
                inserted += sum(1 for f in flags if f)
                updated += sum(1 for f in flags if not f)
        return BarsResult(inserted, updated, len(bars) - inserted - updated)


# --- reads -----------------------------------------------------------------------------------


def encode_cursor(utc_time: datetime, record_id: uuid.UUID) -> str:
    raw = f"{utc_time.astimezone(UTC).isoformat()}|{record_id.hex}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """ValueError for anything that is not a cursor this API issued."""
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + pad).decode()
        t, _, rid = raw.partition("|")
        when = datetime.fromisoformat(t)
        if when.tzinfo is None:
            raise ValueError("cursor time has no zone")
        return when, uuid.UUID(hex=rid)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("bad cursor") from exc


@dataclass(frozen=True, slots=True)
class RecordFilter:
    kinds: list[str] | None = None
    symbol: str | None = None
    account_login: int | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    label: str | None = None


def _filtered(q: Select[Any], f: RecordFilter) -> Select[Any]:
    if f.kinds:
        q = q.where(Mt5Record.kind.in_(f.kinds))
    if f.symbol:
        q = q.where(Mt5Record.symbol == f.symbol)
    if f.account_login is not None:
        q = q.where(Mt5Record.account_login == f.account_login)
    if f.date_from is not None:
        q = q.where(Mt5Record.utc_time >= f.date_from)
    if f.date_to is not None:
        q = q.where(Mt5Record.utc_time <= f.date_to)
    if f.label is not None:
        # @> uses the GIN (jsonb_path_ops) index; same rows as payload->>'label' = :label.
        q = q.where(Mt5Record.payload.contains({"label": f.label}))
    return q


async def list_records(
    db: AsyncSession, f: RecordFilter, limit: int, cursor: tuple[datetime, uuid.UUID] | None
) -> tuple[list[Mt5Record], str | None]:
    """Newest first, keyset on (utc_time, record_id)."""
    q = _filtered(select(Mt5Record), f)
    if cursor is not None:
        q = q.where(
            tuple_(Mt5Record.utc_time, Mt5Record.record_id)
            < tuple_(literal(cursor[0]), literal(cursor[1]))
        )
    q = q.order_by(Mt5Record.utc_time.desc(), Mt5Record.record_id.desc()).limit(limit + 1)
    rows = list((await db.scalars(q)).all())
    nxt = None
    if len(rows) > limit:
        rows = rows[:limit]
        nxt = encode_cursor(rows[-1].utc_time, rows[-1].record_id)
    return rows, nxt


@dataclass(frozen=True, slots=True)
class KindStats:
    kind: str
    today: int
    days_7: int
    total: int
    newest_utc_time: datetime | None
    newest_received_at: datetime | None


@dataclass(frozen=True, slots=True)
class RecordStats:
    as_of: datetime
    kinds: list[KindStats]
    total: int
    newest_utc_time: datetime | None
    newest_received_at: datetime | None
    lag_seconds: float | None
    bot_versions_24h: list[str]


async def record_stats(db: AsyncSession) -> RecordStats:
    now = await db.scalar(select(func.now()))
    assert now is not None
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    week = now - timedelta(days=7)
    t = Mt5Record.utc_time
    rows = (
        await db.execute(
            select(
                Mt5Record.kind,
                func.count().filter(t >= today),
                func.count().filter(t >= week),
                func.count(),
                func.max(t),
                func.max(Mt5Record.received_at),
            )
            .group_by(Mt5Record.kind)
            .order_by(Mt5Record.kind)
        )
    ).all()
    kinds = [KindStats(k, int(a), int(b), int(c), d, e) for k, a, b, c, d, e in rows]
    newest = max((k.newest_utc_time for k in kinds if k.newest_utc_time), default=None)
    newest_rx = max((k.newest_received_at for k in kinds if k.newest_received_at), default=None)
    versions = (
        await db.scalars(
            select(Mt5Record.bot_version)
            .where(Mt5Record.received_at >= now - timedelta(hours=24))
            .distinct()
            .order_by(Mt5Record.bot_version)
        )
    ).all()
    return RecordStats(
        as_of=now,
        kinds=kinds,
        total=sum(k.total for k in kinds),
        newest_utc_time=newest,
        newest_received_at=newest_rx,
        lag_seconds=round((now - newest).total_seconds(), 3) if newest else None,
        bot_versions_24h=list(versions),
    )


async def list_bars(
    db: AsyncSession,
    symbol: str,
    timeframe: str,
    date_from: datetime | None,
    date_to: datetime | None,
    limit: int,
) -> list[Mt5Bar]:
    """Ascending by ts; the first `limit` bars from date_from (or the oldest)."""
    q = select(Mt5Bar).where(Mt5Bar.symbol == symbol, Mt5Bar.timeframe == timeframe)
    if date_from is not None:
        q = q.where(Mt5Bar.ts >= date_from)
    if date_to is not None:
        q = q.where(Mt5Bar.ts <= date_to)
    return list((await db.scalars(q.order_by(Mt5Bar.ts).limit(limit))).all())


@dataclass(frozen=True, slots=True)
class Dataset:
    symbol: str
    timeframe: str
    date_from: datetime | None
    date_to: datetime | None
    bars_from: datetime | None
    bars_used: int
    rows: list[dict[str, Any]]


async def load_dataset_bars(
    db: AsyncSession,
    symbol: str,
    timeframe: str,
    date_from: datetime | None,
    date_to: datetime | None,
) -> list[mt5_dataset.Bar]:
    """Bars up to date_to, starting DATASET_WARMUP_BARS before date_from (warm-up)."""
    cols = (Mt5Bar.ts, Mt5Bar.open, Mt5Bar.high, Mt5Bar.low, Mt5Bar.close, Mt5Bar.tick_volume)
    base = and_(Mt5Bar.symbol == symbol, Mt5Bar.timeframe == timeframe)
    start: datetime | None = None
    if date_from is not None:
        start = await db.scalar(
            select(func.min(literal_column("w.ts"))).select_from(
                select(Mt5Bar.ts)
                .where(base, Mt5Bar.ts < date_from)
                .order_by(Mt5Bar.ts.desc())
                .limit(DATASET_WARMUP_BARS)
                .subquery("w")
            )
        )
        start = start or date_from
    q = select(*cols).where(base)
    if start is not None:
        q = q.where(Mt5Bar.ts >= start)
    if date_to is not None:
        q = q.where(Mt5Bar.ts <= date_to)
    # The newest DATASET_MAX_BARS when unbounded (oldest dropped), returned ascending.
    inner = q.order_by(Mt5Bar.ts.desc()).limit(DATASET_MAX_BARS).subquery()
    rows = (await db.execute(select(inner).order_by(inner.c.ts))).all()
    return [
        mt5_dataset.Bar(ts, float(o), float(h), float(lo), float(c), v)
        for ts, o, h, lo, c, v in rows
    ]


async def crosses_dataset(
    db: AsyncSession,
    symbol: str,
    timeframe: str,
    date_from: datetime | None,
    date_to: datetime | None,
) -> Dataset:
    bars = await load_dataset_bars(db, symbol, timeframe, date_from, date_to)
    rows = mt5_dataset.cross_rows(bars, symbol, timeframe, date_from, date_to)
    return Dataset(
        symbol=symbol,
        timeframe=timeframe,
        date_from=date_from or (bars[0].ts if bars else None),
        date_to=date_to or (bars[-1].ts if bars else None),
        bars_from=bars[0].ts if bars else None,
        bars_used=len(bars),
        rows=rows,
    )


# --- retention -------------------------------------------------------------------------------


async def purge(maker: async_sessionmaker[AsyncSession], settings: Settings) -> dict[str, Any]:
    """mt5.records older than MT5_RECORDS_RETENTION_DAYS (by utc_time). 0 = keep forever:
    nothing is deleted. mt5.bars is never purged."""
    days = settings.MT5_RECORDS_RETENTION_DAYS
    if days <= 0:
        return {"records": 0, "retention_days": 0}
    async with maker() as db, db.begin():
        res = await db.execute(
            delete(Mt5Record).where(Mt5Record.utc_time < func.now() - timedelta(days=days))
        )
    return {"records": res.rowcount, "retention_days": days}  # type: ignore[attr-defined]


# --- summary for the Claude tool -------------------------------------------------------------


async def resolve_symbol(db: AsyncSession, raw: str) -> str:
    """The stored spelling of a symbol, matched case-insensitively (a broker suffix like
    "XAUUSD.m" keeps its case); the input upper-cased when nothing is stored yet."""
    want = raw.strip()
    for col in (Mt5Bar.symbol, Mt5Record.symbol):
        hit = await db.scalar(
            select(col).where(func.upper(col) == want.upper()).order_by(col).limit(1)
        )
        if hit:
            return str(hit)
    return want.upper()


def _ptext(key: str) -> Any:
    """payload->>'key' with the key as a literal (a constant from this module, never input),
    so the same expression in SELECT and GROUP BY is the same SQL text."""
    assert re.fullmatch(r"[a-z_]{1,32}", key)
    return Mt5Record.payload.op("->>")(literal_column(f"'{key}'"))


def _side_word(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in ("LONG", "BUY", "BULL", "1", "+1"):
        return "long"
    if s in ("SHORT", "SELL", "BEAR", "-1"):
        return "short"
    return "unknown"


async def summary(db: AsyncSession, symbol: str, days: int) -> dict[str, Any]:
    """Compact aggregates for one symbol over the last `days` (never raw rows)."""
    now = await db.scalar(select(func.now()))
    assert now is not None
    since = now - timedelta(days=days)
    base = and_(Mt5Record.symbol == symbol, Mt5Record.utc_time >= since)
    p = Mt5Record.payload
    label = _ptext("label")
    side_c, tag_c = _ptext("side"), _ptext("tag")
    crosses = (
        await db.execute(
            select(side_c, tag_c, func.count())
            .where(base, Mt5Record.kind == "event", label == "CROSS")
            .group_by(side_c, tag_c)
        )
    ).all()
    by_side: dict[str, int] = {}
    by_tag: dict[str, int] = {}
    for side, tag, n in crosses:
        w = _side_word(side)
        by_side[w] = by_side.get(w, 0) + int(n)
        t = str(tag or "unknown")
        by_tag[t] = by_tag.get(t, 0) + int(n)
    labels = (
        await db.execute(
            select(label, func.count())
            .where(base, Mt5Record.kind == "event")
            .group_by(label)
            .order_by(func.count().desc())
        )
    ).all()
    # Only numeric results count (the bot sends null for an open position); CASE keeps a
    # stray non-numeric value from ever reaching the cast.
    result = case(
        (func.jsonb_typeof(p["result"]) == "number", cast(p["result"].astext, Numeric)),
        else_=None,
    )
    closed = p["event"].astext == "closed"
    pos = (
        await db.execute(
            select(
                func.count().filter(p["event"].astext == "opened"),
                func.count().filter(closed),
                func.count().filter(and_(closed, result > 0)),
                func.coalesce(func.sum(case((closed, result), else_=None)), 0),
            ).where(base, Mt5Record.kind == "position")
        )
    ).one()
    sl_changes = await db.scalar(select(func.count()).where(base, Mt5Record.kind == "sl_change"))
    accounts = (
        await db.scalars(
            select(Mt5Record.account_login)
            .where(base, Mt5Record.account_login.is_not(None))
            .distinct()
            .limit(5)
        )
    ).all()
    fresh = (
        await db.execute(
            select(
                func.max(Mt5Record.utc_time), func.max(Mt5Record.received_at), func.count()
            ).where(Mt5Record.symbol == symbol)
        )
    ).one()
    bars_newest = await db.scalar(
        select(func.max(Mt5Bar.ts)).where(
            Mt5Bar.symbol == symbol, Mt5Bar.timeframe == DEFAULT_TIMEFRAME
        )
    )
    ds = await crosses_dataset(db, symbol, DEFAULT_TIMEFRAME, since, None)
    widened = sum(1 for r in ds.rows if r["y_widened"])
    return {
        "symbol": symbol,
        "days": days,
        "since": since,
        "now": now,
        "crosses_by_side": by_side,
        "crosses_by_tag": by_tag,
        "labels": [(str(lb or "unlabelled"), int(n)) for lb, n in labels],
        "positions_opened": int(pos[0]),
        "positions_closed": int(pos[1]),
        "positions_won": int(pos[2]),
        "result_total": pos[3],
        "sl_changes": int(sl_changes or 0),
        "accounts": [int(a) for a in accounts if a is not None],
        "newest_utc_time": fresh[0],
        "newest_received_at": fresh[1],
        "records_total": int(fresh[2]),
        "bars_newest": bars_newest,
        "dataset_rows": len(ds.rows),
        "dataset_widened": widened,
    }
