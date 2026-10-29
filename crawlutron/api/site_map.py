"""Site-map endpoint support.

A "map" is the reachable-from-root URL set (in-page links + robots sitemaps +
feeds, gathered in a normal crawl); ``build_site_map`` yields a de-duplicated, stable-ordered list.
"""

from __future__ import annotations

from crawlutron.api.response_shaping import frontier_link_url
from crawlutron.models.crawl_diagnostics import CrawlDiagnostics
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl, admit_or_none
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.models.site_map_response import SiteMapPayload


def build_site_map(crawl_response: ProducerEnvelope, *, host_policy: HostPolicy) -> SiteMapPayload:
    """Aggregate a crawl response's discovered URLs into a site map.

    Returns a SiteMapPayload: ``url`` = crawled root (final_url or original_url); links/sitemaps/
    feeds sorted+de-duplicated, ``all_urls`` = de-duplicated union in stable encounter order.
    """
    results = crawl_response["results"]
    result = results[0] if results else None
    diagnostics: CrawlDiagnostics = result.diagnostics if result is not None else {}
    robots = diagnostics.get("robots")
    discovery = diagnostics.get("discovery")

    root = (result.final_url or result.normalized_url) if result is not None else None
    if root is None and result is not None:
        root = admit_or_none(result.original_url, policy=host_policy)

    def _normalize(u: str | None) -> str | None:
        """Canonicalize every emitted URL: sort query params, strip fragments/
        default ports, resolve origin-relative paths; drop anything not
        normalizable rather than leak it raw."""
        if not u:
            return None
        normalized = root.resolve(u) if root is not None else admit_or_none(u, policy=host_policy)
        return normalized.raw if normalized is not None else None

    links = _discovered_link_roots(result, root=root, host_policy=host_policy)
    sitemaps = [n for u in (robots.get("sitemaps") or []) if (n := _normalize(u))] if robots else []
    sitemap_urls = (
        [n for u in (discovery.get("sitemap_urls") or []) if (n := _normalize(u))] if discovery else []
    )
    feeds = [n for u in (discovery.get("feeds") or []) if (n := _normalize(u))] if discovery else []

    all_urls: list[str] = []
    seen: set[str] = set()
    for u in links + sitemaps + sitemap_urls + feeds:
        if u and u not in seen:
            seen.add(u)
            all_urls.append(u)

    return {
        "url": root.raw if root is not None else None,
        "links": sorted(set(links)),
        "sitemaps": sorted(set(sitemaps + sitemap_urls)),
        "feeds": sorted(set(feeds)),
        "all_urls": all_urls,
    }


def _discovered_link_roots(
    result: EngineCrawlResult | None, *, root: NormalizedUrl | None, host_policy: HostPolicy
) -> list[str]:
    """The discovered links, resolved against the crawl's own root."""
    links: list[str] = []
    for link in result.discovered_links if result is not None else ():
        link_url = frontier_link_url(
            link,
            policy=host_policy,
            resolve_against=root,
        )
        if link_url is not None:
            links.append(link_url.raw)
    return links
