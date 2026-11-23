"""Link, JS-endpoint and pagination discovery delegated to crawlutron_extract.

``DiscoveredLink`` stays core's public type, constructed from the package's
``LinkRecord``. Text normalisation lives here too (the gate-sanctioned owner).
"""

from __future__ import annotations

from dataclasses import replace as _replace
from typing import TypedDict, cast

from crawlutron.extraction.trees import html_diagnostics
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.models.json_value import JsonValue
from crawlutron.models.page_metadata import PageMetadata
from crawlutron_extract import (
    detect_pagination as _package_detect_pagination,
)
from crawlutron_extract import (
    extract_discovered_links as _package_extract_discovered_links,
)
from crawlutron_extract import (
    extract_js_endpoints as _package_extract_js_endpoints,
)
from crawlutron_extract.limits import ExtractLimits
from crawlutron_extract.links import LinkRecord
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl, admit_or_none


class PostFetchDiscovery(TypedDict):
    """The post-fetch discovery envelope: one parse's products, declared
    once at the producer. A consumer that reads a key the producer stopped
    selecting is now a type error, not a silent None.
    """

    diagnostics: dict[str, JsonValue]
    discovered_links: list[DiscoveredLink]
    discovered_link_count: int
    discovered_links_truncated: bool
    js_endpoints: list[dict[str, str]]
    next_page_url: NormalizedUrl | None
    llm_metadata: PageMetadata | None


def normalized_extraction(extracted) -> tuple[str, str | None]:
    """The ``(text, markdown)`` an extractor result contributes to a crawl
    result: ``or ""`` keeps an extractor's ``None`` text off a result.
    """
    return extracted.text or "", extracted.markdown


def _link_record_to_discovered_link(record: LinkRecord, source_url: NormalizedUrl) -> DiscoveredLink:
    """Build core's public ``DiscoveredLink`` from the package's ``LinkRecord``."""
    return DiscoveredLink(
        url=record.raw_href,
        normalized_url=record.normalized_url,
        text=record.text,
        rel=list(record.rel),
        source_url=source_url,
        host=record.normalized_url.host,
        is_same_host=record.is_same_host,
    )


def extract_discovered_links(
    html: str | None,
    source_url: NormalizedUrl | None,
    *,
    max_links: int = 250,
    tree: object = None,
) -> tuple[list[DiscoveredLink], int, bool]:
    """Admitted links: package admission + cap, translated into core's type."""
    del tree  # the package parses its own DOM
    if not html or source_url is None:
        return [], 0, False
    # Admit every unique link (no effective cap) so ``count`` reflects total
    # unique admitted before the cap; the returned list is capped at max_links.
    all_records, _refused = _package_extract_discovered_links(
        html, source_url, limits=_replace(ExtractLimits(), max_links=10**9)
    )
    links = [_link_record_to_discovered_link(r, source_url) for r in all_records[:max_links]]
    count = len(all_records)
    return links, count, count > len(links)


def extract_js_endpoints(
    html: str | None,
    source_url: NormalizedUrl | None = None,
    *,
    host_policy: HostPolicy,
) -> list[dict[str, str]]:
    """Client-side API endpoints from JavaScript, resolved against source_url,
    then re-admitted through the caller's host policy before the URLs return.
    """
    if not html:
        return []
    urls = _package_extract_js_endpoints(html, source_url, limits=ExtractLimits())
    endpoints: list[dict[str, str]] = []
    for url in urls:
        admitted = admit_or_none(url.raw, policy=host_policy)
        if admitted is not None:
            endpoints.append({"url": admitted.raw})
    return endpoints


def detect_pagination(html: str | None, final_url: NormalizedUrl | None) -> NormalizedUrl | None:
    """First rel=next href resolved against final_url; None when absent or non-http."""
    if html is None:
        return None
    return _package_detect_pagination(html, final_url)


def extract_post_fetch_discovery(
    html: str | None,
    final_url: NormalizedUrl | None,
    *,
    js_base_url: NormalizedUrl | None = None,
    max_links: int = 250,
    include_diagnostics: bool = False,
    include_links: bool = False,
    include_js_endpoints: bool = False,
    host_policy: HostPolicy,
) -> PostFetchDiscovery:
    """Sync post-fetch discovery delegated to crawlutron_extract."""
    envelope: PostFetchDiscovery = {
        "diagnostics": {},
        "discovered_links": [],
        "discovered_link_count": 0,
        "discovered_links_truncated": False,
        "js_endpoints": [],
        "next_page_url": None,
        "llm_metadata": None,
    }
    if include_diagnostics:
        envelope["diagnostics"] = (
            cast(
                "dict[str, JsonValue]",
                html_diagnostics(html, final_url, tree=None, limits=ExtractLimits()),
            )
            if html
            else {}
        )
    if include_links:
        links, count, truncated = extract_discovered_links(html, final_url, max_links=max_links)
        envelope["discovered_links"] = links
        envelope["discovered_link_count"] = count
        envelope["discovered_links_truncated"] = truncated
    if include_js_endpoints:
        envelope["js_endpoints"] = extract_js_endpoints(
            html, js_base_url if js_base_url is not None else final_url, host_policy=host_policy
        )
    envelope["next_page_url"] = detect_pagination(html, final_url)
    return envelope
