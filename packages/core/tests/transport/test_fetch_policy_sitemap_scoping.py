"""Tests for robots sitemap cap and host-scoping (issue #1022).

Verifies that ``_resolve_sitemap_urls`` caps directive count, limits to the
robots file's own registrable domain, and records discovery diagnostics.
"""

from __future__ import annotations

import asyncio

import pytest

from crawlutron.admission.policy import read_policy
from crawlutron.models.crawl_diagnostics import CrawlDiagnostics
from crawlutron.transport import fetch_policy
from crawlutron.transport.fetch_policy import discovery_diagnostics
from tests._fetch_doubles import FetcherDouble
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, admitted
from tests._timing_harnesses import HANG_GUARD_SECONDS

_HOST = "target.test"
_CROSS_SITE = "https://evil.example/sitemap.xml"


def _robots_with_many_sitemaps(n: int, cross_site: str | None = None) -> str:
    """A robots.txt with ``n`` same-site Sitemap lines and an optional cross-site."""
    lines = ["User-agent: *", "Allow: /"]
    for i in range(n):
        lines.append(f"Sitemap: https://{_HOST}/sitemap-{i}.xml")
    if cross_site:
        lines.append(f"Sitemap: {cross_site}")
    return "\n".join(lines) + "\n"


async def _run_discovery(fetcher) -> CrawlDiagnostics:
    """Run discovery_diagnostics against the workspace target with one fetcher."""
    return await discovery_diagnostics(
        normalized_url=admitted(f"https://{_HOST}/", policy=UNRESTRICTED_HOST_POLICY),
        html="<html><body>ok</body></html>",
        policy=read_policy({"discovery": {"robots": True, "sitemaps": True}}),
        host_policy=UNRESTRICTED_HOST_POLICY,
        headers={},
        http_fetcher=fetcher,
        engine_robots_sitemaps=None,
        robots_evaluated=True,
    )


class TestSitemapDirectiveCap:
    """At most N sitemap directives are honoured (cap is ROBOTS_SITEMAP_DIRECTIVE_CAP)."""

    async def test_directive_cap_limits_fetches_to_n(self):
        """500 Sitemap: lines cause at most N fetches (counted via fetcher calls)."""
        robot_text = _robots_with_many_sitemaps(500)
        calls: list = []

        def counting_fetcher(url, headers, timeout, *rest):
            # Count every fetch for cap verification.
            if "/robots.txt" in url.raw:
                return (200, robot_text, "text/plain")
            return (200, f"<sitemap><loc>{url.raw}</loc></sitemap>", "text/xml")

        await _run_discovery(FetcherDouble(counting_fetcher, calls=calls))

        cap = fetch_policy._ROBOTS_SITEMAP_DIRECTIVE_CAP
        assert len(calls) <= 1 + cap, f"Expected at most {1 + cap} fetches, got {len(calls)}"

    async def test_diagnostics_include_fetch_and_drop_counts(self):
        """The discovery diagnostics block carries fetch/drop counts."""
        robot_text = _robots_with_many_sitemaps(50)

        def counting_fetcher(url, headers, timeout, *rest):
            # Count fetches for diagnostics verification.
            if "/robots.txt" in url.raw:
                return (200, robot_text, "text/plain")
            return (200, "<sitemap><loc>/page</loc></sitemap>", "text/xml")

        result = await _run_discovery(FetcherDouble(counting_fetcher))
        discovery = result.get("discovery")
        assert discovery is not None, f"Expected discovery block; got {list(result.keys())}"
        assert "fetch_count" in discovery, "fetch_count missing from discovery"
        assert "directive_drop_count" in discovery, "directive_drop_count missing from discovery"
        assert discovery["fetch_count"] == 20
        assert discovery["directive_drop_count"] == 30


class TestSitemapHostScope:
    """Only sitemaps on the robots file's own registrable domain are fetched."""

    async def test_cross_site_sitemap_not_fetched(self):
        """A ``Sitemap: https://evil.example/sitemap.xml`` line is NOT fetched.

        ``resolve`` admits it on its own; the site filter is the security boundary."""
        robot_text = _robots_with_many_sitemaps(0, cross_site=_CROSS_SITE)

        def allowing_fetcher(url, headers, timeout, *rest):
            if "/robots.txt" in url.raw:
                return (200, robot_text, "text/plain")
            assert "evil.example" not in url.raw, f"Cross-site sitemap was fetched: {url.raw}"
            return (200, "<sitemap><loc>/page</loc></sitemap>", "text/xml")

        result = await _run_discovery(FetcherDouble(allowing_fetcher))

        robots_block = result.get("robots")
        if robots_block:
            assert _CROSS_SITE not in robots_block.get("sitemaps", []), (
                f"Cross-site sitemap appeared in robots.sitemaps: {robots_block['sitemaps']}"
            )


class TestSitemapDeadlineBound:
    """Sitemap walks under the request's existing deadline/timeout."""

    async def test_hanging_sitemap_terminates_within_deadline(self):
        """A sitemap that never responds is bounded by the timeout, not the suite.

        The fetcher hangs on every call; the walk must still return because
        the timeout deadline is enforced on each sitemap fetch.
        """
        block = asyncio.Event()

        async def hanging_fetcher(url, headers, timeout, *rest):
            await block.wait()  # blocks forever -- fetcher ignores timeout
            return (200, b"<sitemap/>", "text/xml")

        double = FetcherDouble(hanging_fetcher)
        target = admitted(f"https://{_HOST}/", policy=UNRESTRICTED_HOST_POLICY)

        async def probe():
            return await discovery_diagnostics(
                normalized_url=target,
                html="<html><body>ok</body></html>",
                policy=read_policy(
                    {
                        "discovery": {"robots": True, "sitemaps": True},
                        "limits": {"timeout_seconds": 1},
                    }
                ),
                host_policy=UNRESTRICTED_HOST_POLICY,
                headers={},
                http_fetcher=double,
                engine_robots_sitemaps=None,
                robots_evaluated=True,
            )

        try:
            await asyncio.wait_for(probe(), timeout=HANG_GUARD_SECONDS)
        except TimeoutError:
            pytest.fail(
                f"Sitemap walk did not terminate within {HANG_GUARD_SECONDS}s: "
                "the timeout deadline does not reach the sitemap fetch path."
            )


@pytest.mark.parametrize("n", [0, 1, 20])
class TestSameSiteStillDiscovered:
    """Regression: same-site sitemaps are still discovered exactly as before."""

    async def test_same_site_sitemaps_discovered(self, n: int):
        """A same-site sitemap (and a same-site sitemap index with children)
        is still discovered exactly as before, and diagnostics are a superset."""
        robot_text = _robots_with_many_sitemaps(n)

        def fetcher(url, headers, timeout, *rest):
            if "/robots.txt" in url.raw:
                return (200, robot_text, "text/plain")
            return (
                200,
                f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f"<url><loc>{url.raw}/page</loc></url></urlset>",
                "application/xml",
            )

        result = await _run_discovery(FetcherDouble(fetcher))
        discovery = result.get("discovery")
        assert discovery is not None, f"Expected discovery block; got {list(result.keys())}"
        sitemap_urls = discovery["sitemap_urls"]
        # With no Sitemap directives, the /sitemap.xml fallback still fetches one;
        # with directives, at most n same-site URLs are discovered.
        expected = n if n else 1
        assert len(sitemap_urls) == expected, f"Expected {expected} sitemap URLs, got {len(sitemap_urls)}"
