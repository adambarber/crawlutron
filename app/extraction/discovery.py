"""Link, JS-endpoint and pagination discovery over extracted trees."""

from __future__ import annotations

import re
from html import unescape
from typing import Any

from app.extraction.documents import ExtractedContent
from app.extraction.llm_format import (
    PageMetadata as ExtractedPageMetadata,
)
from app.extraction.llm_format import llm_metadata_from_tree
from app.extraction.trees import html_diagnostics, parse_html_tree
from app.models.discovered_link import DiscoveredLink
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.safety.validation import filter_js_endpoints


def extract_discovered_links(
    html: str | None,
    source_url: NormalizedUrl | None,
    *,
    max_links: int = 250,
    tree: Any | None = None,
) -> tuple[list[Any], int, bool]:
    if not html or not source_url:
        return [], 0, False
    source_host = source_url.host
    if tree is None:
        tree = parse_html_tree(html)
    if tree is None:
        return [], 0, False
    # Single pass: normalize/dedupe inline instead of materializing an
    # intermediate per-anchor dict list (memory on link-heavy pages).
    links: list[Any] = []
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
    """The ``(text, markdown)`` an extractor result contributes to a crawl result.

    Every caller of every extractor normalized the result the same two-line way
    -- ``extracted.text or ""`` and ``extracted.markdown`` -- so the
    normalization had as many copies as there were extractor call sites. The
    ``or ""`` is load-bearing: it is what keeps an extractor's ``None`` text
    from reaching a result at all, and a call site that omitted it produced a
    None that only surfaced further downstream.

    It lives here, beside ``ExtractedContent``, because that is the type whose
    invariant it is: the coercion is a fact about extractor output, not about
    any one strategy's result builder.
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
) -> dict[str, Any]:
    """Sync post-fetch discovery block for the strategy result builders.

    Parses the HTML once and derives frames/forms diagnostics, discovered
    links, raw JS endpoints, and the pagination hint in one pass. Pure CPU
    work with no I/O: both builders run it via ``asyncio.to_thread`` so page
    parsing never parks the event loop. Returns only plain data (dicts,
    lists, strings, DiscoveredLink values) -- the lxml tree never escapes.
    The pagination hint is egress-filtered HERE rather than at the call sites:
    this block used to promise each builder would keep its own filter
    contract, and the browser builder silently dropped it, so a
    ``javascript:`` rel=next escaped into the response. Filtering at the
    producer covers every consumer.
    """
    tree = parse_html_tree(html) if html and (include_diagnostics or include_links) else None
    diagnostics = html_diagnostics(html, final_url, tree=tree) if include_diagnostics else {}
    if include_links:
        discovered_links, discovered_link_count, discovered_links_truncated = extract_discovered_links(
            html, final_url, max_links=max_links, tree=tree
        )
    else:
        discovered_links, discovered_link_count, discovered_links_truncated = [], 0, False
    js_endpoints = (
        extract_js_endpoints(
            html,
            js_base_url if js_base_url is not None else final_url,
            host_policy=host_policy,
        )
        if include_js_endpoints
        else []
    )
    # Metadata rides the tree parsed for THIS call, never its own parse; an
    # empty dict means "no tree" and shaping falls back to its single parse.
    llm_metadata: ExtractedPageMetadata = {}
    if include_metadata and tree is not None and final_url:
        llm_metadata = llm_metadata_from_tree(tree, final_url)
    return {
        "diagnostics": diagnostics,
        "discovered_links": discovered_links,
        "discovered_link_count": discovered_link_count,
        "discovered_links_truncated": discovered_links_truncated,
        "js_endpoints": js_endpoints,
        "next_page_url": detect_pagination(html, final_url),
        "llm_metadata": llm_metadata,
    }


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
    """Extract client-side API endpoints referenced from JavaScript.

    Katana-style discovery: find ``fetch(...)``, ``axios.*(...)``, jQuery
    ``$.get/$.post/$.ajax``, and ``XMLHttpRequest.open(...)`` calls so the
    service can surface dynamic backend endpoints for callers to queue.

    Each match is returned as ``{"url": <resolved absolute URL>}``. When
    ``source_url`` is supplied the endpoint is resolved against it; otherwise
    the raw endpoint string is returned unresolved. Results are de-duplicated
    by their resolved URL.
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
    """Auto-detect the next-page URL via ``rel="next"`` hints.

    Checks both ``<a rel="next" href="...">`` and
    ``<link rel="next" href="...">`` and resolves the href against the
    final page URL. Returns ``None`` when no pagination hint is present.

    The href is attacker-controlled HTML, so the resolved URL leaves the
    service through the egress filter (the value's own ``resolve``) HERE rather
    than at each call site: a non-http(s) target (``javascript:``, ``data:``)
    becomes ``None``, and a consumer that forgot to filter cannot leak it.
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
