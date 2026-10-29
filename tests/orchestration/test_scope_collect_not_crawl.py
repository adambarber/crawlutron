"""Collect-but-don't-crawl contract (flipped scope semantics + skip hosts).

``scope_regex`` gates expansion ONLY: out-of-scope links stay in
``discovered_links`` but are never fetched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.orchestration.engine import crawl_request
from crawlutron.transport.cache import HttpCache
from tests._fetch_doubles import FetcherDouble, PageMap
from tests._fetcher_factories import site_fetcher, tap_fetcher

if TYPE_CHECKING:
    from crawlutron.models.host_policy import HostPolicy
    from crawlutron.models.http_fetcher import HttpFetcher
    from crawlutron.models.normalized_url import NormalizedUrl


def _recording_fetcher(pages: PageMap) -> FetcherDouble:
    # The real site fetcher answers; the tap only records. The subject of every test in this file is which
    # URLs get enqueued, so the fetcher must not be a second implementation of the site.
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


def _crawled_urls(out: ProducerEnvelope) -> set[str]:
    """The URLs the crawl actually fetched a page for."""
    return {r.normalized_url.raw for r in out["results"] if r.normalized_url is not None}


async def _crawl_example_site(pages, **policy):
    """Crawl the example fixture site; return the (envelope, fetched urls) pair.

    ``policy`` is merged over the default limits, so a test names the one knob
    it is about instead of re-spelling a crawl it is not testing.
    """
    fetcher = _recording_fetcher(pages)
    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": 2, "max_pages": 10}, **policy},
    }
    out = await crawl_request(payload, http_fetcher=cast("HttpFetcher", fetcher), cache=HttpCache())
    return out, fetcher.calls


def _recorded_discovered(out: ProducerEnvelope, root_url: str = "http://example.com/") -> set[str]:
    """URLs recorded on the seed result's ``discovered_links``, as a set.

    The property is that a recorded-but-not-fetched link still reaches the seed
    result; stated once so neither the root nor the shape can drift per copy.
    """
    root = next(
        r for r in out["results"] if r.normalized_url is not None and r.normalized_url.raw == root_url
    )
    return {d.normalized_url.raw for d in root.discovered_links}


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
    crawled = _crawled_urls(out)
    # expansion stayed in scope...
    assert crawled == {"http://example.com/", "http://example.com/keep/x"}
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
    crawled = _crawled_urls(out)
    assert crawled == {"http://example.com/", "http://example.com/local"}
    assert not any("pinterest.com" in u or "x.com/" in u for u in fetched), fetched
    recorded = _recorded_discovered(out)
    assert "https://www.pinterest.com/pin/1" in recorded
    assert "https://x.com/mayor" in recorded


async def test_document_urls_recorded_but_not_fetched_by_default():
    # PDFs/CSVs/etc yield no HTML links: record their URLs when advertised, but never spend bandwidth
    # downloading them during URL discovery.
    out, fetched = await _crawl_example_site(_doc_pages())
    crawled = _crawled_urls(out)
    assert crawled == {"http://example.com/", "http://example.com/local"}
    assert not any(u.endswith(".pdf") for u in fetched)
    assert "http://example.com/reports/2024-minutes.pdf" in _recorded_discovered(out)


async def test_document_urls_fetched_when_extraction_document_requested():
    """Callers who explicitly want document contents still get them."""
    out, _fetched = await _crawl_example_site(
        {**_doc_pages(), "/reports/2024-minutes.pdf": (200, "%PDF-1.4 minutes", "application/pdf")},
        extraction="document",
    )
    crawled = _crawled_urls(out)
    assert "http://example.com/reports/2024-minutes.pdf" in crawled


class TestSitemapCandidateBuilderUsesTheCanonicalOrigin:
    """``fetch_job_sitemap_candidates`` re-derived the origin by hand.

    It rebuilt ``scheme://netloc`` instead of via ``NormalizedUrl``, so a
    default port or trailing dot changed the sitemap URL; asserted behaviourally.
    """

    async def _sitemap_url_for(self, root_url: str) -> str | None:
        """Run the builder with the sitemap fetch stubbed, returning the URL asked for."""
        import crawlutron.admission.admission as admission

        seen: list[str] = []

        async def _capture(
            fetcher: HttpFetcher,
            sitemap_url: NormalizedUrl,
            timeout_seconds: int,
            *,
            max_depth: int = 2,
            max_urls: int = 100,
            conditional: dict[str, str] | None = None,
            host_policy: HostPolicy,
            _test_stub: bool = True,  # marks this as a test double
        ) -> set[tuple[NormalizedUrl, str | None]]:
            seen.append(sitemap_url.raw)
            return set()

        from crawlutron.admission.admission import JobAdmissionContext
        from crawlutron.admission.policy import PolicyTier
        from crawlutron.config.settings import CrawlerSettings
        from crawlutron.models.crawl_policy import CrawlPolicy

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
        from crawlutron.models.host_policy import HostPolicy
        from crawlutron.models.normalized_url import NormalizedUrl

        canonical = NormalizedUrl(root_url, policy=HostPolicy.unrestricted())
        expected = f"{canonical.origin}/sitemap.xml"

        actual = await self._sitemap_url_for(root_url)
        assert actual == expected, f"{root_url!r} -> {actual!r}, canonicalizer says {expected!r}"

    async def test_a_non_http_root_yields_no_candidates(self):
        assert await self._sitemap_url_for("ftp://example.com/") is None
