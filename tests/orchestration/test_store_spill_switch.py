"""The engine spills page results into the store as each batch completes.

The file spill (mkstemp + flock + NDJSON) is gone: the same write/read shape
rides the crawl_spills table -- incremental INSERT per result, ordered read-back
for the response, rows reclaimed by age.
"""

from __future__ import annotations

from crawlutron.storage.result_store import CrawlSpillStore
from tests._crawl_runners import crawled_results

_PAGES = {
    "/": (200, '<html><body><a href="/a">a</a></body></html>', "text/html"),
    "/a": (200, "<html>aaa</html>", "text/html"),
}


def _store(tmp_path) -> CrawlSpillStore:
    return CrawlSpillStore(tmp_path / "spills.db")


async def test_the_crawl_spills_each_result_into_the_store(tmp_path):
    """One row per result, present before the crawl returns -- the incremental
    write the file spill gave, now durable rows."""
    store = _store(tmp_path)
    try:
        out = await crawled_results(_PAGES, spill_store=store)
        with __import__("sqlite3").connect(tmp_path / "spills.db") as conn:
            count = conn.execute("SELECT COUNT(*) FROM crawl_spills").fetchone()[0]
        assert count == len(out["results"]) >= 1, (
            f"expected one row per result ({len(out['results'])}), saw {count}"
        )
    finally:
        store.close()


async def test_the_response_reassembles_from_the_store_in_submission_order(tmp_path):
    store = _store(tmp_path)
    try:
        straight = await crawled_results(_PAGES)
        spilled = await crawled_results(_PAGES, spill_store=store)
        assert [r.original_url for r in spilled["results"]] == [
            r.original_url for r in straight["results"]
        ], "store-backed reassembly diverged from the straight crawl"
    finally:
        store.close()


async def test_a_store_backed_crawl_leaves_no_spill_file(tmp_path):
    """The switch is total: no mkstemp NDJSON artifact on disk afterward."""
    import tempfile

    store = _store(tmp_path)
    try:
        before = {p for p in __import__("os").listdir(tempfile.gettempdir()) if p.startswith("bc-spill-")}
        await crawled_results(_PAGES, spill_store=store)
        after = {p for p in __import__("os").listdir(tempfile.gettempdir()) if p.startswith("bc-spill-")}
        assert after - before == set(), "the crawl still wrote a file spill"
    finally:
        store.close()
