"""Collect-but-don't-crawl contract (flipped scope semantics + skip hosts).

* ``scope_regex`` gates frontier expansion ONLY: out-of-scope links stay in
  ``discovered_links`` (municipalities advertise social profiles we must
  record) but are never fetched.
* Analytics/skip-list hosts are likewise recorded yet never enqueued as jobs,
  regardless of strategy.
"""

import pytest

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request
from tests._fetcher_factories import site_fetcher, tap_fetcher


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


def _recorded_discovered(out: dict, root_url: str = "http://example.com") -> set[str]:
    """URLs recorded on the seed result's ``discovered_links``, as a set.

    The property under test is that a link which was *recorded but deliberately
    not fetched* still reaches the seed result. Each of these tests needs the
    recorded half on its own -- the URL it is asking about was never fetched, so
    it has no entry in ``results`` -- and the lookup that got there was three
    verbatim copies of the same two lines. Stated once because it has two ways
    to go wrong that a single copy makes visible: a root keyed on a hard-coded
    URL, and a comprehension that keeps whole records instead of URLs, which
    passes for every host and fails for exactly one.
    """
    root = next(r for r in out["results"] if r.normalized_url == root_url)
    return {d.normalized_url for d in root.discovered_links}


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
    crawled = {r.normalized_url for r in out["results"]}
    # expansion stayed in scope...
    assert crawled == {"http://example.com", "http://example.com/keep/x"}
    # ...and instagram was never fetched
    assert not any("instagram.com" in u for u in fetched)
    # ...but the advertised profile is still recorded on the seed result
    assert "https://instagram.com/cityofexample" in _recorded_discovered(out)


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
    crawled = {r.normalized_url for r in out["results"]}
    assert crawled == {"http://example.com", "http://example.com/local"}
    assert not any("pinterest.com" in u or "x.com/" in u for u in fetched), fetched
    recorded = _recorded_discovered(out)
    assert "https://www.pinterest.com/pin/1" in recorded
    assert "https://x.com/mayor" in recorded


async def test_document_urls_recorded_but_not_fetched_by_default():
    # PDFs/CSVs/etc yield no HTML links: record their URLs when advertised,
    # but never spend bandwidth downloading them during URL discovery.
    out, fetched = await _crawl_example_site(_doc_pages())
    crawled = {r.normalized_url for r in out["results"]}
    assert crawled == {"http://example.com", "http://example.com/local"}
    assert not any(u.endswith(".pdf") for u in fetched)
    assert "http://example.com/reports/2024-minutes.pdf" in _recorded_discovered(out)


async def test_document_urls_fetched_when_extraction_document_requested():
    """Callers who explicitly want document contents still get them."""
    out, _fetched = await _crawl_example_site(
        {**_doc_pages(), "/reports/2024-minutes.pdf": (200, "%PDF-1.4 minutes", "application/pdf")},
        extraction="document",
    )
    crawled = {r.normalized_url for r in out["results"]}
    assert "http://example.com/reports/2024-minutes.pdf" in crawled


class TestSitemapCandidateBuilderUsesTheCanonicalOrigin:
    """``fetch_job_sitemap_candidates`` re-derived the origin by hand.

    It called ``urlparse`` and rebuilt ``scheme://netloc`` itself instead of
    going through ``normalize_public_http_url``, the function whose docstring
    says it is the single source of truth for what a normalized URL is. The
    hand-built form keeps a default port, keeps host case, and keeps a trailing
    dot -- so the same job root produced a different sitemap URL depending on
    which spelling the caller happened to store. The policy is also never
    consulted here, so a host the canonicalizer would refuse is used as a
    sitemap origin before the fetcher sees it.

    Asserted behaviourally: the URL the builder fetches is the one the
    canonicalizer would produce for the same root.
    """

    async def _sitemap_url_for(self, root_url: str) -> str | None:
        """Run the builder with the sitemap fetch stubbed, returning the URL asked for."""
        import app.crawl.admission as admission

        seen: list[str] = []

        async def _capture(_fetcher, sitemap_url, _timeout, **_kwargs):
            seen.append(sitemap_url)
            return set()

        from app.api_models import CrawlPolicy
        from app.crawl.admission import JobAdmissionContext
        from app.crawl.policy import PolicyTier
        from app.settings import CrawlerSettings

        context = JobAdmissionContext.create(
            CrawlPolicy(), PolicyTier.model_defaults(), CrawlerSettings.from_mapping({})
        )
        original = admission.fetch_sitemap_urls
        admission.fetch_sitemap_urls = _capture
        try:
            await admission.fetch_job_sitemap_candidates(root_url, context)
        finally:
            admission.fetch_sitemap_urls = original
        return seen[0] if seen else None

    @pytest.mark.parametrize(
        "root_url",
        [
            "https://example.com/deep/path",
            "HTTPS://Example.COM:443/deep/path",
            "https://example.com.:443/deep/path",
            "http://example.com:80/deep/path",
        ],
    )
    async def test_the_origin_is_canonical_regardless_of_how_the_root_was_spelled(self, root_url):
        from urllib.parse import urlsplit

        from app.validation import HostPolicy, normalize_public_http_url

        canonical = normalize_public_http_url(root_url, policy=HostPolicy.unrestricted())
        parts = urlsplit(canonical)
        expected = f"{parts.scheme}://{parts.netloc}/sitemap.xml"

        actual = await self._sitemap_url_for(root_url)
        assert actual == expected, f"{root_url!r} -> {actual!r}, canonicalizer says {expected!r}"

    async def test_a_non_http_root_yields_no_candidates(self):
        assert await self._sitemap_url_for("ftp://example.com/") is None
