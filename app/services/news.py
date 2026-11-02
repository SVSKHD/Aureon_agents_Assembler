"""Forex/news feed pull: RSS 2.0 / RSS 1.0 / Atom into public.astra_documents (namespace forex).

Offline by default: with NEWS_FEED_URLS empty the `news_pull` job is not scheduled and a
manual run does nothing. Safety:

- Any document with a DOCTYPE or ENTITY declaration is rejected before parsing (no
  external entities, no entity expansion). defusedxml is used when installed.
- Redirects are followed by hand (at most 3), only to http(s) URLs.
- Responses are capped at NEWS_MAX_FEED_BYTES; at most NEWS_MAX_ITEMS items per run.
- Only items with an http(s) link are kept. doc_id = sha1(link); aureon_user_id is NULL.
"""

from __future__ import annotations

import hashlib
import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET  # DOCTYPE/ENTITY are rejected before parsing

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.finance import astra_documents

_fromstring: Callable[[bytes], ET.Element]
try:  # optional hardening; the DOCTYPE check below is what is relied on
    _fromstring = importlib.import_module("defusedxml.ElementTree").fromstring
except ImportError:  # pragma: no cover - depends on the environment
    _fromstring = ET.fromstring

NAMESPACE = "forex"
MAX_REDIRECTS = 3
_DECL = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


class FeedError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Item:
    title: str
    link: str
    published: datetime | None
    source: str

    @property
    def doc_id(self) -> str:
        return hashlib.sha1(self.link.encode(), usedforsecurity=False).hexdigest()

    def data(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "link": self.link,
            "published": self.published.astimezone(UTC).isoformat() if self.published else None,
            "source": self.source,
        }


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _http(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _date(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _link(entry: ET.Element) -> str:
    best = ""
    for c in entry:
        if _local(c.tag) != "link":
            continue
        href = c.get("href")
        if href is None:  # RSS: <link>url</link>
            return _text(c)
        if c.get("rel", "alternate") == "alternate":
            return href.strip()
        best = best or href.strip()
    return best


def parse_feed(body: bytes, fallback_source: str) -> list[Item]:
    """Items of an RSS or Atom document. Raises FeedError on DOCTYPE/ENTITY or bad XML."""
    if _DECL.search(body):
        raise FeedError("feed declares a DOCTYPE or ENTITY; refused")
    try:
        root = _fromstring(body)
    except Exception as exc:  # ParseError, or a defusedxml refusal
        raise FeedError(f"invalid XML: {type(exc).__name__}") from exc
    kind = _local(root.tag)
    if kind == "feed":  # Atom
        source = _text(_child(root, "title")) or fallback_source
        entries = [e for e in root if _local(e.tag) == "entry"]
    elif kind in ("rss", "rdf"):
        channel = _child(root, "channel")
        source = _text(_child(channel, "title")) if channel is not None else ""
        source = source or fallback_source
        scope = list(channel) if (channel is not None and kind == "rss") else list(root)
        entries = [e for e in scope if _local(e.tag) == "item"]
    else:
        raise FeedError(f"not an RSS or Atom feed (<{kind}>)")
    items: list[Item] = []
    for e in entries:
        link = _link(e)
        title = _text(_child(e, "title"))
        if not title or not _http(link):
            continue
        published = _date(_text(_child(e, "pubdate", "published", "updated", "date")))
        items.append(Item(title=title[:500], link=link[:2000], published=published, source=source))
    return items


async def fetch(client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes:
    """GET with manual redirects (http(s) only) and a size cap."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not _http(current):
            raise FeedError("refusing a non-http(s) URL")
        async with client.stream("GET", current, follow_redirects=False) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location", "")
                current = urljoin(current, location)
                continue
            resp.raise_for_status()
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
                buf += chunk
                if len(buf) > max_bytes:
                    raise FeedError(f"feed larger than {max_bytes} bytes")
            return bytes(buf)
    raise FeedError("too many redirects")


async def upsert(db: AsyncSession, items: list[Item]) -> int:
    t = astra_documents
    now = datetime.now(UTC)
    n = 0
    for it in items:
        stmt = pg_insert(t).values(
            namespace=NAMESPACE, doc_id=it.doc_id, user_id=None, aureon_user_id=None,
            data=it.data(), updated_at=now,
        )  # fmt: skip
        stmt = stmt.on_conflict_do_update(
            index_elements=[t.c.namespace, t.c.doc_id],
            set_={"data": stmt.excluded.data, "updated_at": now},
            # Never touch a row a user owns, and skip unchanged items.
            where=(t.c.aureon_user_id.is_(None)) & (t.c.data != stmt.excluded.data),
        )
        res = await db.execute(stmt)
        n += int(res.rowcount)  # type: ignore[attr-defined]
    return n


async def pull(
    db: AsyncSession, settings: Settings, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    urls = settings.NEWS_FEED_URLS
    if not urls:
        return {"feeds": 0, "items": 0, "written": 0, "skipped": "NEWS_FEED_URLS is empty"}
    own = client is None
    http = client or httpx.AsyncClient(
        timeout=settings.NEWS_HTTP_TIMEOUT, headers={"User-Agent": "aureon-news/1"}
    )
    items: list[Item] = []
    errors: dict[str, str] = {}
    try:
        for url in urls:
            if len(items) >= settings.NEWS_MAX_ITEMS:
                break
            host = urlsplit(url).netloc or "feed"
            try:
                body = await fetch(http, url, settings.NEWS_MAX_FEED_BYTES)
                items += parse_feed(body, host)
            except FeedError as exc:
                errors[host] = str(exc)[:300]
            except httpx.HTTPStatusError as exc:  # no URL in the message: it may hold a token
                errors[host] = f"HTTP {exc.response.status_code}"
            except httpx.HTTPError as exc:
                errors[host] = type(exc).__name__
    finally:
        if own:
            await http.aclose()
    by_id: dict[str, Item] = {}
    for it in items:
        by_id.setdefault(it.doc_id, it)  # first occurrence wins
        if len(by_id) >= settings.NEWS_MAX_ITEMS:
            break
    unique = list(by_id.values())
    written = await upsert(db, unique)
    return {"feeds": len(urls), "items": len(unique), "written": written, "errors": errors}
