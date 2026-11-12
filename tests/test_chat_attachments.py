"""Files attached to an AI chat message (app/services/chat_attachments.py), how a turn sends
them, and how the budget estimate counts them. No database, no network."""

from __future__ import annotations

import base64
import io
import os
import time
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openpyxl import Workbook

from app.ai import chat, loop
from app.ai.gateway import IMAGE_EST_CHARS, PDF_PAGE_EST_CHARS, _content_chars
from app.ai.loop import LoopResult
from app.ai.redact import Redactor
from app.core.config import Settings
from app.services import chat_attachments as files

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n1 0 obj << /Type /Pages /Count 2 >>\n" + b"<< /Type /Page >>\n" * 2 + b"%%EOF"


@pytest.fixture
def env(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(update={"DATA_ROOT": tmp_path})


def _xlsx() -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Sales"
    ws.append(["Month", "Orders", "Revenue"])
    ws.append(["Oct", 120, 340000])
    ws.append(["Nov", 98, 275500.5])
    wb.create_sheet("Empty")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- what is accepted -----------------------------------------------------------------------


def test_types_come_from_the_bytes_not_the_name() -> None:
    assert files.sniff(PNG[:16], "x.pdf") == "image/png"
    assert files.sniff(PDF[:16], "photo.png") == "application/pdf"
    assert files.sniff(b"GIF89a" + b"\x00" * 10, "a") == "image/gif"
    assert files.sniff(b"PK\x03\x04" + b"\x00" * 12, "book.xlsx") is not None
    assert files.sniff(b"PK\x03\x04" + b"\x00" * 12, "archive.zip") is None
    assert files.sniff(b"a,b\n1,2", "data.csv") == "text/csv"
    assert files.sniff(b"MZ\x90\x00", "tool.exe") is None
    assert files.clean_filename("../../etc/passwd") == "passwd"


def test_an_image_is_stored_and_sent_as_an_image(env: Settings) -> None:
    user = uuid.uuid4()
    att = files.save(env, user, "chart.png", PNG)
    assert (att.kind, att.media_type, att.size) == ("image", "image/png", len(PNG))
    block = files.content_block(env, user, files.reference(att))
    assert block["type"] == "image"
    assert base64.standard_b64decode(block["source"]["data"]) == PNG
    # Stored under UUIDs only.
    stored = sorted(p.name for p in (env.DATA_ROOT / "ai-attachments" / str(user)).iterdir())
    assert stored == sorted([str(att.id), f"{att.id}.json"])


def test_a_pdf_counts_its_pages_and_has_a_limit(env: Settings) -> None:
    user = uuid.uuid4()
    att = files.save(env, user, "statement.pdf", PDF)
    assert (att.kind, att.pages) == ("pdf", 2)
    block = files.content_block(env, user, files.reference(att))
    assert block["type"] == "document" and block["title"] == "statement.pdf"
    small = env.model_copy(update={"AI_ATTACHMENT_MAX_PDF_PAGES": 1})
    with pytest.raises(files.AttachmentError) as err:
        files.save(small, user, "long.pdf", PDF)
    assert err.value.status == 413


def test_spreadsheets_become_text_tables(env: Settings) -> None:
    user = uuid.uuid4()
    att = files.save(env, user, "sales.xlsx", _xlsx())
    assert (att.kind, att.rows) == ("table", 3)
    text = files.content_block(env, user, files.reference(att))["text"]
    assert text.startswith("[Attached spreadsheet sales.xlsx]\n## Sheet: Sales")
    assert "Month\tOrders\tRevenue" in text and "Nov\t98\t275500.5" in text
    csv_att = files.save(env, user, "x.csv", b"name,amount\nRavi,10\n")
    assert "Ravi\t10" in files.content_block(env, user, files.reference(csv_att))["text"]
    # Cut, and says so.
    text2, rows = files.spreadsheet_text(("a,b\n" * 5000).encode(), "text/csv", 1000)
    assert len(text2) < 1100 and "[cut at 1000 characters" in text2 and rows < 5000


def test_refusals(env: Settings) -> None:
    user = uuid.uuid4()
    for name, data, status in (
        ("tool.exe", b"MZ\x90\x00" * 10, 415),
        ("big.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024), 413),
        ("empty.csv", b"\n\n", 422),
    ):
        with pytest.raises(files.AttachmentError) as err:
            files.save(env, user, name, data)
        assert err.value.status == status, name


def test_another_user_never_sees_a_file(env: Settings) -> None:
    owner, other = uuid.uuid4(), uuid.uuid4()
    att = files.save(env, owner, "a.png", PNG)
    assert files.load(env, owner, att.id) is not None
    assert files.load(env, other, att.id) is None
    gone = files.content_block(env, other, files.reference(att))
    assert gone == {"type": "text", "text": f"{files.GONE}: a.png"}


def test_old_files_are_removed(env: Settings) -> None:
    user = uuid.uuid4()
    att = files.save(env, user, "a.png", PNG)
    meta = env.DATA_ROOT / "ai-attachments" / str(user) / f"{att.id}.json"
    old = time.time() - 40 * 86400
    os.utime(meta, (old, old))
    assert files.purge_old(env, user) == 1
    assert files.load(env, user, att.id) is None


# --- the budget estimate --------------------------------------------------------------------


def test_images_and_pdfs_are_estimated_by_tokens_not_base64() -> None:
    big = base64.standard_b64encode(b"\x00" * 3_000_000).decode()
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big}}
    doc = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(PDF).decode(),
        },
    }
    chars = _content_chars([{"role": "user", "content": [image, doc]}])
    assert chars == IMAGE_EST_CHARS + 2 * PDF_PAGE_EST_CHARS + 20
    assert chars < len(big) / 100


# --- the turn -------------------------------------------------------------------------------


async def test_a_turn_stores_references_and_sends_the_files(
    env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = uuid.uuid4()
    img = files.save(env, user, "chart.png", PNG)
    table = files.save(env, user, "x.csv", b"name,email\nRavi,ravi@example.com\n")
    sent: list[Any] = []

    async def fake_run(*args: object, **kwargs: Any) -> LoopResult:
        sent.extend(kwargs["history"])
        return LoopResult("ok", [{"role": "assistant", "content": []}], 1, Decimal(0),
                          "end_turn", [])  # fmt: skip

    rows: list[Any] = []

    class _Db:
        def add(self, row: Any) -> None:
            row.id = len(rows) + 1
            rows.append(row)

        async def flush(self) -> None: ...

        async def execute(self, *_: object) -> None: ...

        @asynccontextmanager
        async def begin(self):  # type: ignore[no-untyped-def]
            yield

    @asynccontextmanager
    async def maker():  # type: ignore[no-untyped-def]
        yield _Db()

    monkeypatch.setattr(loop, "run", fake_run)
    ctx = SimpleNamespace(settings=env, redactor=Redactor(), maker=maker, user_id=user)
    await chat.turn(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        ctx,  # type: ignore[arg-type]
        conversation=SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        history=[],
        prompt="what does this show?",
        agent="general",
        attachments=[files.reference(img), files.reference(table)],
    )
    # Claude got the image itself and the table as text.
    blocks = sent[-1]["content"]
    assert [b["type"] for b in blocks] == ["text", "image", "text"]
    assert "Ravi\t" in blocks[2]["text"]
    # The stored row keeps references, never the bytes.
    stored = rows[0].blocks
    assert [b["type"] for b in stored] == ["text", files.REF_TYPE, files.REF_TYPE]
    assert "data" not in str(stored)
    assert rows[0].content == "what does this show?"
    # Replaying the conversation expands them again, identically.
    again = files.expand(env, user, [{"role": "user", "content": stored}])
    assert again[0]["content"][1] == blocks[1]
