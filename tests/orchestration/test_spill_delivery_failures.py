"""Spill sink failures are surfaced on the wire instead of being swallowed."""

from __future__ import annotations

from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.orchestration.engine import crawl_request
from crawlutron.storage.result_store import CrawlSpillStore
from crawlutron.transport.cache import HttpCache
from tests._fetch_doubles import FetcherDouble


def _pages() -> dict[str, tuple[int, str, str]]:
    return {
        "/": (200, "<html>root</html>", "text/html"),
        "/a": (200, "<html>page-a</html>", "text/html"),
        "/b": (200, "<html>page-b</html>", "text/html"),
    }


def _page_answer(url, *_):
    entry = _pages().get(url.path or "/")
    if entry is None:
        return {"status": 404, "body": b"<html></html>", "ctype": "text/html"}
    status, body_text, ctype = entry
    return {
        "status": status,
        "body": body_text.encode("utf-8") if isinstance(body_text, str) else body_text,
        "ctype": ctype,
    }


class _FailingSpillStore(CrawlSpillStore):
    """A spill store whose ``put`` (exposed as async ``aput`` by ``atwin``)
    raises on one submission index, simulating a disk-full / SQLITE_BUSY sink.
    """

    def __init__(self, db_path, *, fail_at: int) -> None:
        self._fail_at = fail_at
        super().__init__(db_path)

    def put(self, request_id: str, sequence: int, result: EngineCrawlResult) -> None:
        if sequence == self._fail_at:
            raise OSError("spill sink: disk full")
        super().put(request_id, sequence, result)


async def _crawl_multi(urls: list[str], spill_store: CrawlSpillStore):
    fetcher = FetcherDouble(_page_answer)
    return await crawl_request(
        {"urls": urls, "policy": {"limits": {}}},
        http_fetcher=fetcher,
        cffi_fetcher=fetcher,
        cache=HttpCache(),
        spill_store=spill_store,
    )


async def test_spill_sink_failure_names_the_failed_index_and_count(tmp_path):
    store = _FailingSpillStore(tmp_path / "spills.db", fail_at=1)
    try:
        out = await _crawl_multi(
            ["http://example.com/", "http://example.com/a", "http://example.com/b"], store
        )
    finally:
        store.close()
    assert out["delivery_failures"] == [1]
    assert len(out["results"]) == 2


async def test_spill_without_sink_failure_omits_delivery_failures(tmp_path):
    store = CrawlSpillStore(tmp_path / "spills.db")
    try:
        out = await _crawl_multi(
            ["http://example.com/", "http://example.com/a", "http://example.com/b"], store
        )
    finally:
        store.close()
    assert "delivery_failures" not in out
    assert len(out["results"]) == 3
