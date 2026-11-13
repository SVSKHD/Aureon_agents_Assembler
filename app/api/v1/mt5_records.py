"""MT5 records API (docs/36-mt5-records.md): the 1.7.x bot's records and long-history bars.

Machine routes (bearer MT5_RECORDS_TOKEN; no session, no CSRF; hidden from the UI manifest):

    POST /api/trading/mt5/records         aureon.mt5.records/v1, ≤ MT5_RECORDS_MAX_BATCH rows
    POST /api/trading/mt5/bars            aureon.mt5.bars/v1, ≤ 5,000 closed bars

    200 / 401 bad token / 413 too large / 415 content type or encoding / 422 schema or batch
    size / 429 rate limit (Retry-After) / 503 token unset or misconfigured, or storage down

Reads (signed-in session, like the other /api/trading/mt5 GETs):

    GET /api/trading/mt5/records          filters + keyset paging, newest first
    GET /api/trading/mt5/records/stats    counts per kind, freshness, lag, bot versions
    GET /api/trading/mt5/bars             ascending; format=json|csv
    GET /api/trading/mt5/dataset/crosses  one row per completed EMA 8/32 cross; json|csv|jsonl

Read-only towards trading: nothing here places, modifies or closes an order. The old bot's
POST /api/trading/mt5/ingest (aureon.mt5.push/v1) is a different route and is not touched.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError

from app.agents.trading.mt5.push import BodyTooLarge, BodyUnsupported, PushInvalid, decode_body
from app.agents.trading.ratelimit import BrokerRateLimiter, RateLimitExceeded, Window
from app.api.ui import ui_module
from app.api.v1.mt5_push import _read_capped
from app.auth.deps import Current, DbSession, SettingsDep
from app.core.config import Environment, Settings
from app.core.logging import get_logger
from app.core.prod_checks import records_token_reuse_problem, records_token_strength_problem
from app.schemas.mt5_records import (
    Mt5BarOut,
    Mt5BarRejected,
    Mt5BarsAck,
    Mt5BarsOut,
    Mt5CrossesOut,
    Mt5KindStats,
    Mt5RecordOut,
    Mt5RecordsAck,
    Mt5RecordsOut,
    Mt5RecordStatsOut,
    Mt5Rejected,
)
from app.services import mt5_dataset
from app.services import mt5_records as svc

log = get_logger(__name__)
UI_MODULE = ui_module(
    "mt5-records", label="MT5 records", group="markets", icon="database", order=25
)
router = APIRouter(prefix="/trading/mt5", tags=["trading", "mt5"])

RECORDS_PATH = "/api/trading/mt5/records"
BARS_PATH = "/api/trading/mt5/bars"
SYMBOL = r"^[A-Za-z0-9._#+-]{1,64}$"
TIMEFRAME = r"^[A-Za-z0-9]{1,4}$"

_MACHINE_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "bad or missing bearer token"},
    413: {"description": "body over MT5_INGEST_MAX_BYTES, or over the decompressed cap"},
    415: {"description": "Content-Type other than application/json, or encoding not gzip"},
    422: {"description": "wrong schema id, bad envelope, or too many rows"},
    429: {"description": "too many requests for this token (Retry-After)"},
    503: {"description": "MT5_RECORDS_TOKEN unset or misconfigured, or storage unavailable"},
}
_JSON_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/json": {"schema": {"type": "object"}}},
    }
}


def _err(code: int, key: str, detail: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(code, detail={"code": key, "detail": detail}, headers=headers)


def check_records_token(settings: Settings, authorization: str | None) -> str:
    """503 when off; 401 unless `Authorization: Bearer <MT5_RECORDS_TOKEN>`. Returns the token.

    Same rules as MT5_INGEST_TOKEN (app/api/v1/mt5_push.py): hmac.compare_digest on bytes,
    evaluated even for a wrong scheme; re-checked on every request because managed settings
    can change it at runtime; a value reused from any other credential (any env) or weak in
    prod fails closed with 503 `records_misconfigured`. It authorizes these two routes only."""
    token = settings.MT5_RECORDS_TOKEN.get_secret_value() if settings.MT5_RECORDS_TOKEN else ""
    if not token:
        raise _err(503, "records_disabled", "MT5 records are disabled (MT5_RECORDS_TOKEN unset)")
    problem = records_token_reuse_problem(settings)
    if problem is None and settings.ENV is Environment.PROD:
        problem = records_token_strength_problem(token)
    if problem is not None:
        log.error("mt5_records_token_misconfigured", problem=problem)
        raise _err(503, "records_misconfigured", "MT5 records are disabled: " + problem)
    scheme, _, got = (authorization or "").partition(" ")
    match = hmac.compare_digest(got.strip().encode(), token.encode())
    if not (match and scheme.lower() == "bearer"):
        raise _err(
            401, "unauthorized", "bad or missing bearer token", {"WWW-Authenticate": "Bearer"}
        )
    return token


def _limiter(request: Request, settings: Settings, token: str) -> Any:
    """One fixed window per token (a fingerprint, never the token, names the Redis keys)."""
    fp = hashlib.sha256(token.encode()).hexdigest()[:16]
    key = (fp, settings.MT5_RECORDS_RATE_PER_MINUTE)
    state = request.app.state
    lim = getattr(state, "mt5_records_limiter", None)
    if lim is None or getattr(state, "mt5_records_limiter_key", key) != key:
        lim = BrokerRateLimiter(
            state.redis,
            f"ratelimit:mt5-records:{fp}",
            {"push": [Window(60, settings.MT5_RECORDS_RATE_PER_MINUTE)]},
        )
        state.mt5_records_limiter = lim
        state.mt5_records_limiter_key = key
    return lim


def _store(request: Request) -> svc.RecordsStore:
    store: svc.RecordsStore | None = getattr(request.app.state, "mt5_records_store", None)
    if store is None:
        store = svc.DbRecordsStore(request.app.state.sessionmaker)
        request.app.state.mt5_records_store = store
    return store


async def _machine_body(
    request: Request,
    settings: Settings,
    authorization: str | None,
    content_type: str | None,
    content_encoding: str | None,
    *,
    decimals: bool,
) -> Any:
    """Token, rate limit, content type, size caps, gzip, JSON: the shared front half."""
    token = check_records_token(settings, authorization)
    try:
        await _limiter(request, settings, token).acquire("push", max_wait=0)
    except RateLimitExceeded as exc:
        raise _err(
            429, "rate_limited", "too many requests", {"Retry-After": str(max(1, int(exc.wait_s)))}
        ) from exc
    except Exception as exc:  # Redis down: the token still gates the route; fail open
        log.warning("mt5_records_ratelimit_unavailable", error=type(exc).__name__)
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise _err(415, "unsupported_media_type", "Content-Type must be application/json")
    try:
        raw = await _read_capped(request, settings.MT5_INGEST_MAX_BYTES)
        body = decode_body(raw, content_encoding, settings.MT5_INGEST_MAX_DECOMPRESSED_BYTES)
        return svc.parse_json(body, decimals=decimals)
    except BodyTooLarge as exc:
        raise _err(413, "too_large", str(exc)) from exc
    except BodyUnsupported as exc:
        raise _err(415, "unsupported_encoding", str(exc)) from exc
    except PushInvalid as exc:
        raise _invalid(exc) from exc


def _invalid(exc: PushInvalid) -> HTTPException:
    return HTTPException(
        422, detail={"code": "invalid_body", "detail": exc.detail, "fields": exc.fields}
    )


@router.post(
    "/records",
    response_model=Mt5RecordsAck,
    responses=_MACHINE_RESPONSES,
    openapi_extra=_JSON_BODY,
)
async def post_records(
    request: Request,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    content_type: Annotated[str | None, Header()] = None,
    content_encoding: Annotated[str | None, Header()] = None,
) -> Mt5RecordsAck:
    """The bot sends its records (store.py rows) in batches. Idempotent per record_id; a bad
    row is listed in `rejected` and never fails the batch. Never touches orders."""
    doc = await _machine_body(
        request, settings, authorization, content_type, content_encoding, decimals=False
    )
    try:
        batch_id, rows = svc.parse_records_doc(doc, settings.MT5_RECORDS_MAX_BATCH)
    except PushInvalid as exc:
        raise _invalid(exc) from exc
    split = svc.split_batch(rows, datetime.now(UTC))
    try:
        new = await _store(request).store_records(split.rows, batch_id)
    except (SQLAlchemyError, OSError) as exc:
        log.error("mt5_records_store_failed", error=type(exc).__name__)
        raise _err(503, "storage_unavailable", "could not store the records; retry") from exc
    dup = len(split.rows) - new + split.repeated
    log.info(
        "mt5_records",
        batch_id=str(batch_id),
        accepted=new,
        duplicate=dup,
        rejected=len(split.rejected),
    )
    return Mt5RecordsAck(
        accepted=new,
        duplicate=dup,
        rejected=[
            Mt5Rejected(record_id=r.record_id, index=r.index, reason=r.reason)
            for r in split.rejected
        ],
    )


@router.post(
    "/bars",
    response_model=Mt5BarsAck,
    responses=_MACHINE_RESPONSES,
    openapi_extra=_JSON_BODY,
)
async def post_bars(
    request: Request,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    content_type: Annotated[str | None, Header()] = None,
    content_encoding: Annotated[str | None, Header()] = None,
) -> Mt5BarsAck:
    """Closed candles, upserted on (symbol, timeframe, ts): a resend with other values
    corrects the stored bar. A bar with high < low or a non-positive price is rejected."""
    doc = await _machine_body(
        request, settings, authorization, content_type, content_encoding, decimals=True
    )
    try:
        bars_doc = svc.parse_bars_doc(doc)
    except PushInvalid as exc:
        raise _invalid(exc) from exc
    good, bad = svc.split_bars(bars_doc)
    try:
        res = await _store(request).store_bars(good) if good else svc.BarsResult(0, 0, 0)
    except (SQLAlchemyError, OSError) as exc:
        log.error("mt5_bars_store_failed", error=type(exc).__name__)
        raise _err(503, "storage_unavailable", "could not store the bars; retry") from exc
    log.info(
        "mt5_bars",
        symbol=bars_doc.symbol,
        timeframe=bars_doc.timeframe,
        inserted=res.inserted,
        updated=res.updated,
        unchanged=res.unchanged,
        rejected=len(bad),
    )
    return Mt5BarsAck(
        inserted=res.inserted,
        updated=res.updated,
        unchanged=res.unchanged,
        rejected=[Mt5BarRejected(ts=b.ts, index=b.index, reason=b.reason) for b in bad],
    )


# --- reads (session) ---------------------------------------------------------------------------


def _utc(t: datetime | None) -> datetime | None:
    if t is None:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _range(date_from: datetime | None, date_to: datetime | None) -> tuple[Any, Any]:
    f, t = _utc(date_from), _utc(date_to)
    if f is not None and t is not None and f > t:
        raise _err(422, "invalid_range", "`from` is after `to`")
    return f, t


_KIND_RE = re.compile(r"^[a-z_]{1,32}$")


@router.get("/records", response_model=Mt5RecordsOut)
async def get_records(
    db: DbSession,
    _: Current,
    kind: Annotated[list[str] | None, Query(max_length=20)] = None,
    symbol: Annotated[str | None, Query(pattern=SYMBOL)] = None,
    account_login: Annotated[int | None, Query(ge=0)] = None,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    label: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> Mt5RecordsOut:
    """Newest first. `kind` is repeatable; `label` matches payload.label; pass `next_cursor`
    back as `cursor` for the next page."""
    if kind and not all(_KIND_RE.fullmatch(k) for k in kind):
        raise _err(422, "invalid_kind", "kind must match ^[a-z_]{1,32}$")
    f, t = _range(date_from, date_to)
    try:
        cur = svc.decode_cursor(cursor) if cursor else None
    except ValueError as exc:
        raise _err(422, "invalid_cursor", "cursor was not issued by this API") from exc
    rows, nxt = await svc.list_records(
        db,
        svc.RecordFilter(kind or None, symbol, account_login, f, t, label),
        limit,
        cur,
    )
    return Mt5RecordsOut(
        rows=[Mt5RecordOut.model_validate(r, from_attributes=True) for r in rows],
        next_cursor=nxt,
    )


@router.get("/records/stats", response_model=Mt5RecordStatsOut)
async def get_record_stats(db: DbSession, _: Current) -> Mt5RecordStatsOut:
    st = await svc.record_stats(db)
    return Mt5RecordStatsOut(
        as_of=st.as_of,
        kinds=[Mt5KindStats.model_validate(k, from_attributes=True) for k in st.kinds],
        total=st.total,
        newest_utc_time=st.newest_utc_time,
        newest_received_at=st.newest_received_at,
        lag_seconds=st.lag_seconds,
        bot_versions_24h=st.bot_versions_24h,
    )


BAR_CSV_COLUMNS = (
    "symbol", "timeframe", "ts", "server_ts", "open", "high", "low", "close", "tick_volume",
    "spread", "ema8", "ema32", "ema200", "atr14", "source",
)  # fmt: skip


def _bar_cell(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


@router.get("/bars", response_model=Mt5BarsOut)
async def get_bars(
    db: DbSession,
    _: Current,
    symbol: Annotated[str, Query(pattern=SYMBOL)],
    timeframe: Annotated[str, Query(pattern=TIMEFRAME)] = svc.DEFAULT_TIMEFRAME,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=20_000)] = 5000,
    format: Literal["json", "csv"] = "json",
) -> Mt5BarsOut | Response:
    """Stored closed bars, ascending by ts (the first `limit` from `from`)."""
    f, t = _range(date_from, date_to)
    tf = timeframe.upper()
    rows = await svc.list_bars(db, symbol, tf, f, t, limit)
    if format == "csv":
        csv = mt5_dataset.to_csv(
            ({c: _bar_cell(getattr(r, c)) for c in BAR_CSV_COLUMNS} for r in rows),
            BAR_CSV_COLUMNS,
        )
        return Response(
            csv,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{symbol}_{tf}_bars.csv"',
                "X-Aureon-Row-Count": str(len(rows)),
            },
        )
    return Mt5BarsOut(
        symbol=symbol,
        timeframe=tf,
        count=len(rows),
        rows=[Mt5BarOut.model_validate(r, from_attributes=True) for r in rows],
    )


def _iso(t: datetime | None) -> str:
    return t.isoformat() if t else ""


@router.get("/dataset/crosses", response_model=Mt5CrossesOut)
async def get_crosses_dataset(
    db: DbSession,
    _: Current,
    symbol: Annotated[str, Query(pattern=SYMBOL)],
    timeframe: Annotated[str, Query(pattern=TIMEFRAME)] = svc.DEFAULT_TIMEFRAME,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    format: Literal["json", "csv", "jsonl"] = "json",
) -> Mt5CrossesOut | Response:
    """The training set: one row per completed EMA 8/32 cross in [from, to], computed from
    mt5.bars with the bot's labels.py definitions. Features first; outcomes are prefixed
    `y_`. The last, still-open cross is excluded. csv / jsonl carry the row count, the range
    and the definitions version in X-Aureon-* headers."""
    f, t = _range(date_from, date_to)
    tf = timeframe.upper()
    ds = await svc.crosses_dataset(db, symbol, tf, f, t)
    if format == "json":
        return Mt5CrossesOut(
            definitions_version=mt5_dataset.DEFINITIONS_VERSION,
            symbol=symbol,
            timeframe=tf,
            date_from=ds.date_from,
            date_to=ds.date_to,
            bars_from=ds.bars_from,
            bars_used=ds.bars_used,
            row_count=len(ds.rows),
            feature_columns=list(mt5_dataset.FEATURES),
            outcome_columns=list(mt5_dataset.OUTCOMES),
            rows=ds.rows,
        )
    headers = {
        "Content-Disposition": f'attachment; filename="{symbol}_{tf}_crosses.{format}"',
        "X-Aureon-Definitions-Version": mt5_dataset.DEFINITIONS_VERSION,
        "X-Aureon-Row-Count": str(len(ds.rows)),
        "X-Aureon-Date-From": _iso(ds.date_from),
        "X-Aureon-Date-To": _iso(ds.date_to),
        "X-Aureon-Bars-From": _iso(ds.bars_from),
    }
    if format == "csv":
        return Response(
            mt5_dataset.to_csv(ds.rows), media_type="text/csv; charset=utf-8", headers=headers
        )
    return Response(
        mt5_dataset.to_jsonl(ds.rows), media_type="application/x-ndjson", headers=headers
    )


__all__ = ["BARS_PATH", "RECORDS_PATH", "check_records_token", "router"]
