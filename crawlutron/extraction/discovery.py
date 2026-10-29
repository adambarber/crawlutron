"""Link, JS-endpoint and pagination discovery over extracted trees."""

from __future__ import annotations

import re
from html import unescape
from typing import Any, TypedDict, cast

from crawlutron.extraction.documents import ExtractedContent
from crawlutron.extraction.llm_format import (
    PageMetadata as ExtractedPageMetadata,
)
from crawlutron.extraction.llm_format import llm_metadata_from_tree
from crawlutron.extraction.trees import html_diagnostics, parse_html_tree
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.json_value import JsonValue
from crawlutron.models.normalized_url import NormalizedUrl as NormalizedUrl
from crawlutron.models.normalized_url import admit_or_none
from crawlutron.safety.validation import filter_js_endpoints


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
    llm_metadata: ExtractedPageMetadata


def extract_discovered_links(
    html: str | None,
    source_url: NormalizedUrl | None,
    *,
    max_links: int = 250,
    tree: Any | None = None,
) -> tuple[list[DiscoveredLink], int, bool]:
    if not html or not source_url:
        return [], 0, False
    source_host = source_url.host
    if tree is None:
        tree = parse_html_tree(html)
    if tree is None:
        return [], 0, False
    # Single pass: normalize/dedupe inline instead of materializing an
    # intermediate per-anchor dict list (memory on link-heavy pages).
    links: list[DiscoveredLink] = []
    seen: set[NormalizedUrl] = set()
    total_count = 0
    for el in tree.iter("a"):
        # Skip anchors nested inside another anchor to avoid double counting.
        parent = el.getparent()
        if parent is not None and str(parent.tag) == "a":
            continue
        attrs = {key.lower(): value for key, value in el.attrib.items()}
        href = unescape(str(attrs.get("href", "")).strip())
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        normalized_url = source_url.resolve(href)
        if normalized_url is None or normalized_url in seen:
            continue
        seen.add(normalized_url)
        total_count += 1
        if len(links) >= max_links:
            continue
        text = re.sub(r"\s+", " ", unescape("".join(el.itertext()))).strip()
        host = normalized_url.host
        links.append(
            DiscoveredLink(
                url=href,
                normalized_url=normalized_url,
                text=text,
                rel=str(attrs.get("rel", "")).split(),
                source_url=source_url,
                host=host,
                is_same_host=host.lower() == source_host,
            )
        )
    return links, total_count, total_count > len(links)


def normalized_extraction(extracted: ExtractedContent) -> tuple[str, str | None]:
    """The ``(text, markdown)`` an extractor result contributes to a crawl result:
    ``or ""`` keeps an extractor's ``None`` text off a result, so the coercion
    lives here beside ``ExtractedContent``, the type whose invariant it is.
    """
    return extracted.text or "", extracted.markdown


def extract_post_fetch_discovery(
    html: str | None,
    final_url: NormalizedUrl | None,
    *,
    js_base_url: NormalizedUrl | None = None,
    max_links: int = 250,
    include_diagnostics: bool = False,
    include_links: bool = False,
    include_js_endpoints: bool = False,
    include_metadata: bool = False,
    host_policy: HostPolicy,
) -> PostFetchDiscovery:
    """Sync post-fetch discovery for the result builders: parse HTML once and
    derive diagnostics, links, endpoints, and the hint in pure-CPU passes, with
    the hint egress-filtered at the producer (a builder once leaked ``javascript:``).
    """
    tree = parse_html_tree(html) if html and (include_diagnostics or include_links) else None
    envelope: PostFetchDiscovery = {
        "diagnostics": {},
        "discovered_links": [],
        "discovered_link_count": 0,
        "discovered_links_truncated": False,
        "js_endpoints": [],
        "next_page_url": None,
        "llm_metadata": {},
    }
    if include_diagnostics:
        # HtmlDiagnostics' values are the JSON spellings, so the envelope's
        # diagnostics row admits it directly.
        envelope["diagnostics"] = cast(
            "dict[str, JsonValue]", dict(html_diagnostics(html, final_url, tree=tree))
        )
    if include_links:
        links, count, truncated = extract_discovered_links(html, final_url, max_links=max_links, tree=tree)
        envelope["discovered_links"] = links
        envelope["discovered_link_count"] = count
        envelope["discovered_links_truncated"] = truncated
    if include_js_endpoints:
        envelope["js_endpoints"] = extract_js_endpoints(
            html,
            js_base_url if js_base_url is not None else final_url,
            host_policy=host_policy,
        )
    # Metadata rides the tree parsed for THIS call, never its own parse; an
    # empty dict means "no tree" and shaping falls back to its single parse.
    if include_metadata and tree is not None and final_url:
        envelope["llm_metadata"] = llm_metadata_from_tree(tree, final_url)
    envelope["next_page_url"] = detect_pagination(html, final_url)
    return envelope


# --- Katana-inspired advanced discovery ---


_JS_ENDPOINT_RE = re.compile(
    r"""(?:fetch|axios\.\w+|jQuery\.ajax|\$\.ajax|\$\.get|\$\.post)\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


_XHR_OPEN_RE = re.compile(
    r"""(?:xhr|xmlhttprequest)\.open\s*\(\s*["'][A-Z]+["'],\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def extract_js_endpoints(
    html: str | None,
    source_url: NormalizedUrl | None = None,
    *,
    host_policy: HostPolicy,
) -> list[dict[str, str]]:
    """Extract client-side API endpoints from JavaScript (``fetch``, ``axios.*``,
    jQuery ``$.*``, ``XMLHttpRequest.open``) so callers can queue dynamic backends,
    resolving against ``source_url`` when given or returning the raw string.
    """
    if not html:
        return []
    endpoints: list[dict[str, str]] = []
    seen: set[str] = set()
    for pattern in (_JS_ENDPOINT_RE, _XHR_OPEN_RE):
        for match in pattern.finditer(html):
            endpoint = match.group(1).strip()
            if not endpoint:
                continue
            if source_url is None:
                # No base to resolve against: keep the raw scrape. The
                # absolute-only filter below still admits or drops it.
                resolved = endpoint
            else:
                admitted = source_url.resolve(endpoint)
                # A refused endpoint can never survive the egress filter
                # downstream; dropping it here keeps raw text out of the list.
                if admitted is None:
                    continue
                resolved = admitted.raw
            if resolved in seen:
                continue
            seen.add(resolved)
            endpoints.append({"url": resolved})
    if source_url is None:
        return [
            endpoint
            for endpoint in endpoints
            if not endpoint["url"].lower().startswith(("http://", "https://"))
            or admit_or_none(endpoint["url"], policy=host_policy) is not None
        ]
    return filter_js_endpoints(endpoints, resolve_against=source_url, policy=host_policy)


_NEXT_REL_ANCHOR_RE = re.compile(r"<a\b[^>]*\brel=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)


_NEXT_REL_LINK_RE = re.compile(r"<link\b[^>]*\brel=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)


_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)


def detect_pagination(
    html: str | None,
    final_url: NormalizedUrl | None,
) -> NormalizedUrl | None:
    """Auto-detect the next-page URL via ``rel="next"`` (anchor or link), resolving
    against the final page URL, or ``None``. The href is attacker-controlled, so it
    leaves only through the egress filter here -- a non-http target becomes ``None``.
    """
    if not html or not final_url:
        return None
    for container_re in (_NEXT_REL_ANCHOR_RE, _NEXT_REL_LINK_RE):
        for tag in container_re.finditer(html):
            rels = tag.group(1).lower().split()
            if "next" not in rels:
                continue
            href_match = _HREF_RE.search(tag.group(0))
            if href_match:
                return final_url.resolve(href_match.group(1).strip())
    return None
