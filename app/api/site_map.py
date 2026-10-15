"""Site-map endpoint support.

A "map" is a structured discovery of the URLs reachable from a root URL: the
in-page discovered links plus the URLs advertised via robots.txt sitemaps and
feeds. The crawler already gathers these during a normal crawl (discovery diag
in :func:`app.orchestration.engine.crawl_request`); ``build_site_map`` aggregates them
into a single de-duplicated, stable-ordered URL list for callers that only want
the frontier (e.g. to drive their own recursive crawl or sitemap generation).
"""

from __future__ import annotations

from app.api.response_shaping import frontier_link_url
from app.models.host_policy import HostPolicy
from app.models.normalized_url import admit_or_none
from app.models.result_record import ProducerEnvelope
from app.models.site_map_response import SiteMapPayload


def build_site_map(crawl_response: ProducerEnvelope, *, host_policy: HostPolicy) -> SiteMapPayload:
    """Aggregate discovered URLs from a crawl response into a site map.

    Returns a dict with:

    * ``url``      - the crawled root URL (final_url or original_url)
    * ``links``    - in-page discovered links (sorted, de-duplicated)
    * ``sitemaps`` - robots.txt + sitemap.xml URLs (sorted, de-duplicated)
    * ``feeds``    - discovered feed URLs (sorted, de-duplicated)
    * ``all_urls`` - the de-duplicated union in stable encounter order

    The function is pure and defensive about the link shape (dict or dataclass).
    """
    results = crawl_response["results"]
    result = results[0] if results else None
    diagnostics = result.diagnostics if result is not None else {}
    robots = diagnostics.get("robots") or {}
    discovery = diagnostics.get("discovery") or {}

    root = (result.final_url or result.normalized_url) if result is not None else None
    if root is None and result is not None:
        root = admit_or_none(result.original_url, policy=host_policy)

    def _normalize(u: str | None) -> str | None:
        """Canonicalize every emitted URL (sorts query params, strips
        fragments/default ports). Origin-relative paths resolve against the
        crawled root; anything that still is not a normalizable public
        URL is dropped rather than leaked raw."""
        if not u:
            return None
        normalized = root.resolve(u) if root is not None else admit_or_none(u, policy=host_policy)
        return normalized.raw if normalized is not None else None

    links: list[str] = []
    for link in result.discovered_links if result is not None else ():
        link_url = frontier_link_url(
            link,
            policy=host_policy,
            resolve_against=root,
        )
        if link_url is not None:
            links.append(link_url.raw)

    sitemaps: list[str] = []
    sitemap_urls: list[str] = []
    feeds: list[str] = []
    for u in robots.get("sitemaps", []) or []:
        n = _normalize(u)
        if n:
            sitemaps.append(n)
    for u in discovery.get("sitemap_urls", []) or []:
        n = _normalize(u)
        if n:
            sitemap_urls.append(n)
    for u in discovery.get("feeds", []) or []:
        n = _normalize(u)
        if n:
            feeds.append(n)

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
