"""The redactor (app/ai/redact.py): what never reaches Claude, and what still does."""

from __future__ import annotations

import pytest

from app.ai.redact import Redactor, redact
from app.core import logging as app_logging


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("mail ravi.k@aquakart.in now", "mail [EMAIL] now"),
        ("call +91 98480 22338 or 9848022338", "call [PHONE] or [PHONE]"),
        ("office 040-23456789", "office [PHONE]"),
        ("Address: 12-3 Banjara Hills, Hyderabad 500034\nnext", "Address: [ADDRESS]\nnext"),
        ("ship to: Plot 4, Madhapur", "ship to: [ADDRESS]"),
        ("Plot 4, Madhapur, 500081 today", "Plot 4, Madhapur, [ADDRESS] today"),
        ("client id: 1100234567", "client id: [ACCOUNT_ID]"),
        ("Account No. 50100012345678", "Account No. [ACCOUNT_ID]"),
        ("demat 1208160000012345", "demat [ACCOUNT_ID]"),
        ("PAN ABCDE1234F", "PAN [ACCOUNT_ID]"),
        ("aadhaar 1234 5678 9012", "aadhaar [ACCOUNT_ID]"),
        ("IFSC HDFC0001234", "IFSC [ACCOUNT_ID]"),
        ("card 4111 1111 1111 1111 ok", "card [ACCOUNT_ID] ok"),
        ("Customer: Suresh Babu bought 2", "Customer: [NAME] bought 2"),
        ("thanks Mr. Rao", "thanks Mr. [NAME]"),
        ("key sk-ant-api03-abcdefghijklmnopqrstuv", "key [REDACTED]"),
        ("Authorization: Bearer abcdefghijklmnop", "Authorization: Bearer [REDACTED]"),
        ("db postgres://app:hunter22@db:5432/x", "db postgres://app:[REDACTED]@db:5432/x"),
    ],
)
def test_redacts(text: str, want: str) -> None:
    assert redact(text) == want


@pytest.mark.parametrize(
    "text",
    [
        "BUY 120 RELIANCE at 2850.50, stop 2810, target 2930",
        "NIFTY 25500 CE expiry 2026-11-27, OI 1234567, volume 123456789",
        "P&L today -4,250.75 INR on 3 positions",
        "order count 14, revenue 2,45,000",
    ],
)
def test_keeps_trading_and_business_numbers(text: str) -> None:
    assert redact(text) == text


def test_known_names_are_removed_everywhere_and_idempotent() -> None:
    r = Redactor(["Lakshmi Narayana", "Pawan"])
    out = r.text("Lakshmi Narayana asked; pawan replied to LAKSHMI NARAYANA")
    assert out == "[NAME] asked; [NAME] replied to [NAME]"
    assert r.text(out) == out
    r.add_names(["Ab"])  # too short to be a name: ignored, never redacts "about"
    assert r.text("about") == "about"


def test_registered_secret_values_are_removed() -> None:
    app_logging.register_secret_values(["opaque-token-value-123"])
    try:
        assert redact("token opaque-token-value-123 here") == "token [REDACTED] here"
    finally:
        app_logging.register_secret_values([])


def test_only_user_side_content_and_text_blocks_change() -> None:
    r = Redactor()
    thinking = {"type": "thinking", "thinking": "x@y.com", "signature": "sig"}
    tool_use = {"type": "tool_use", "id": "t", "name": "n", "input": {"email": "x@y.com"}}
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "x@y.com"}]},
        {"role": "assistant", "content": [thinking, tool_use, {"type": "text", "text": "x@y.com"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [
            {"type": "text", "text": "x@y.com"}]}]},
    ]  # fmt: skip
    out = r.messages(msgs)
    assert out[0]["content"][0]["text"] == "[EMAIL]"
    assert out[1] == msgs[1]  # Claude's turn is replayed byte-for-byte
    assert out[2]["content"][0]["content"][0]["text"] == "[EMAIL]"
