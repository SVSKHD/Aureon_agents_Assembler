"""DhanHQ v2 live market feed: binary packet parser + resilient WebSocket client.

The packet layouts come from the v2 docs and the official SDK's struct formats. Packets are
little-endian and packed, and every one starts with an 8-byte header (`<BHBI`):

| Byte(s) | Field |
| --- | --- |
| 0 | response code |
| 1-2 | message length |
| 3 | exchange segment code |
| 4-7 | security id |

| Code | Packet | Bytes |
| --- | --- | --- |
| 2 | Ticker: LTP, LTT | 16 |
| 4 | Quote: LTP, LTQ, LTT, ATP, volume, total sell/buy qty, open/close/high/low | 50 |
| 5 | OI | 12 |
| 6 | Prev close + prev OI | 16 |
| 8 | Full: quote fields, OI, OI hi/lo, OHLC, 5-level depth | 162 |
| 50 | Disconnect + reason | 10 |

Codes 1 (index) and 7 (market status) have no published layout; they are counted and skipped,
never guessed. A frame may hold several packets, so the parser walks them by known size.

Connection rules (docs): at most 5000 instruments per connection and 100 per subscribe
message. The server pings every 10 s; the websockets library answers the pings.

**Security:** the URL carries the access token, so it is never logged.

**Tokens** are read at every connect (a callable is accepted), so an expired login falls back
to a valid manual token without a restart, and a replaced token is picked up on the next
reconnect. Only a short sha256 prefix of the token in use is kept (`stats.token_fp`) so the
worker can tell "the token that failed" from "a new token".
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import random
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlencode

from app.agents.trading.dhan.parse import SEGMENT_BY_CODE, SEGMENT_CODES
from app.core.logging import get_logger

log = get_logger(__name__)

HEADER = struct.Struct("<BHBI")
TICKER = struct.Struct("<fI")  # after header
QUOTE = struct.Struct("<fHIfIIIffff")
OI = struct.Struct("<I")
PREV_CLOSE = struct.Struct("<fI")
FULL = struct.Struct("<fHIfIIIIIIffff")
DEPTH = struct.Struct("<IIHHff")
DISCONNECT = struct.Struct("<H")
SIZES = {2: 16, 4: 50, 5: 12, 6: 16, 8: 162, 50: 10}
UNDOCUMENTED = {1, 3, 7}

SUBSCRIBE = {"ticker": 15, "quote": 17, "full": 21}
DISCONNECT_REQUEST = 12
MAX_PER_MESSAGE = 100
MAX_PER_CONNECTION = 5000
AUTH_REASONS = {806, 807, 808, 809, 810}  # no data plan / token expired / invalid creds
TOO_MANY = 805

Mode = Literal["ticker", "quote", "full"]


def _px(v: float) -> Decimal:
    """float32 → Decimal at float32 precision (≈7 significant digits)."""
    return Decimal(f"{v:.7g}")


def _ts(epoch: int) -> datetime | None:
    return datetime.fromtimestamp(epoch, UTC) if epoch > 0 else None


@dataclass(slots=True)
class FeedPacket:
    kind: str  # ticker | quote | oi | prev_close | full | disconnect
    segment: str | None
    security_id: str
    ltp: Decimal | None = None
    ltt: datetime | None = None
    ltq: int | None = None
    atp: Decimal | None = None
    volume: int | None = None
    total_buy_qty: int | None = None
    total_sell_qty: int | None = None
    open: Decimal | None = None
    close: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    oi: int | None = None
    oi_day_high: int | None = None
    oi_day_low: int | None = None
    prev_close: Decimal | None = None
    prev_oi: int | None = None
    depth: list[dict[str, Any]] | None = None
    reason: int | None = None

    @property
    def key(self) -> str:
        return f"{self.segment}:{self.security_id}"

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in self.__slots__:
            v = getattr(self, name)
            if v is None:
                continue
            out[name] = (
                str(v)
                if isinstance(v, Decimal)
                else (v.isoformat() if isinstance(v, datetime) else v)
            )
        return out


def parse_frame(data: bytes) -> tuple[list[FeedPacket], int]:
    """Parse every complete packet in a binary frame. Returns (packets, skipped_count)."""
    out: list[FeedPacket] = []
    skipped = 0
    off = 0
    while off + HEADER.size <= len(data):
        code, _length, seg_code, sid = HEADER.unpack_from(data, off)
        size = SIZES.get(code)
        if size is None:
            # Unknown or undocumented packet: we can't know its length, so stop here.
            skipped += 1
            break
        if off + size > len(data):
            skipped += 1
            break
        p = FeedPacket(kind="", segment=SEGMENT_BY_CODE.get(seg_code), security_id=str(sid))
        body = off + HEADER.size
        if code == 2:
            ltp, ltt = TICKER.unpack_from(data, body)
            p.kind, p.ltp, p.ltt = "ticker", _px(ltp), _ts(ltt)
        elif code == 6:
            prev, prev_oi = PREV_CLOSE.unpack_from(data, body)
            p.kind, p.prev_close, p.prev_oi = "prev_close", _px(prev), prev_oi
        elif code == 5:
            (p.oi,) = OI.unpack_from(data, body)
            p.kind = "oi"
        elif code == 4:
            (ltp, ltq, ltt, atp, vol, sell, buy, o, c, h, lo) = QUOTE.unpack_from(data, body)
            p.kind = "quote"
            p.ltp, p.ltq, p.ltt, p.atp = _px(ltp), ltq, _ts(ltt), _px(atp)
            p.volume, p.total_sell_qty, p.total_buy_qty = vol, sell, buy
            p.open, p.close, p.high, p.low = _px(o), _px(c), _px(h), _px(lo)  # wire: O C H L
        elif code == 8:
            (ltp, ltq, ltt, atp, vol, sell, buy, oi, oi_hi, oi_lo, o, c, h, lo) = FULL.unpack_from(
                data, body
            )
            p.kind = "full"
            p.ltp, p.ltq, p.ltt, p.atp = _px(ltp), ltq, _ts(ltt), _px(atp)
            p.volume, p.total_sell_qty, p.total_buy_qty = vol, sell, buy
            p.oi, p.oi_day_high, p.oi_day_low = oi, oi_hi, oi_lo
            p.open, p.close, p.high, p.low = _px(o), _px(c), _px(h), _px(lo)
            p.depth = []
            for k in range(5):
                bq, aq, bo, ao, bp, ap = DEPTH.unpack_from(data, off + 62 + 20 * k)
                p.depth.append(
                    {
                        "bid_qty": bq,
                        "ask_qty": aq,
                        "bid_orders": bo,
                        "ask_orders": ao,
                        "bid": str(_px(bp)),
                        "ask": str(_px(ap)),
                    }
                )
        elif code == 50:
            (p.reason,) = DISCONNECT.unpack_from(data, body)
            p.kind = "disconnect"
        out.append(p)
        off += size
    return out, skipped


def subscribe_messages(
    instruments: list[tuple[str, str]], mode: Mode, unsubscribe: bool = False
) -> list[str]:
    code = SUBSCRIBE[mode] + (1 if unsubscribe else 0)
    msgs = []
    for i in range(0, len(instruments), MAX_PER_MESSAGE):
        batch = instruments[i : i + MAX_PER_MESSAGE]
        msgs.append(
            json.dumps(
                {
                    "RequestCode": code,
                    "InstrumentCount": len(batch),
                    "InstrumentList": [
                        {"ExchangeSegment": s, "SecurityId": sid} for s, sid in batch
                    ],
                }
            )
        )
    return msgs


def parse_instruments(refs: list[str]) -> list[tuple[str, str]]:
    out = []
    for r in refs:
        seg, _, sid = r.partition(":")
        if seg not in SEGMENT_CODES or not sid.isdigit():
            raise ValueError(f"bad feed instrument {r!r}; expected SEGMENT:SECURITY_ID")
        out.append((seg, sid))
    if len(out) > MAX_PER_CONNECTION:
        raise ValueError(f"at most {MAX_PER_CONNECTION} instruments per connection")
    return list(dict.fromkeys(out))


@dataclass
class FeedStats:
    connected: bool = False
    connects: int = 0
    reconnects: int = 0
    packets: int = 0
    skipped: int = 0
    dropped: int = 0
    last_packet_at: float | None = None
    last_disconnect_reason: int | None = None
    auth_failed: bool = False
    last_error: str | None = None
    started_at: float = field(default_factory=time.time)
    # Connection attempts that ended without data since the last healthy connection.
    consecutive_failures: int = 0
    last_connected_at: float | None = None
    # Instruments subscribed on the CURRENT connection (0 while disconnected).
    subscribed: int = 0
    # sha256 prefix of the token used for the current/last connection, and of the one Dhan
    # refused (never the token itself).
    token_fp: str | None = None
    failed_token_fp: str | None = None
    reconnects_requested: int = 0


Connector = Callable[[str], Awaitable[Any]]
TokenSource = str | Callable[[], str | None]


def token_fp(token: str | None) -> str | None:
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else None


class DhanFeed:
    """Connects, subscribes, parses, and puts packets on a bounded queue.

    Reconnects with exponential backoff and jitter. If the server reports an auth problem
    (token expired or invalid, no data plan), it stops for good and leaves `stats.auth_failed`
    set: retrying would only hammer Dhan with a dead token.
    """

    def __init__(
        self,
        *,
        url: str,
        client_id: str,
        access_token: TokenSource,
        instruments: list[tuple[str, str]],
        mode: Mode,
        queue_max: int,
        max_backoff: float,
        connector: Connector | None = None,
    ) -> None:
        self._base = url.rstrip("/")
        self._client_id = client_id
        self._token = access_token
        self.instruments = instruments
        self.mode = mode
        self.queue: asyncio.Queue[FeedPacket] = asyncio.Queue(maxsize=queue_max)
        self.max_backoff = max_backoff
        self.stats = FeedStats()
        self._connector = connector
        self._reconnect = asyncio.Event()

    def _current_token(self) -> str | None:
        return self._token() if callable(self._token) else self._token

    def _url(self, token: str) -> str:
        return f"{self._base}?" + urlencode(
            {"version": 2, "token": token, "clientId": self._client_id, "authType": 2}
        )

    def request_reconnect(self) -> None:
        """Drop the current connection and reconnect now (re-reading the token)."""
        self.stats.reconnects_requested += 1
        self._reconnect.set()

    def token_changed(self) -> bool:
        """After an auth failure: is there now a different token than the one Dhan refused?"""
        fp = token_fp(self._current_token())
        return fp is not None and fp != self.stats.failed_token_fp

    def reset_auth(self) -> None:
        """Allow `run` again after an auth failure (only once the token has changed)."""
        self.stats.auth_failed = False
        self.stats.last_disconnect_reason = None
        self.stats.consecutive_failures = 0

    def __repr__(self) -> str:
        return f"DhanFeed(instruments={len(self.instruments)}, mode={self.mode})"

    async def _connect(self, token: str) -> Any:
        url = self._url(token)
        if self._connector is not None:
            return await self._connector(url)
        from websockets.asyncio.client import connect

        return await connect(url, ping_interval=None, max_size=2**20, open_timeout=15)

    def _enqueue(self, p: FeedPacket) -> None:
        if self.queue.full():  # bounded: keep the newest data, count what we dropped
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.stats.dropped += 1
        self.queue.put_nowait(p)

    async def run(self, stopping: asyncio.Event) -> None:
        backoff = 1.0
        while not stopping.is_set() and not self.stats.auth_failed:
            token = self._current_token()
            if not token:
                # No usable token at all: same as an auth failure (nothing to retry with).
                self.stats.auth_failed = True
                self.stats.last_error = "no_token"
                self.stats.failed_token_fp = None
                log.error("dhan_feed_no_token")
                break
            self.stats.token_fp = token_fp(token)
            self._reconnect.clear()
            ws = None
            got_data = False
            requested = False
            try:
                ws = await self._connect(token)
                self.stats.connected = True
                self.stats.connects += 1
                self.stats.last_connected_at = time.time()
                for msg in subscribe_messages(self.instruments, self.mode):
                    await ws.send(msg)
                # One subscription set per connection: a reconnect is a new socket, so
                # re-subscribing can never double-subscribe.
                self.stats.subscribed = len(self.instruments)
                log.info("dhan_feed_connected", instruments=len(self.instruments), mode=self.mode)
                while not stopping.is_set():
                    recv = asyncio.ensure_future(ws.recv())
                    stop = asyncio.ensure_future(stopping.wait())
                    again = asyncio.ensure_future(self._reconnect.wait())
                    done, _ = await asyncio.wait(
                        {recv, stop, again}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if stop in done or again in done:
                        recv.cancel()
                        stop.cancel()
                        again.cancel()
                        requested = again in done and not stopping.is_set()
                        break
                    stop.cancel()
                    again.cancel()
                    frame = recv.result()
                    if isinstance(frame, str):
                        continue  # server only sends binary; ignore stray text
                    packets, skipped = parse_frame(frame)
                    self.stats.skipped += skipped
                    for p in packets:
                        if p.kind == "disconnect":
                            self._on_disconnect(p.reason)
                            raise _ServerClosed
                        self.stats.packets += 1
                        self.stats.last_packet_at = time.time()
                        self._enqueue(p)
                    if packets and not got_data:
                        got_data, backoff = True, 1.0  # healthy again
                        self.stats.consecutive_failures = 0
            except _ServerClosed:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network drop, handshake failure, bad frame
                self.stats.last_error = type(exc).__name__
                log.warning("dhan_feed_dropped", error=type(exc).__name__)
            finally:
                self.stats.connected = False
                self.stats.subscribed = 0
                if ws is not None:
                    with contextlib.suppress(Exception):
                        if stopping.is_set() or requested:
                            await ws.send(json.dumps({"RequestCode": DISCONNECT_REQUEST}))
                        await ws.close()
            if stopping.is_set() or self.stats.auth_failed:
                break
            self.stats.reconnects += 1
            if requested:
                # An explicit reconnect (admin or bounded recovery): no backoff growth.
                self.stats.last_error = "reconnect_requested"
                log.info("dhan_feed_reconnect_requested")
                backoff = 1.0
                continue
            if not got_data:
                self.stats.consecutive_failures += 1
            wait = min(self.max_backoff, backoff) * random.uniform(0.8, 1.2)  # noqa: S311
            if self.stats.last_disconnect_reason == TOO_MANY:
                wait = self.max_backoff  # another session took our slot; back off hard
            log.info("dhan_feed_reconnecting", in_seconds=round(wait, 1))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), wait)
            backoff = min(self.max_backoff, backoff * 2)

    def _on_disconnect(self, reason: int | None) -> None:
        self.stats.last_disconnect_reason = reason
        if reason in AUTH_REASONS:
            self.stats.auth_failed = True
            self.stats.failed_token_fp = self.stats.token_fp
            log.error("dhan_feed_auth_failed", reason=reason)
        else:
            log.warning("dhan_feed_server_disconnect", reason=reason)


class _ServerClosed(Exception):
    pass
