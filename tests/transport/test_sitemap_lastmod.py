"""Sitemap lastmod association: linear, and off the loop when large (#526)."""

import asyncio
import time

from crawlutron.transport.fetch_helpers import SITEMAP_CACHE, fetch_sitemap_urls
from tests._assertions import install_to_thread_spy
from tests._fetch_doubles import FetcherDouble, make_fetch
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

# Where the complexity claim lives: growth step and the ratio bound. Quadratic
# on this step costs ~16x; the linear fix costs ~4x; the bound sits between.
_BASE, _GROW_STEP, _RATIO_BOUND = 9_216, 4, 10
# The base is sized so the small walk's baseline clears ~100ms (#611).
_SAMPLES = 2


def _entries(n: int) -> str:
    return "".join(
        f"<url><loc>http://x.test/p{i}</loc><lastmod>2024-06-0{1 + i % 3}T00:00:00Z</lastmod></url>"
        for i in range(n)
    )


def _sitemapindex(body: str) -> str:
    """Wrap one body in a sitemapindex envelope."""
    return f"<sitemapindex>{body}</sitemapindex>"


def _sitemap_fetcher(body: str):
    """A fetcher answering one 200 sitemap body."""
    return FetcherDouble(
        lambda u, _sent_headers, *_: make_fetch(
            url="http://x.test/sitemap.xml",
            final_url="http://x.test/sitemap.xml",
            status=200,
            headers={"content-type": "application/xml"},
            body=body.encode(),
        )
    )


def _index_fetcher():
    """One root index whose child loc carries an XML entity, answered by a page body."""

    def answer(u, _sent_headers, *_: object):
        return make_fetch(
            url=u.raw,
            final_url=u.raw,
            status=200,
            headers={"content-type": "application/xml"},
            body=(
                b"<sitemapindex><sitemap><loc>http://x.test/child?a=1&amp;b=2</loc></sitemap></sitemapindex>"
                if u.raw == "http://x.test/sitemap.xml"
                else b"<url><loc>http://x.test/page</loc></url>"
            ),
        )

    return FetcherDouble(answer)


async def _walk(body: str, max_urls: int) -> set[tuple[str, str | None]]:
    """One public-door walk; the cache entry is discarded either way."""
    url = canonical("http://x.test/sitemap.xml")
    try:
        resolved = await fetch_sitemap_urls(
            _sitemap_fetcher(body), url, 30_000, max_urls=max_urls, host_policy=UNRESTRICTED_HOST_POLICY
        )
        return {(loc.raw, lastmod) for loc, lastmod in resolved}
    finally:
        SITEMAP_CACHE.discard((url.raw, UNRESTRICTED_HOST_POLICY))


async def test_loc_xml_entity_decodes_and_dedupes_against_href_twin():
    """A <loc> with &amp; resolves to the same NormalizedUrl as the single-& href twin."""
    body = "<url><loc>http://x.test/p?a=1&amp;b=2</loc></url>"
    pairs = await _walk(body, 10)
    href_twin = canonical("http://x.test/p?a=1&b=2")
    assert pairs == {(href_twin.raw, None)}, pairs


async def test_index_child_loc_xml_entity_decodes():
    """A sitemapindex child <loc> keeps its real & through resolution too."""
    fetcher = _index_fetcher()
    url = canonical("http://x.test/sitemap.xml")
    try:
        await fetch_sitemap_urls(fetcher, url, 30_000, max_urls=10, host_policy=UNRESTRICTED_HOST_POLICY)
    finally:
        SITEMAP_CACHE.discard((url.raw, UNRESTRICTED_HOST_POLICY))
    assert "http://x.test/child?a=1&b=2" in fetcher.calls, fetcher.calls


async def test_lastmod_association_is_linear_not_quadratic():
    """4x the entries must not cost ~16x the work. Both walks are CPU-bound parsing/association work, so each
    side is warmed then timed best-of-N on process_time; the base input is scaled so the small baseline
    clears ~100ms, and the best-of-N sampling keeps a descheduled process from reading as super-linear.
    """
    small_elapsed = await _warmed_best(_entries(_BASE), _BASE + 1)
    large_elapsed = await _warmed_best(_entries(_BASE * _GROW_STEP), _BASE * _GROW_STEP + 1)
    assert large_elapsed < small_elapsed * _RATIO_BOUND, "association cost grew super-linearly"


async def _warmed_best(entries: str, max_urls: int, samples: int = _SAMPLES) -> float:
    """Warm the walk once, then return the cheapest of ``samples`` process_time runs."""
    await _walk(entries, max_urls)
    best = float("inf")
    for _ in range(samples):
        started = time.process_time()
        walked = await _walk(entries, max_urls)
        best = min(best, time.process_time() - started)
    assert len(walked) == max_urls - 1, "the walk dropped entries"
    return best


async def test_pairing_semantics_unchanged():
    """Each loc pairs with the first lastmod strictly inside its own entry span."""
    body = (
        "<url><loc>http://x.test/a</loc><lastmod>2024-06-01</lastmod></url>"
        "<url><loc>http://x.test/b</loc></url>"
        "<url><loc>http://x.test/c</loc><lastmod>2024-06-03</lastmod></url>"
    )
    pairs = await _walk(body, 10)
    assert pairs == {
        ("http://x.test/a", "2024-06-01"),
        ("http://x.test/b", None),
        ("http://x.test/c", "2024-06-03"),
    }, pairs


async def test_large_sitemap_association_runs_off_the_event_loop(monkeypatch):
    """A body past the offload threshold must not hold the loop for the scan."""
    seen = install_to_thread_spy(monkeypatch)
    body = _entries(20_000)
    assert len(body) >= 262_144
    await _walk(body, 30_000)
    assert "_collect_page_entries" in seen, (
        f"the large-body lastmod association ran on the event loop (to_thread saw: {seen})"
    )


async def test_large_sitemapindex_scan_runs_off_the_event_loop(monkeypatch):
    """A sitemapindex body offloads its findall+lower too (#544/P6)."""
    seen = install_to_thread_spy(monkeypatch)
    body = _sitemapindex(_entries(20_000))
    assert len(body) >= 262_144

    async def _capped_walk():
        try:
            return await _walk(body, 4)
        finally:
            pass

    # max_urls=4 caps the child fan-out: the index is huge, the walk is not.
    await asyncio.wait_for(_capped_walk(), timeout=10)
    assert "_scan_sitemap_text" in seen, (
        f"the large sitemapindex scan ran on the event loop (to_thread saw: {seen})"
    )
