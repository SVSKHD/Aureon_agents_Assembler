"""Dhan stays read-only (docs/11-dhan.md): pinned by tests, not only by convention.

The MT5 equivalent is in test_prod_integration_unit.py. Here:

- no order-placing name exists anywhere in the Dhan package or on DhanAdapter;
- every request the client builds is a GET or one of the market-data POSTs;
- the client refuses a write before any byte leaves (assert_read_only in _request);
- the LIVE flag cannot load without the Phase G sign-off.
"""

from __future__ import annotations

import ast
from pathlib import Path

import httpx
import pytest

from app.agents.trading.dhan.adapter import DhanAdapter
from app.agents.trading.dhan.client import (
    READ_POST_PATHS,
    DhanClient,
    ReadOnlyViolation,
    assert_read_only,
)
from app.core.config import Settings
from tests.test_prod_integration_unit import ORDER_NAME

DHAN = Path(__file__).resolve().parents[1] / "app" / "agents" / "trading" / "dhan"


def test_no_order_methods_in_the_dhan_package() -> None:
    files = sorted(DHAN.rglob("*.py"))
    assert files
    for f in files:
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                assert not ORDER_NAME.search(node.name), f"{f.name}: {node.name}"
    for cls in (DhanAdapter, DhanClient):
        assert not [m for m in dir(cls) if ORDER_NAME.search(m)], cls


def test_every_request_in_the_client_source_is_a_read() -> None:
    """Each `self._request(<method>, <path>, ...)` uses GET, or POST to a market-data path."""
    tree = ast.parse((DHAN / "client.py").read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "_request"
    ]
    assert len(calls) >= 8
    for call in calls:
        method = call.args[0]
        assert isinstance(method, ast.Constant), ast.unparse(call)
        if method.value == "POST":
            path = call.args[1]
            assert isinstance(path, ast.Constant) and path.value in READ_POST_PATHS
        else:
            assert method.value == "GET", ast.unparse(call)
    # No direct HTTP verb calls that bypass _request.
    verbs = {"post", "put", "patch", "delete"}
    direct = [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in verbs]
    assert not direct


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/orders"),
        ("POST", "/orders/slicing"),
        ("PUT", "/orders/123"),
        ("DELETE", "/orders/123"),
        ("PATCH", "/positions"),
        ("POST", "/super/orders"),
        ("POST", "/forever/orders"),
        ("POST", "/killswitch"),
    ],
)
async def test_the_client_refuses_writes_before_any_request(method: str, path: str) -> None:
    sent: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(req)
        return httpx.Response(200, json={})

    with pytest.raises(ReadOnlyViolation):
        assert_read_only(method, path)
    client = DhanClient(
        "1000000001",
        "token",
        base_url="https://dhan.test/v2",
        limiter=None,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ReadOnlyViolation):
        await client._request(method, path, "non_trading", {"quantity": 1})
    assert not sent
    await client.aclose()


async def test_reads_still_go_through() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = DhanClient(
        "1000000001",
        "token",
        base_url="https://dhan.test/v2",
        limiter=None,
        transport=httpx.MockTransport(handler),
    )
    assert await client.orders() == []
    for path in sorted(READ_POST_PATHS):
        assert_read_only("POST", path)
    assert_read_only("get", "/orders/42")
    await client.aclose()


def test_live_cannot_load_without_phase_g(settings: Settings) -> None:
    with pytest.raises(ValueError, match="PHASE_G_APPROVED"):
        Settings.model_validate({**settings.model_dump(), "TRADING_LIVE_ENABLED": True})
