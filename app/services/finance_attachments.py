"""Receipt attachments for finance rows, stored on the HDD.

Files live at DATA_ROOT/finance-attachments/<user uuid>/<attachment uuid>. The path is
built from UUIDs only: nothing the client sends (filename included) reaches the
filesystem. The content type is sniffed from the magic bytes; the client's header is
ignored. Owner-scoped like the rest of finance: another user's attachment is 404.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.finance import FinanceAttachment
from app.services import finance as fin
from app.services.finance import FinanceError

ROOT_DIR = "finance-attachments"
CHUNK = 64 * 1024
ALLOWED_TYPES = ("image/png", "image/jpeg", "image/webp", "application/pdf")


class Readable(Protocol):
    async def read(self, size: int = -1) -> bytes: ...


def sniff(head: bytes) -> str | None:
    """Content type from the file's first bytes, or None if not an allowed type."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    return None


_UNSAFE = re.compile(r"[\x00-\x1f\x7f/\\]")


def clean_filename(name: str | None) -> str:
    """Display name only (metadata and Content-Disposition), never a path."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE.sub("_", base).strip().lstrip(".")
    return base[:200] or "attachment"


def attachments_root(settings: Settings) -> Path:
    return settings.DATA_ROOT / ROOT_DIR


def relative_path(user_id: uuid.UUID, attachment_id: uuid.UUID) -> str:
    return f"{ROOT_DIR}/{user_id}/{attachment_id}"


def safe_file(settings: Settings, relative: str) -> Path | None:
    """Resolve a stored path, refusing anything outside DATA_ROOT/finance-attachments."""
    root = attachments_root(settings).resolve()
    candidate = (settings.DATA_ROOT / relative).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate if candidate.is_file() else None


def _write(path: Path, chunks: list[bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    try:
        with tmp.open("xb") as fh:
            for c in chunks:
                fh.write(c)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def unlink_files(settings: Settings, relatives: list[str]) -> None:
    """Best effort, after the DB commit (a leftover file is harmless; a dangling row is not)."""
    root = attachments_root(settings).resolve()
    for rel in relatives:
        p = (settings.DATA_ROOT / rel).resolve()
        if p.is_relative_to(root):
            with contextlib.suppress(FileNotFoundError):
                p.unlink()


async def read_upload(upload: Readable, max_bytes: int) -> tuple[list[bytes], int, str, str]:
    """Read at most max_bytes (413 beyond), sniff the type (415 if not allowed) and hash it."""
    chunks: list[bytes] = []
    size = 0
    digest = hashlib.sha256()
    while True:
        chunk = await upload.read(CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise FinanceError(413, "too_large", f"attachments are limited to {max_bytes} bytes")
        digest.update(chunk)
        chunks.append(chunk)
    if size == 0:
        raise FinanceError(422, "empty", "the file is empty")
    content_type = sniff(b"".join(chunks)[:16])
    if content_type is None:
        raise FinanceError(
            415, "unsupported_type", f"only {', '.join(ALLOWED_TYPES)} files are accepted"
        )
    return chunks, size, content_type, digest.hexdigest()


async def prepare(
    db: AsyncSession, settings: Settings, user_id: uuid.UUID, kind: str, ref: str
) -> None:
    """Checks before reading the body: the row is the user's own, and the cap has room."""
    res = fin.RESOURCES.get(kind)
    if res is None:
        raise FinanceError(422, "bad_kind", "kind must be trades, expenses or secured")
    await fin.get_row(db, res, user_id, ref)  # 404 for another user's row
    count = await db.scalar(
        select(func.count())
        .select_from(FinanceAttachment)
        .where(FinanceAttachment.aureon_user_id == user_id)
    )
    if int(count or 0) >= settings.FINANCE_ATTACHMENTS_MAX_PER_USER:
        raise FinanceError(
            409,
            "attachment_limit",
            f"at most {settings.FINANCE_ATTACHMENTS_MAX_PER_USER} attachments per user",
        )


def store(
    settings: Settings, user_id: uuid.UUID, attachment_id: uuid.UUID, chunks: list[bytes]
) -> str:
    """Write the file (sync; run in a thread). Returns its path relative to DATA_ROOT."""
    rel = relative_path(user_id, attachment_id)
    _write(settings.DATA_ROOT / rel, chunks)
    return rel


async def get(db: AsyncSession, user_id: uuid.UUID, attachment_id: uuid.UUID) -> FinanceAttachment:
    row: FinanceAttachment | None = await db.scalar(
        select(FinanceAttachment).where(
            FinanceAttachment.id == attachment_id, FinanceAttachment.aureon_user_id == user_id
        )
    )
    if row is None:
        raise FinanceError(404, "not_found", "attachment not found")
    return row


async def list_for(
    db: AsyncSession, user_id: uuid.UUID, kind: str | None, ref: str | None
) -> list[FinanceAttachment]:
    q = select(FinanceAttachment).where(FinanceAttachment.aureon_user_id == user_id)
    if kind is not None:
        q = q.where(FinanceAttachment.kind == kind)
    if ref is not None:
        q = q.where(FinanceAttachment.ref == ref)
    rows = await db.scalars(q.order_by(FinanceAttachment.created_at.desc()).limit(500))
    return list(rows.all())
