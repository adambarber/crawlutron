"""Streaming spill (#2): page results serialize to disk as each batch
completes, so peak memory scales with batch size instead of total pages.

Contract:
* ``crawl_request(..., spill_path=...)`` writes exactly one JSON line per
  crawled page ({"i": index, "r": to_dict(result)}) in append order.
* The assembled response is byte-equivalent to a run without spilling.
* Auto-created temp spill files are deleted when the request finishes;
  stale spills from dead runs are garbage-collected on subsequent crawls.
"""

import json
import os
import tempfile

from _testkit import site_fetcher

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request


def _pages():
    return {
        "/": (200, '<html><body><a href="/a">a</a>'
                    '<a href="https://instagram.com/city">ig</a></body></html>', "text/html"),
        "/a": (200, "<html>aaa</html>", "text/html"),
    }


def _payload(scope=True):
    policy = {"limits": {"max_depth": 1, "max_pages": 10}}
    if scope:
        # keep the crawl on the seed host so instagram stays recorded-but-unfetched
        policy["scope_regex"] = r"^https?://example\.com(/|$)"
    return {"url": "http://example.com/", "policy": policy}


async def test_spill_file_gets_one_json_line_per_page(tmp_path):
    sp = tmp_path / "spill.ndjson"
    out = await crawl_request(
        _payload(), http_fetcher=site_fetcher(_pages()), cache=HttpCache(), spill_path=str(sp)
    )
    lines = [json.loads(line) for line in sp.read_text().splitlines() if line.strip()]
    assert len(lines) == len(out["results"]) == 2
    assert sorted(entry["i"] for entry in lines) == [0, 1]
    for entry in lines:
        assert isinstance(entry["r"], dict)
        assert "normalized_url" in entry["r"]


async def test_response_equivalent_with_and_without_spill(tmp_path):
    straight = await crawl_request(
        _payload(), http_fetcher=site_fetcher(_pages()), cache=HttpCache()
    )
    spilled = await crawl_request(
        _payload(), http_fetcher=site_fetcher(_pages()), cache=HttpCache(),
        spill_path=str(tmp_path / "s.ndjson"),
    )
    assert straight["results"] == spilled["results"]
    assert straight["submitted_count"] == spilled["submitted_count"]


async def test_auto_spill_deleted_after_successful_request(monkeypatch):
    import app.crawl.engine as engine

    created: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        if kwargs.get("prefix", args[0] if args else "").startswith(engine._SPILL_PREFIX):
            created.append(path)
        return fd, path

    monkeypatch.setattr(engine.tempfile, "mkstemp", spy_mkstemp)
    out = await crawl_request(_payload(), http_fetcher=site_fetcher(_pages()), cache=HttpCache())
    assert out["results"], "sanity: crawl produced results"
    assert created, "engine did not create an auto spill file"
    assert all(not os.path.exists(p) for p in created)


async def test_stale_spills_garbage_collected_on_next_crawl(monkeypatch):
    import app.crawl.engine as engine

    stale = os.path.join(tempfile.gettempdir(), engine._SPILL_PREFIX + "stale.ndjson")
    fresh = os.path.join(tempfile.gettempdir(), engine._SPILL_PREFIX + "fresh.ndjson")
    for path in (stale, fresh):
        with open(path, "w") as fh:
            fh.write("{}\n")
    old = __import__("time").time() - 7200
    os.utime(stale, (old, old))

    try:
        await crawl_request(_payload(), http_fetcher=site_fetcher(_pages()), cache=HttpCache())
        assert not os.path.exists(stale), "stale spill survived GC"
        assert os.path.exists(fresh), "GC ate a fresh spill from another live request"
    finally:
        for path in (stale, fresh):
            if os.path.exists(path):
                os.unlink(path)
