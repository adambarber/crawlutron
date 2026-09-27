"""Collect-but-don't-crawl contract (flipped scope semantics + skip hosts).

* ``scope_regex`` gates frontier expansion ONLY: out-of-scope links stay in
  ``discovered_links`` (municipalities advertise social profiles we must
  record) but are never fetched.
* Analytics/skip-list hosts are likewise recorded yet never enqueued as jobs,
  regardless of strategy.
"""

from _fetcher_factories import site_fetcher, tap_fetcher

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request


def _recording_fetcher(pages):
    # The real site fetcher answers; the tap only records. The subject of every
    # test in this file is which URLs get enqueued, so the fetcher must not be a
    # second implementation of the site.
    return tap_fetcher(site_fetcher(pages))


def _doc_pages():
    """Town page advertising a PDF alongside an HTML subpage."""
    return {
        "/": (
            200,
            '<html><body><a href="/local">l</a><a href="/reports/2024-minutes.pdf">pdf</a></body></html>',
            "text/html",
        ),
        "/local": (200, "<html>l</html>", "text/html"),
    }


async def _crawl_example_site(pages, **policy):
    """Crawl the example fixture site; return the (envelope, fetched urls) pair.

    ``policy`` is merged over the default limits, so a test that is about one
    policy knob (a scope regex, ``extraction``) names that knob instead of
    re-spelling the crawl it is not about -- which is how two of these tests
    each grew their own copy of the payload, the cache and the
    crawled-URL-set line.
    """
    fetcher = _recording_fetcher(pages)
    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": 2, "max_pages": 10}, **policy},
    }
    out = await crawl_request(payload, http_fetcher=fetcher, cache=HttpCache())
    return out, fetcher.calls


async def test_scope_records_out_of_scope_links_but_never_fetches_them():
    pages = {
        "/": (
            200,
            '<html><body><a href="/keep/x">k</a>'
            '<a href="https://instagram.com/cityofexample">ig</a></body></html>',
            "text/html",
        ),
        "/keep/x": (200, "<html>x</html>", "text/html"),
    }
    out, fetched = await _crawl_example_site(
        pages,
        limits={"max_depth": 1, "max_pages": 10},
        scope_regex=r"^https?://example\.com(/|$)",
    )
    crawled = {r["normalized_url"] for r in out["results"]}
    # expansion stayed in scope...
    assert crawled == {"http://example.com", "http://example.com/keep/x"}
    # ...and instagram was never fetched
    assert not any("instagram.com" in u for u in fetched)
    # ...but the advertised profile is still recorded on the seed result
    root = next(r for r in out["results"] if r["normalized_url"] == "http://example.com")
    recorded = {d["normalized_url"] for d in root["discovered_links"]}
    assert "https://instagram.com/cityofexample" in recorded


async def test_skip_list_hosts_recorded_but_never_enqueued():
    pages = {
        "/": (
            200,
            '<html><body><a href="/local">l</a>'
            '<a href="https://www.pinterest.com/pin/1">p</a>'
            '<a href="https://x.com/mayor">x</a></body></html>',
            "text/html",
        ),
        "/local": (200, "<html>l</html>", "text/html"),
    }
    out, fetched = await _crawl_example_site(pages)
    crawled = {r["normalized_url"] for r in out["results"]}
    assert crawled == {"http://example.com", "http://example.com/local"}
    assert not any("pinterest.com" in u or "x.com/" in u for u in fetched), fetched
    root = next(r for r in out["results"] if r["normalized_url"] == "http://example.com")
    recorded = {d["normalized_url"] for d in root["discovered_links"]}
    assert "https://www.pinterest.com/pin/1" in recorded
    assert "https://x.com/mayor" in recorded


async def test_document_urls_recorded_but_not_fetched_by_default():
    # PDFs/CSVs/etc yield no HTML links: record their URLs when advertised,
    # but never spend bandwidth downloading them during URL discovery.
    out, fetched = await _crawl_example_site(_doc_pages())
    crawled = {r["normalized_url"] for r in out["results"]}
    assert crawled == {"http://example.com", "http://example.com/local"}
    assert not any(u.endswith(".pdf") for u in fetched)
    root = next(r for r in out["results"] if r["normalized_url"] == "http://example.com")
    recorded = {d["normalized_url"] for d in root["discovered_links"]}
    assert "http://example.com/reports/2024-minutes.pdf" in recorded


async def test_document_urls_fetched_when_extraction_document_requested():
    """Callers who explicitly want document contents still get them."""
    out, _fetched = await _crawl_example_site(
        {**_doc_pages(), "/reports/2024-minutes.pdf": (200, "%PDF-1.4 minutes", "application/pdf")},
        extraction="document",
    )
    crawled = {r["normalized_url"] for r in out["results"]}
    assert "http://example.com/reports/2024-minutes.pdf" in crawled
