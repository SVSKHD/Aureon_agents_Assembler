"""Pure mapping from DhanHQ v2 JSON / CSV to the broker-agnostic trading types.

Field names follow the v2 docs and the official SDK exactly, including Dhan's own
misspellings (`availabelBalance`, `receiveableAmount`). Notes:

- Numbers go through Decimal(str(x)), so a JSON float like 0.1 becomes Decimal("0.1"), not a
  binary approximation.
- Dhan times are IST wall-clock strings, which we convert to UTC. Candle `timestamp` values are
  already true UTC epoch seconds.
- Instruments are referenced as "SEGMENT:SECURITY_ID" (e.g. "NSE_EQ:2885"), because Dhan security
  ids are only unique within an exchange segment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.agents.trading.types import (
    AccountInfo,
    BrokerOrder,
    BrokerTrade,
    Candle,
    Funds,
    Holding,
    InstrumentInfo,
    OptionChain,
    OptionLeg,
    OptionStrike,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
)

IST = timezone(timedelta(hours=5, minutes=30), "IST")
BROKER = "dhan"

# Annexure: exchange segment string <-> numeric code (the code is used in the WS feed).
SEGMENT_CODES: dict[str, int] = {
    "IDX_I": 0,
    "NSE_EQ": 1,
    "NSE_FNO": 2,
    "NSE_CURRENCY": 3,
    "BSE_EQ": 4,
    "MCX_COMM": 5,
    "BSE_CURRENCY": 7,
    "BSE_FNO": 8,
}
SEGMENT_BY_CODE = {v: k for k, v in SEGMENT_CODES.items()}

# Instrument-master (EXCH_ID, SEGMENT letter) -> API exchange segment.
CSV_SEGMENTS: dict[tuple[str, str], str] = {
    ("NSE", "E"): "NSE_EQ",
    ("NSE", "D"): "NSE_FNO",
    ("NSE", "C"): "NSE_CURRENCY",
    ("BSE", "E"): "BSE_EQ",
    ("BSE", "D"): "BSE_FNO",
    ("BSE", "C"): "BSE_CURRENCY",
    ("MCX", "M"): "MCX_COMM",
    ("NSE", "I"): "IDX_I",
    ("BSE", "I"): "IDX_I",
}

ORDER_STATUS: dict[str, OrderStatus] = {
    "TRANSIT": OrderStatus.OPEN,
    "PENDING": OrderStatus.OPEN,
    "TRIGGERED": OrderStatus.OPEN,
    "PART_TRADED": OrderStatus.PARTIAL,
    "TRADED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    # "CLOSED" (super orders) and anything new stay UNKNOWN rather than guessed.
}
ORDER_TYPES: dict[str, OrderType] = {
    "LIMIT": OrderType.LIMIT,
    "MARKET": OrderType.MARKET,
    "STOP_LOSS": OrderType.STOP_LIMIT,
    "STOP_LOSS_MARKET": OrderType.STOP,
}


@dataclass(frozen=True, slots=True)
class DhanRef:
    """Where an instrument lives on Dhan (enough to call quote/chart/feed APIs)."""

    segment: str
    security_id: str
    instrument: str  # INDEX, EQUITY, FUTIDX, OPTIDX, FUTSTK, OPTSTK, FUTCOM, ...

    @property
    def key(self) -> str:
        return f"{self.segment}:{self.security_id}"


def split_ref(ref: str) -> tuple[str, str]:
    seg, sep, sid = ref.partition(":")
    if not sep or seg not in SEGMENT_CODES or not sid.isdigit():
        raise ValueError(f"expected 'SEGMENT:SECURITY_ID' (e.g. NSE_EQ:2885), got {ref!r}")
    return seg, sid


def dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def dec0(v: Any) -> Decimal:
    return dec(v) or Decimal(0)


def ist_to_utc(text: str | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> datetime | None:
    if not text:
        return None
    try:
        local = datetime.strptime(text.strip(), fmt).replace(tzinfo=IST)
    except ValueError:
        return None
    return None if local.year < 2000 else local.astimezone(UTC)  # "01/01/1980" = no trade


# --- account ------------------------------------------------------------------------------


def token_expiry(profile: dict[str, Any]) -> datetime | None:
    """`tokenValidity` looks like "30/03/2025 15:37" (IST)."""
    return ist_to_utc(profile.get("tokenValidity"), "%d/%m/%Y %H:%M")


def account(profile: dict[str, Any]) -> AccountInfo:
    return AccountInfo(BROKER, str(profile["dhanClientId"]), "Dhan", "INR")


def funds(raw: dict[str, Any], account_id: str, as_of: datetime) -> Funds:
    return Funds(
        account_id=account_id,
        currency="INR",
        available=dec0(raw.get("availabelBalance")),  # sic, Dhan's spelling
        used_margin=dec0(raw.get("utilizedAmount")),
        equity=None,
        as_of=as_of,
        raw=raw,
    )


def positions(rows: list[dict[str, Any]], account_id: str) -> list[Position]:
    out = []
    for r in rows:
        qty = dec0(r.get("netQty"))
        if qty == 0:  # closed for the day; not an open position
            continue
        avg = dec0(r.get("buyAvg") if qty > 0 else r.get("sellAvg"))
        out.append(
            Position(
                account_id=account_id,
                symbol=str(r.get("tradingSymbol") or r.get("securityId")),
                broker_instrument_id=f"{r.get('exchangeSegment')}:{r.get('securityId')}",
                quantity=qty,
                avg_price=avg,
                currency="INR",
                unrealized_pnl=dec(r.get("unrealizedProfit")),
                realized_pnl=dec(r.get("realizedProfit")),
                product=r.get("productType"),
                raw=r,
            )
        )
    return out


def holdings(rows: list[dict[str, Any]], account_id: str) -> list[Holding]:
    # Holdings report exchange "ALL" (not a segment), so no segment-qualified ref is possible.
    return [
        Holding(
            account_id=account_id,
            symbol=str(r.get("tradingSymbol")),
            broker_instrument_id=None,
            isin=r.get("isin"),
            quantity=dec0(r.get("totalQty")),
            available_quantity=dec0(r.get("availableQty")),
            avg_cost=dec0(r.get("avgCostPrice")),
            last_price=dec(r.get("lastTradedPrice")),
            raw=r,
        )
        for r in rows
    ]


def order(r: dict[str, Any], account_id: str) -> BrokerOrder:
    raw_status = str(r.get("orderStatus") or "UNKNOWN")
    placed = ist_to_utc(r.get("createTime")) or ist_to_utc(r.get("exchangeTime"))
    return BrokerOrder(
        account_id=account_id,
        order_id=str(r["orderId"]),
        symbol=str(r.get("tradingSymbol") or r.get("securityId")),
        side=Side(str(r.get("transactionType"))),
        order_type=ORDER_TYPES.get(str(r.get("orderType")), OrderType.LIMIT),
        quantity=dec0(r.get("quantity")),
        filled_quantity=dec0(r.get("filledQty")),
        status=ORDER_STATUS.get(raw_status, OrderStatus.UNKNOWN),
        raw_status=raw_status,
        placed_at=placed or datetime.now(UTC),
        price=dec(r.get("price")),
        avg_fill_price=dec(r.get("averageTradedPrice")),
        updated_at=ist_to_utc(r.get("updateTime")),
        correlation_id=r.get("correlationId") or None,
        raw=r,
    )


def trade(r: dict[str, Any], account_id: str) -> BrokerTrade:
    return BrokerTrade(
        account_id=account_id,
        trade_id=str(r.get("exchangeTradeId")),
        order_id=str(r["orderId"]),
        symbol=str(r.get("tradingSymbol") or r.get("customSymbol") or r.get("securityId")),
        broker_instrument_id=f"{r.get('exchangeSegment')}:{r.get('securityId')}",
        side=Side(str(r.get("transactionType"))),
        quantity=dec0(r.get("tradedQuantity")),
        price=dec0(r.get("tradedPrice")),
        executed_at=ist_to_utc(r.get("exchangeTime"))
        or ist_to_utc(r.get("createTime"))
        or datetime.now(UTC),
        raw=r,
    )


def one_or_first(payload: Any) -> dict[str, Any] | None:
    """Single-order endpoints return an object (SDK) or a one-element list (docs)."""
    if isinstance(payload, list):
        return payload[0] if payload else None
    return payload if isinstance(payload, dict) else None


# --- market data --------------------------------------------------------------------------


def quotes(payload: dict[str, Any], as_of: datetime) -> list[Quote]:
    out = []
    for seg, by_id in (payload.get("data") or {}).items():
        for sid, q in (by_id or {}).items():
            depth = q.get("depth") or {}
            buy = (depth.get("buy") or [{}])[0]
            sell = (depth.get("sell") or [{}])[0]
            ts = ist_to_utc(q.get("last_trade_time"), "%d/%m/%Y %H:%M:%S") or as_of
            out.append(
                Quote(
                    f"{seg}:{sid}",
                    ts,
                    last=dec(q.get("last_price")),
                    bid=dec(buy.get("price")),
                    ask=dec(sell.get("price")),
                    volume=dec(q.get("volume")),
                )
            )
    return out


def candles(payload: dict[str, Any]) -> list[Candle]:
    """Parallel arrays → candles. `timestamp` is UTC epoch seconds."""
    ts = payload.get("timestamp") or []
    o, h, lo, c = (payload.get(k) or [] for k in ("open", "high", "low", "close"))
    vol, oi = payload.get("volume") or [], payload.get("open_interest") or []
    if not (len(ts) == len(o) == len(h) == len(lo) == len(c)):
        raise ValueError("Dhan candle arrays have different lengths")
    out = []
    for i, t in enumerate(ts):
        out.append(
            Candle(
                datetime.fromtimestamp(int(t), UTC),
                dec0(o[i]),
                dec0(h[i]),
                dec0(lo[i]),
                dec0(c[i]),
                dec0(vol[i] if i < len(vol) else 0),
                dec(oi[i]) if i < len(oi) else None,
            )
        )
    return out


def _leg(raw: dict[str, Any] | None, segment: str | None) -> OptionLeg | None:
    if not raw:
        return None
    g = raw.get("greeks") or {}
    sid = raw.get("security_id")
    return OptionLeg(
        broker_instrument_id=f"{segment}:{sid}" if sid is not None and segment else None,
        last_price=dec(raw.get("last_price")),
        oi=dec(raw.get("oi")),
        volume=dec(raw.get("volume")),
        implied_volatility=dec(raw.get("implied_volatility")),
        delta=dec(g.get("delta")),
        gamma=dec(g.get("gamma")),
        theta=dec(g.get("theta")),
        vega=dec(g.get("vega")),
        previous_close=dec(raw.get("previous_close_price")),
        previous_oi=dec(raw.get("previous_oi")),
        bid=dec(raw.get("top_bid_price")),
        ask=dec(raw.get("top_ask_price")),
    )


def option_chain(
    payload: dict[str, Any],
    underlying: str,
    expiry: date,
    legs_segment: str | None,
    as_of: datetime,
) -> OptionChain:
    data = payload.get("data") or {}
    strikes = [
        OptionStrike(dec0(k), _leg(v.get("ce"), legs_segment), _leg(v.get("pe"), legs_segment))
        for k, v in (data.get("oc") or {}).items()
    ]
    strikes.sort(key=lambda s: s.strike)
    return OptionChain(
        underlying=underlying,
        expiry=expiry,
        underlying_price=dec(data.get("last_price")),
        strikes=strikes,
        as_of=as_of,
    )


# --- instrument master --------------------------------------------------------------------

# Detailed CSV column -> compact CSV column (both formats are accepted).
_COMPACT = {
    "EXCH_ID": "SEM_EXM_EXCH_ID",
    "SEGMENT": "SEM_SEGMENT",
    "SECURITY_ID": "SEM_SMST_SECURITY_ID",
    "INSTRUMENT": "SEM_INSTRUMENT_NAME",
    "SYMBOL_NAME": "SM_SYMBOL_NAME",
    "DISPLAY_NAME": "SEM_CUSTOM_SYMBOL",
    "LOT_SIZE": "SEM_LOT_UNITS",
    "SM_EXPIRY_DATE": "SEM_EXPIRY_DATE",
    "STRIKE_PRICE": "SEM_STRIKE_PRICE",
    "OPTION_TYPE": "SEM_OPTION_TYPE",
    "TICK_SIZE": "SEM_TICK_SIZE",
}


def _col(row: dict[str, str], name: str) -> str:
    v = row.get(name)
    if v is None:
        v = row.get(_COMPACT.get(name, ""), "")
    return (v or "").strip()


def instrument_row(row: dict[str, str]) -> tuple[InstrumentInfo, dict[str, Any]] | None:
    """One master row → (normalized instrument, dhan.instruments values), or None to skip."""
    row = {k.lstrip("﻿").strip(): v for k, v in row.items() if k}
    segment = CSV_SEGMENTS.get((_col(row, "EXCH_ID"), _col(row, "SEGMENT")))
    sid = _col(row, "SECURITY_ID")
    if not segment or not sid:
        return None
    instrument = _col(row, "INSTRUMENT") or "UNKNOWN"
    symbol = (
        _col(row, "DISPLAY_NAME")
        or row.get("SEM_TRADING_SYMBOL", "").strip()
        or _col(row, "SYMBOL_NAME")
        or sid
    )
    expiry_txt = _col(row, "SM_EXPIRY_DATE")
    expiry = None
    if expiry_txt and not expiry_txt.startswith("0001"):
        try:
            expiry = date.fromisoformat(expiry_txt[:10])
        except ValueError:
            expiry = None
    strike = dec(_col(row, "STRIKE_PRICE"))
    strike = strike if strike is not None and strike > 0 else None
    opt_raw = _col(row, "OPTION_TYPE")
    opt = opt_raw if opt_raw in ("CE", "PE") else None
    lot = dec(_col(row, "LOT_SIZE")) or Decimal(1)
    tick = dec(_col(row, "TICK_SIZE")) or Decimal("0.05")
    underlying = row.get("UNDERLYING_SYMBOL", "").strip() or None
    info = InstrumentInfo(
        broker=BROKER,
        broker_instrument_id=f"{segment}:{sid}",
        symbol=symbol[:100],
        instrument_type=instrument[:32],
        currency="INR",
        lot_size=lot,
        tick_size=tick,
        exchange=_col(row, "EXCH_ID")[:16] or None,
        segment=segment,
        expiry=expiry,
        strike=strike,
        option_type=opt,
        underlying=underlying,
    )
    dhan_row = {
        "exchange_segment": segment,
        "security_id": sid,
        "trading_symbol": symbol[:100],
        "instrument": instrument[:32],
        "lot_size": lot,
        "tick_size": tick,
        "expiry_date": expiry,
        "strike_price": strike,
        "option_type": opt,
        "underlying_security_id": (row.get("UNDERLYING_SECURITY_ID") or "").strip()[:32] or None,
        "raw": {k: v for k, v in row.items() if v not in (None, "")},
    }
    return info, dhan_row
