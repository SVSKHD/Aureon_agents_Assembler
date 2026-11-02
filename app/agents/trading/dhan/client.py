"""Low-level DhanHQ v2 REST client: READ-ONLY endpoints only.

- **No order endpoints:** there is no place, modify or cancel method here, and no generic
  "call any URL" helper. Order execution is Trading Phase G, in a separate module behind the
  LIVE gate.
- **Credentials:** the access token comes from env (Settings) and is sent only in the
  `access-token` header. It is never logged or put in error messages.
- **Rate limits** use the documented table. Buckets are shared across processes via Redis
  (BrokerRateLimiter):
  - non-trading APIs: 20/s
  - data APIs (charts): 5/s, 100 000/day
  - quote APIs: 1/s
  - option chain: 1 per 3 s
- **Retries:** rate-limit (DH-904/805) and transient network errors are retried with backoff
  (read calls are idempotent). Auth failures (DH-901, 807-810) raise CredentialsExpired
  straight away.
- **Request ids:** every request gets an id (`aureon-…`). It goes into our logs and into
  order_events written by reconciliation, so a stored row can be traced to the call that
  produced it.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import httpx

from app.agents.trading.base import BrokerError, CredentialsExpired
from app.agents.trading.ratelimit import BrokerRateLimiter, Window
from app.core.logging import get_logger

log = get_logger(__name__)

BUCKETS: dict[str, list[Window]] = {
    "non_trading": [Window(1, 20)],
    "data": [Window(1, 5), Window(86_400, 100_000)],
    "quote": [Window(1, 1)],
    "option_chain": [Window(3, 1)],
}
AUTH_CODES = {"DH-901", "807", "808", "809", "810"}
RETRY_CODES = {"DH-904", "805", "DH-908", "DH-909"}
MAX_ATTEMPTS = 3


class DhanApiError(BrokerError):
    def __init__(self, code: str | None, message: str, http_status: int | None = None) -> None:
        super().__init__(f"Dhan {code or http_status}: {message}"[:300])
        self.code = code
        self.http_status = http_status


def new_request_id() -> str:
    return f"aureon-{secrets.token_hex(8)}"


class DhanClient:
    def __init__(
        self,
        client_id: str,
        access_token: str,
        *,
        base_url: str,
        limiter: BrokerRateLimiter | None,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self._token = access_token
        self.limiter = limiter
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "access-token": access_token,
                "client-id": client_id,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        self.last_request_id: str | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    def __repr__(self) -> str:  # never print the token
        return f"DhanClient(client_id={self.client_id!r})"

    async def _request(
        self, method: str, path: str, bucket: str, body: dict[str, Any] | None = None
    ) -> Any:
        request_id = new_request_id()
        self.last_request_id = request_id
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.limiter is not None:
                await self.limiter.acquire(bucket)
            cause: Exception | None = None
            try:
                resp = await self._http.request(method, path, json=body)
            except httpx.TimeoutException as exc:
                err = DhanApiError("timeout", "Dhan did not respond in time")
                cause = exc
            except httpx.TransportError as exc:
                err = DhanApiError("network", "could not reach Dhan")
                cause = exc
            else:
                if resp.status_code < 400:
                    log.debug(
                        "dhan_request", request_id=request_id, path=path, status=resp.status_code
                    )
                    return resp.json() if resp.content else None
                err = _error(resp)
                if err.code in AUTH_CODES or resp.status_code == 401:
                    raise CredentialsExpired(
                        f"Dhan rejected the access token ({err.code or resp.status_code}); "
                        "generate a new one on web.dhan.co"
                    ) from None
                if err.code not in RETRY_CODES and resp.status_code not in (429, 502, 503, 504):
                    raise err
            log.warning(
                "dhan_retry", request_id=request_id, path=path, attempt=attempt, code=err.code
            )
            if attempt == MAX_ATTEMPTS:
                raise err from cause
            await asyncio.sleep(0.5 * 2 ** (attempt - 1))
        raise AssertionError("unreachable")

    # --- account / portfolio (non-trading bucket) --------------------------------------

    async def profile(self) -> dict[str, Any]:
        out: dict[str, Any] = await self._request("GET", "/profile", "non_trading")
        return out

    async def fund_limit(self) -> dict[str, Any]:
        out: dict[str, Any] = await self._request("GET", "/fundlimit", "non_trading")
        return out

    async def holdings(self) -> list[dict[str, Any]]:
        return await self._list("/holdings")

    async def positions(self) -> list[dict[str, Any]]:
        return await self._list("/positions")

    async def orders(self) -> list[dict[str, Any]]:
        return await self._list("/orders")

    async def order(self, order_id: str) -> Any:
        return await self._request("GET", f"/orders/{_safe(order_id)}", "non_trading")

    async def order_by_correlation(self, correlation_id: str) -> Any:
        return await self._request(
            "GET", f"/orders/external/{_safe(correlation_id)}", "non_trading"
        )

    async def trades(self) -> list[dict[str, Any]]:
        return await self._list("/trades")

    async def trades_for_order(self, order_id: str) -> list[dict[str, Any]]:
        return await self._list(f"/trades/{_safe(order_id)}")

    async def _list(self, path: str) -> list[dict[str, Any]]:
        out = await self._request("GET", path, "non_trading")
        # Empty books come back as [] or (sometimes) null.
        return list(out or [])

    # --- market data ---------------------------------------------------------------------

    async def market_quote(self, by_segment: dict[str, list[int]]) -> dict[str, Any]:
        out: dict[str, Any] = await self._request("POST", "/marketfeed/quote", "quote", by_segment)
        return out

    async def historical_daily(self, body: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = await self._request("POST", "/charts/historical", "data", body)
        return out

    async def intraday(self, body: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = await self._request("POST", "/charts/intraday", "data", body)
        return out

    async def option_chain(
        self, underlying_id: int, underlying_seg: str, expiry: str
    ) -> dict[str, Any]:
        out: dict[str, Any] = await self._request(
            "POST",
            "/optionchain",
            "option_chain",
            {"UnderlyingScrip": underlying_id, "UnderlyingSeg": underlying_seg, "Expiry": expiry},
        )
        return out

    async def expiry_list(self, underlying_id: int, underlying_seg: str) -> dict[str, Any]:
        out: dict[str, Any] = await self._request(
            "POST",
            "/optionchain/expirylist",
            "option_chain",
            {"UnderlyingScrip": underlying_id, "UnderlyingSeg": underlying_seg},
        )
        return out


def _safe(part: str) -> str:
    if not part or not all(c.isalnum() or c in "-_" for c in part):
        raise ValueError("invalid id")
    return part


def _error(resp: httpx.Response) -> DhanApiError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    code = body.get("errorCode") or body.get("errorType") or None
    message = str(body.get("errorMessage") or resp.reason_phrase or "error")
    return DhanApiError(str(code) if code else None, message, resp.status_code)
