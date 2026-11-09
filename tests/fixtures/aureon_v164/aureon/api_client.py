"""Reusable, opt-in client for an authenticated Aureon Assembler HTTP API.

This is a consumer, not an API server. Routes are configurable because the
Assembler endpoint contract has not been verified. No trade/order endpoints
are included intentionally.
"""
from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import urljoin, urlsplit

import requests


class APIClientError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, transient: bool = False):
        super().__init__(msg); self.status = status; self.transient = transient
    """A network, HTTP, or API-response error with secrets omitted."""


class AssemblerClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10,
        session: requests.Session | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("https", "http") or not parts.netloc:
            raise ValueError("ASSEMBLER_API_URL must be a valid HTTP(S) URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("API URL must not include credentials, query, or fragment")
        host = (parts.hostname or "").lower()
        if parts.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Use HTTPS for remote API connections")
        if not token or not token.strip():
            raise ValueError("ASSEMBLER_API_TOKEN is required")
        if not 0 < timeout <= 120:
            raise ValueError("timeout must be between 0 and 120 seconds")
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "aureon-v1-api-client/1.0",
        })

    @classmethod
    def from_env(cls) -> "AssemblerClient":
        return cls(
            os.environ["ASSEMBLER_API_URL"],
            os.environ["ASSEMBLER_API_TOKEN"],
            timeout=float(os.getenv("ASSEMBLER_API_TIMEOUT", "10")),
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> Any:
        method = method.upper()
        if method not in ("GET", "POST", "PATCH", "PUT", "DELETE"):
            raise ValueError("Unsupported HTTP method")
        if not path or path.startswith(("/", "//")) or "://" in path or "?" in path or "#" in path:
            raise ValueError("Provide a relative API path without query strings")
        if any(p in (".", "..") for p in path.split("/")):
            raise ValueError("API path traversal is not allowed")
        url = urljoin(self.base_url, path)
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc:
            raise ValueError("API URL must stay on the configured host")
        try:
            response = self.session.request(
                method, url, params=params, json=data, timeout=self.timeout,
                allow_redirects=False,
            )
            if 300 <= response.status_code < 400:
                raise APIClientError(f"API returned redirect HTTP {response.status_code}; refusing to forward credentials")
            if response.status_code >= 500:
                raise APIClientError(f"Assembler API server error HTTP {response.status_code}", status=response.status_code, transient=True)
            if response.status_code >= 400:
                raise APIClientError(f"Assembler API rejected request HTTP {response.status_code}", status=response.status_code, transient=False)
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        except APIClientError:
            raise
        except requests.Timeout as exc:
            raise APIClientError("Assembler API request timed out", transient=True) from exc
        except requests.RequestException as exc:
            # Avoid echoing exception strings that may contain sensitive URLs.
            raise APIClientError("Assembler API HTTP/network request failed", transient=True) from exc
        except ValueError as exc:
            raise APIClientError("Assembler API returned invalid JSON") from exc

    def get(self, path: str, *, params: Mapping[str, Any] | None = None, retries: int = 3) -> Any:
        """GET is idempotent: retry with backoff on timeouts / 5xx / connection errors."""
        import time
        last: Exception | None = None
        for attempt in range(retries):
            try:
                return self.request("GET", path, params=params)
            except APIClientError as exc:
                last = exc
                if not exc.transient:          # 4xx / auth / bad JSON: retrying cannot help
                    raise
                time.sleep(0.5 * (2 ** attempt))
        assert last is not None
        raise last

    def post(self, path: str, data: Mapping[str, Any]) -> Any:
        return self.request("POST", path, data=data)

    def patch(self, path: str, data: Mapping[str, Any]) -> Any:
        return self.request("PATCH", path, data=data)
