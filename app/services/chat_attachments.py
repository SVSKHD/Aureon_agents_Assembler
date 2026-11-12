"""Files attached to an AI chat message: photos, PDFs, spreadsheets (docs/26-ai-providers.md).

Upload (`POST /api/ai/attachments`), then send the returned ids with the message. Files live at
DATA_ROOT/ai-attachments/<user uuid>/<attachment uuid>, beside a small JSON description. The
path is built from UUIDs only: nothing the client sends (the filename included) reaches the
filesystem, and another user's id is simply not found.

What Claude receives:

- **Images** (PNG, JPEG, WebP, GIF; at most 5 MB each, Claude's limit): an image block.
- **PDFs** (at most AI_ATTACHMENT_MAX_PDF_PAGES pages): a document block.
- **Spreadsheets** (Excel .xlsx, CSV, TSV): converted here, at upload, to a plain-text table
  (every sheet, capped at AI_ATTACHMENT_TABLE_MAX_CHARS), sent as a text block and redacted
  like the rest of the message. The original file is not kept.

Images and PDFs cannot be redacted: what is in them reaches Claude as it is. The app says so
before sending.

The content type is sniffed from the bytes, never taken from the client. Stored conversation
rows keep a reference block ({"type": "aureon_attachment", "id": …}), never the file; it is
expanded again from disk each time the conversation is replayed (the same bytes, so the
prompt cache still matches). Files older than AI_ATTACHMENT_KEEP_DAYS are removed; a reply
that refers to one then sees "[attachment no longer available]".
"""

from __future__ import annotations

import base64
import contextlib
import csv
import io
import json
import os
import re
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from app.core.config import Settings

ROOT_DIR = "ai-attachments"
CHUNK = 64 * 1024
IMAGE_MAX_BYTES = 5 * 1024 * 1024  # Claude's per-image limit
REF_TYPE = "aureon_attachment"
GONE = "[attachment no longer available]"

Kind = Literal["image", "pdf", "table"]
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
_PDF_PAGE = re.compile(rb"/Type\s*/Page(?!s)")
_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\]")


class AttachmentError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class Readable(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class Attachment:
    id: uuid.UUID
    name: str
    kind: Kind
    media_type: str
    size: int
    pages: int | None = None
    rows: int | None = None

    def meta(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "name": self.name,
            "kind": self.kind,
            "media_type": self.media_type,
            "size": self.size,
            "pages": self.pages,
            "rows": self.rows,
            "created_at": time.time(),
        }


def clean_filename(name: str | None) -> str:
    """Display name only, never a path."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE.sub("_", base).strip().lstrip(".")
    return base[:120] or "attachment"


def sniff(head: bytes, name: str) -> str | None:
    """The media type from the first bytes (and, for text tables, the extension)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    lower = name.lower()
    if head.startswith(b"PK\x03\x04") and lower.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if lower.endswith((".csv", ".tsv")) and b"\x00" not in head:
        return "text/tab-separated-values" if lower.endswith(".tsv") else "text/csv"
    return None


def user_dir(settings: Settings, user_id: uuid.UUID) -> Path:
    return settings.DATA_ROOT / ROOT_DIR / str(user_id)


def _paths(settings: Settings, user_id: uuid.UUID, att_id: uuid.UUID) -> tuple[Path, Path]:
    d = user_dir(settings, user_id)
    return d / str(att_id), d / f"{att_id}.json"


async def read_upload(upload: Readable, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await upload.read(CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise AttachmentError(413, "too_large", f"files are limited to {max_bytes} bytes")
        chunks.append(chunk)
    if size == 0:
        raise AttachmentError(422, "empty", "the file is empty")
    return b"".join(chunks)


def _cell(v: Any) -> str:
    if v is None:
        return ""
    text = str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ").strip()
    return text[:200]


def _table_text(
    sheets: Iterable[tuple[str, Iterable[Iterable[Any]]]], max_chars: int
) -> tuple[str, int]:
    """Tab-separated text, one section per sheet, cut at max_chars. Returns (text, rows)."""
    out: list[str] = []
    used = 0
    rows = 0
    cut = False
    for title, values in sheets:
        header = f"## Sheet: {title}" if title else "## Table"
        out.append(header)
        used += len(header) + 1
        for row in values:
            cells = [_cell(v) for v in row]
            while cells and not cells[-1]:
                cells.pop()
            if not cells:
                continue
            line = "\t".join(cells)
            if used + len(line) + 1 > max_chars:
                cut = True
                break
            out.append(line)
            used += len(line) + 1
            rows += 1
        if cut:
            break
    if cut:
        out.append(f"…[cut at {max_chars} characters: {rows} rows shown]")
    return "\n".join(out), rows


def spreadsheet_text(data: bytes, media_type: str, max_chars: int) -> tuple[str, int]:
    if media_type.startswith("text/"):
        text = data.decode("utf-8-sig", errors="replace")
        delimiter = "\t" if media_type == "text/tab-separated-values" else ","
        return _table_text([("", csv.reader(io.StringIO(text), delimiter=delimiter))], max_chars)
    try:
        from openpyxl import load_workbook  # only for .xlsx uploads
    except ImportError as exc:  # pragma: no cover - a dependency of the image
        raise AttachmentError(
            415, "unsupported_type", "Excel files are not supported here"
        ) from exc
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise AttachmentError(422, "unreadable", "the Excel file could not be read") from exc
    try:
        return _table_text(((ws.title, ws.iter_rows(values_only=True)) for ws in wb.worksheets),
                           max_chars)  # fmt: skip
    finally:
        wb.close()


def pdf_pages(data: bytes) -> int:
    return len(_PDF_PAGE.findall(data))


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        with tmp.open("xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def purge_old(settings: Settings, user_id: uuid.UUID, now: float | None = None) -> int:
    """Remove this user's attachments older than AI_ATTACHMENT_KEEP_DAYS. Returns how many."""
    d = user_dir(settings, user_id)
    if not d.is_dir():
        return 0
    cutoff = (now or time.time()) - settings.AI_ATTACHMENT_KEEP_DAYS * 86400
    removed = 0
    for meta in d.glob("*.json"):
        with contextlib.suppress(OSError, ValueError):
            if meta.stat().st_mtime < cutoff:
                att_id = uuid.UUID(meta.stem)
                data, _ = _paths(settings, user_id, att_id)
                data.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                removed += 1
    return removed


def count(settings: Settings, user_id: uuid.UUID) -> int:
    d = user_dir(settings, user_id)
    return sum(1 for _ in d.glob("*.json")) if d.is_dir() else 0


def save(settings: Settings, user_id: uuid.UUID, filename: str | None, data: bytes) -> Attachment:
    """Check, convert and store one upload. Raises AttachmentError with an HTTP status."""
    name = clean_filename(filename)
    media_type = sniff(data[:16], name)
    if media_type is None:
        raise AttachmentError(
            415,
            "unsupported_type",
            "photos (PNG, JPEG, WebP, GIF), PDF, Excel (.xlsx), CSV and TSV files only",
        )
    purge_old(settings, user_id)
    if count(settings, user_id) >= settings.AI_ATTACHMENTS_MAX_PER_USER:
        raise AttachmentError(409, "too_many", "too many stored attachments; try again later")
    att_id = uuid.uuid4()
    path, meta_path = _paths(settings, user_id, att_id)
    if media_type in IMAGE_TYPES:
        if len(data) > IMAGE_MAX_BYTES:
            raise AttachmentError(413, "too_large", "photos are limited to 5 MB each")
        att = Attachment(att_id, name, "image", media_type, len(data))
        _write(path, data)
    elif media_type == "application/pdf":
        pages = pdf_pages(data)
        if pages > settings.AI_ATTACHMENT_MAX_PDF_PAGES:
            raise AttachmentError(
                413, "too_many_pages",
                f"PDFs are limited to {settings.AI_ATTACHMENT_MAX_PDF_PAGES} pages",
            )  # fmt: skip
        att = Attachment(att_id, name, "pdf", media_type, len(data), pages=pages or None)
        _write(path, data)
    else:
        text, rows = spreadsheet_text(data, media_type, settings.AI_ATTACHMENT_TABLE_MAX_CHARS)
        if not rows:
            raise AttachmentError(422, "empty", "the spreadsheet has no rows")
        att = Attachment(att_id, name, "table", media_type, len(data), rows=rows)
        _write(path, text.encode())
    _write(meta_path, json.dumps(att.meta()).encode())
    return att


def load(settings: Settings, user_id: uuid.UUID, att_id: uuid.UUID) -> Attachment | None:
    """This user's attachment, or None (missing, expired or someone else's)."""
    path, meta_path = _paths(settings, user_id, att_id)
    try:
        meta = json.loads(meta_path.read_text())
        if not path.is_file():
            return None
        return Attachment(
            att_id,
            str(meta["name"]),
            meta["kind"],
            str(meta["media_type"]),
            int(meta["size"]),
            meta.get("pages"),
            meta.get("rows"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def reference(att: Attachment) -> dict[str, Any]:
    """What a stored conversation row keeps: the id and the name, never the file."""
    return {"type": REF_TYPE, "id": str(att.id), "name": att.name, "kind": att.kind}


def content_block(settings: Settings, user_id: uuid.UUID, ref: dict[str, Any]) -> dict[str, Any]:
    """A stored reference turned back into what Claude reads."""
    try:
        att_id = uuid.UUID(str(ref.get("id")))
    except ValueError:
        return {"type": "text", "text": GONE}
    att = load(settings, user_id, att_id)
    path, _ = _paths(settings, user_id, att_id)
    if att is None:
        return {"type": "text", "text": f"{GONE}: {clean_filename(str(ref.get('name')))}"}
    data = path.read_bytes()
    if att.kind == "table":
        return {"type": "text", "text": f"[Attached spreadsheet {att.name}]\n{data.decode()}"}
    encoded = base64.standard_b64encode(data).decode()
    if att.kind == "pdf":
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
            "title": att.name,
        }
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": att.media_type, "data": encoded},
    }


def expand(
    settings: Settings, user_id: uuid.UUID, messages: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Messages with every stored attachment reference replaced by its content."""
    out: list[dict[str, Any]] = []
    for m in messages:
        content = m.get("content")
        if (
            m.get("role") == "user"
            and isinstance(content, list)
            and any(isinstance(b, dict) and b.get("type") == REF_TYPE for b in content)
        ):
            blocks = [
                content_block(settings, user_id, b)
                if isinstance(b, dict) and b.get("type") == REF_TYPE
                else b
                for b in content
            ]
            out.append({**m, "content": blocks})
        else:
            out.append(m)
    return out


__all__ = [
    "GONE",
    "REF_TYPE",
    "Attachment",
    "AttachmentError",
    "clean_filename",
    "content_block",
    "expand",
    "load",
    "pdf_pages",
    "purge_old",
    "read_upload",
    "reference",
    "save",
    "sniff",
    "spreadsheet_text",
]
