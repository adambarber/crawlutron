"""Streaming spill: page results persist as each batch completes.

Peak memory scales with batch size, not total pages; ``crawl_request`` writes one spill row per page
through the injected store, and the response reassembles from the rows in submission order.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from crawlutron.storage.result_store import CrawlSpillStore
from tests._fetcher_factories import crawled_results


def _pages():
    return {
        "/": (
            200,
            '<html><body><a href="/a">a</a><a href="https://instagram.com/city">ig</a></body></html>',
            "text/html",
        ),
        "/a": (200, "<html>aaa</html>", "text/html"),
    }


def _policy(scope=True):
    """The policy half of the crawl, as (limits, policy) for crawled_results."""
    policy: dict[str, object] = {}
    if scope:
        # keep the crawl on the seed host so instagram stays recorded-but-unfetched
        policy["scope_regex"] = r"^https?://example\.com(/|$)"
    return {"max_depth": 1, "max_pages": 10}, policy


async def _crawl(*, scope=True, **kwargs):
    limits, policy = _policy(scope)
    return await crawled_results(_pages(), limits=limits, policy=policy, **kwargs)


def _row_count(db_path) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM crawl_spills").fetchone()[0]


async def test_the_store_gets_one_spill_row_per_page(tmp_path):
    store = CrawlSpillStore(tmp_path / "spills.db")
    try:
        out = await _crawl(spill_store=store)
        assert _row_count(tmp_path / "spills.db") == len(out["results"])
    finally:
        store.close()


async def test_response_equivalent_with_and_without_store(tmp_path):
    straight = await _crawl()
    store = CrawlSpillStore(tmp_path / "spills.db")
    try:
        spilled = await _crawl(spill_store=store)
        assert straight["results"] == spilled["results"]
        assert straight["submitted_count"] == spilled["submitted_count"]
    finally:
        store.close()
