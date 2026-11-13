"""The envelope is read once per content-cache replay: the headers read threads
into body-replay and revalidation, so each path does exactly one get."""

from __future__ import annotations

from typing import TYPE_CHECKING

from crawlutron.transport.cache import HttpCache
from crawlutron.transport.content_cache import ContentCache
from tests._fetch_doubles import FetcherDouble
from tests._fetcher_factories import run_http_strategy

if TYPE_CHECKING:
    from crawlutron.models.crawl_result import EngineCrawlResult


class _ReadCounter:
    """Wraps ``content_cache.aget`` so a replay can count envelope reads."""

    def __init__(self, content_cache: ContentCache) -> None:
        self._real_aget = content_cache.aget
        self.count = 0

    async def __call__(self, key: str):
        self.count += 1
        return await self._real_aget(key)


async def _replay_reads_once(
    content_cache: ContentCache,
    url: str,
    fetcher: FetcherDouble,
    monkeypatch,
) -> tuple[int, EngineCrawlResult]:
    """Seed, then run one replay under a counting ``aget``; return reads and result."""
    cache = HttpCache()
    assert (
        await run_http_strategy(url, {}, fetcher, cache=cache, content_cache=content_cache)
    ).status == "success"
    counter = _ReadCounter(content_cache)
    monkeypatch.setattr(content_cache, "aget", counter)
    result = await run_http_strategy(url, {}, fetcher, cache=cache, content_cache=content_cache)
    return counter.count, result


def _etag_answer():
    etag = '"v1"'
    body = b"<html><body>STORE</body></html>"

    def respond(_url, sent: dict[str, str], *_args):
        matches = sent.get("If-None-Match", "") == etag
        return {
            "status": 304 if matches else 200,
            "headers": {"etag": etag},
            "body": b"" if matches else body,
        }

    return respond


async def _assert_single_read(content_cache, url, fetcher, monkeypatch) -> None:
    """Seed, then replay once under a counting ``aget`` and require one read."""
    reads, result = await _replay_reads_once(content_cache, url, fetcher, monkeypatch)
    assert result.status == "success"
    assert reads == 1, reads


class TestOneReadPerReplay:
    async def test_body_replay_reads_the_envelope_once(self, tmp_path, monkeypatch):
        url = "http://example.com/no-validator"
        body = b"<html><body>REPLAY</body></html>"
        content_cache = ContentCache(cache_dir=str(tmp_path / "cc"), default_ttl=3600)
        fetcher = FetcherDouble(
            lambda _url, _headers, *_args: {
                "status": 200,
                "headers": {"content-type": "text/html"},
                "body": body,
            }
        )
        await _assert_single_read(content_cache, url, fetcher, monkeypatch)

    async def test_304_revalidation_reads_the_envelope_once(self, tmp_path, monkeypatch):
        url = "http://example.com/etagged-reval"
        content_cache = ContentCache(cache_dir=str(tmp_path / "cc"), default_ttl=3600)
        await _assert_single_read(content_cache, url, FetcherDouble(_etag_answer()), monkeypatch)
