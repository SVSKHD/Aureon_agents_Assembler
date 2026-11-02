"""News feed parser and fetcher (no DB): RSS / Atom fixtures, XXE refusal, redirect rules."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.core.config import Settings
from app.services import news
from app.workers.scheduler import default_jobs

RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>FX Desk</title>
<item><title> EUR/USD   climbs </title><link>https://news.test/a</link>
  <pubDate>Mon, 02 Nov 2026 09:00:00 GMT</pubDate></item>
<item><title>Bad scheme</title><link>javascript:alert(1)</link></item>
<item><title>Undated</title><link>http://news.test/b</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom FX</title>
<entry><title>Yen slips</title>
  <link rel="self" href="https://atom.test/self"/>
  <link rel="alternate" href="https://atom.test/yen"/>
  <updated>2026-11-01T08:30:00Z</updated></entry>
</feed>"""

XXE = b"""<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<rss version="2.0"><channel><item><title>&xxe;</title><link>https://x.test/</link></item>
</channel></rss>"""

LAUGHS = b"""<?xml version="1.0"?>
<!doctype lolz [<!entity lol "lol"><!entity lol2 "&lol;&lol;&lol;&lol;">]>
<rss><channel><item><title>&lol2;</title></item></channel></rss>"""


def test_parse_rss() -> None:
    items = news.parse_feed(RSS, "fallback")
    assert [(i.title, i.link, i.source) for i in items] == [
        ("EUR/USD climbs", "https://news.test/a", "FX Desk"),
        ("Undated", "http://news.test/b", "FX Desk"),
    ]
    assert items[0].published == datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert items[1].published is None
    assert items[0].data()["published"] == "2026-11-02T09:00:00+00:00"
    assert len(items[0].doc_id) == 40


def test_parse_atom_prefers_alternate_link() -> None:
    [item] = news.parse_feed(ATOM, "fallback")
    assert (item.title, item.link, item.source) == ("Yen slips", "https://atom.test/yen", "Atom FX")
    assert item.published == datetime(2026, 11, 1, 8, 30, tzinfo=UTC)


@pytest.mark.parametrize("payload", [XXE, LAUGHS])
def test_doctype_and_entities_are_refused(payload: bytes) -> None:
    with pytest.raises(news.FeedError, match="DOCTYPE"):
        news.parse_feed(payload, "x")


def test_garbage_and_non_feeds() -> None:
    with pytest.raises(news.FeedError, match="invalid XML"):
        news.parse_feed(b"<rss><channel>", "x")
    with pytest.raises(news.FeedError, match="not an RSS"):
        news.parse_feed(b"<html><body/></html>", "x")


@respx.mock
async def test_fetch_follows_http_redirects_only() -> None:
    respx.get("https://feeds.test/a").mock(
        return_value=httpx.Response(302, headers={"location": "/b"})
    )
    respx.get("https://feeds.test/b").mock(return_value=httpx.Response(200, content=RSS))
    respx.get("https://feeds.test/evil").mock(
        return_value=httpx.Response(301, headers={"location": "file:///etc/passwd"})
    )
    async with httpx.AsyncClient() as c:
        assert await news.fetch(c, "https://feeds.test/a", 10_000) == RSS
        with pytest.raises(news.FeedError, match="non-http"):
            await news.fetch(c, "https://feeds.test/evil", 10_000)
        with pytest.raises(news.FeedError, match="non-http"):
            await news.fetch(c, "ftp://feeds.test/x", 10_000)
        with pytest.raises(news.FeedError, match="larger than"):
            await news.fetch(c, "https://feeds.test/b", 100)


@respx.mock
async def test_pull_reports_errors_without_urls(data_root: Path) -> None:
    respx.get("https://feeds.test/x?token=secret").mock(return_value=httpx.Response(500))
    respx.get("https://feeds.test/xxe").mock(return_value=httpx.Response(200, content=XXE))

    class NoDb:
        async def execute(self, *_a: object) -> None:  # pragma: no cover - nothing to write
            raise AssertionError("nothing should be written")

    s = _settings(data_root, NEWS_FEED_URLS='["https://feeds.test/x?token=secret"]')
    out = await news.pull(NoDb(), s)  # type: ignore[arg-type]
    assert out["errors"] == {"feeds.test": "HTTP 500"} and out["items"] == 0
    assert "secret" not in str(out)
    s = _settings(data_root, NEWS_FEED_URLS='["https://feeds.test/xxe"]')
    out = await news.pull(NoDb(), s)  # type: ignore[arg-type]
    assert "DOCTYPE" in out["errors"]["feeds.test"]


def _settings(data_root: Path, **kw: object) -> Settings:
    return Settings(
        ENV="test",
        DATABASE_URL="postgresql+asyncpg://x:x@127.0.0.1:1/none",
        SECRET_KEY="t" * 40,
        DATA_ROOT=data_root,
        **kw,  # type: ignore[arg-type]
    )


async def test_empty_feed_list_is_a_noop_and_not_scheduled(data_root: Path) -> None:
    s = _settings(data_root)
    assert s.NEWS_FEED_URLS == []
    assert "news-pull" not in [j[0] for j in default_jobs(s)]
    out = await news.pull(None, s)  # type: ignore[arg-type]
    assert out["items"] == 0 and "skipped" in out
    on = _settings(data_root, NEWS_FEED_URLS='["https://feeds.test/rss"]')
    assert ("news-pull", "news_pull", "*/30 * * * *") in default_jobs(on)
