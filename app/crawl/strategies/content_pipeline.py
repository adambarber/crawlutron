"""The content pipeline both crawl strategies share: extract, then discover.

The browser strategy and the HTTP strategy each need the same three things out
of one fetched body: the text/markdown an extractor produced, the post-fetch
discovery envelope parsed off the page, and the part of that envelope which
belongs in a crawl result. All three were spelled once per strategy, which is
what the duplication report named: the three-way extraction dispatch, the
offloaded discovery call, and the result field block. They live here so a
change to the dispatch or to the envelope reaches both paths, and a field
threaded from discovery into a result can no longer be added to one strategy
and forgotten in the other -- a silent behavioural divergence between the
browser and HTTP results for the same page.

What stays with the caller is policy: which extraction mode, which content
type, the selector pair, the method label for the body/text mode, and the
discovery switches. Those are exactly the parts the two strategies genuinely
disagree about. This module must not fetch, decode a body (``body_decode``
owns that), classify a page, or assemble a result (``result_helpers`` owns
that).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.api_models import CrawlExtractionMethod, CrawlPolicy
from app.constants import (
    EXTRACTION_DOCUMENT,
    EXTRACTION_METHOD_DOCUMENT,
    EXTRACTION_METHOD_TRAFILATURA,
    EXTRACTION_TRAFILATURA,
)
from app.crawl.extraction import (
    Selectors,
    extract_content,
    extract_document,
    extract_html,
    extract_post_fetch_discovery,
    normalized_extraction,
)
from app.crawl.policy import discovery_flags
from app.models import CrawlError
from app.validation import HostPolicy, filter_js_endpoints


@dataclass(frozen=True)
class _ExtractedContent:
    """What one extraction contributes to a crawl result.

    Frozen because it is a settled fact about a finished extraction, not a
    scratch value: the strategy that receives it has already decided what it
    does with it, and the three fields are the three a result carries.
    """

    text: str
    markdown: str | None
    extraction_method: str


async def _run_extraction(
    *,
    extraction: CrawlExtractionMethod,
    content: str | bytes,
    decoded: str,
    content_type: str | None,
    final_url: str,
    policy: CrawlPolicy,
    default_method: str,
    html: bool,
) -> _ExtractedContent:
    """Run the configured extractor for one body, off the event loop.

    The three modes are dispatched here, once, so a fourth mode is a change to
    this function rather than a branch added to every strategy that fetches.
    ``content`` is the raw body the byte-oriented extractors take; ``decoded``
    is the same body's text form, which is what the HTML extractor reads. The
    browser path passes the same string for both, because a render hands over
    text it already decoded. ``policy`` carries the selector pair, which belongs
    to the two HTML paths only -- the document path takes neither, and handing
    it one is a TypeError -- and it is read here rather than shaped by each
    caller so that a selector honoured on one path cannot be dropped on the
    other for the same page. It is read into ONE :class:`Selectors` and handed
    whole to both HTML paths, so the two cannot be given different narrowing
    requests for the same page.

    ``html`` is the caller's claim that the body is HTML and it gates
    trafilatura alone: the HTTP path computes it from the content type, while
    the browser path passes the rendered page through either way, since a
    render that produced no markup still yields the worker's text and
    trafilatura falls back to it. ``default_method`` is the label for the
    body/text mode, whose name depends on how the body was obtained (HTML vs
    decoded body vs browser render).
    """
    selectors = Selectors(css=policy.css_selector, xpath=policy.xpath)
    if extraction == EXTRACTION_DOCUMENT:
        # The document extractor is typed for bytes; a browser render has text.
        payload = content.encode("utf-8", "replace") if isinstance(content, str) else content
        extracted = await asyncio.to_thread(extract_document, payload, final_url)
        method = EXTRACTION_METHOD_DOCUMENT
    elif extraction == EXTRACTION_TRAFILATURA:
        if not html:
            # No HTML, nothing for trafilatura to read: no text, no markdown.
            return _ExtractedContent("", None, EXTRACTION_METHOD_TRAFILATURA)
        extracted = await asyncio.to_thread(extract_html, decoded, final_url, selectors)
        method = EXTRACTION_METHOD_TRAFILATURA
    else:
        extracted = await asyncio.to_thread(extract_content, content, content_type, final_url, selectors)
        method = default_method
    text, markdown = normalized_extraction(extracted)
    return _ExtractedContent(text=text, markdown=markdown, extraction_method=method)


async def _discover_after_fetch(
    *,
    html: str | None,
    final_url: str,
    js_base_url: str | None,
    status: str,
    policy: CrawlPolicy,
    host_policy: HostPolicy,
) -> dict[str, Any]:
    """The post-fetch discovery envelope, parsed off the event loop.

    One call site for the CPU-bound page parse, because the two strategies
    differ only in the switches they pass and must not differ in whether the
    parse happens at all. ``include_metadata`` is not a switch: the page
    metadata rides the tree this call already parsed, and the result's
    ``llm_metadata`` field is fed from here by both strategies.

    The switches are derived from the policy here rather than taken as
    arguments, which is the point of the signature. They used to be passed in,
    and the two strategies passed different answers for one policy: the browser
    path asked for links, diagnostics and JS endpoints unconditionally while
    the HTTP path asked for them only when a discovery channel was on or the
    call had a depth to spend. So the same policy discovered links on a
    browser-rendered page and not on an HTTP-fetched one, and the divergence
    was invisible in both results -- each was individually well-formed. A
    caller cannot pass a wrong combination here because there is no wrong one
    to pass.
    """
    flags = discovery_flags(policy)
    discovery_block = any(flags)
    # A depth to spend is a reason to look for links even with no discovery
    # channel on: a whole-site job is unbounded, and the links it follows are
    # the whole reason it exists.
    want_links = discovery_block or policy.limits.max_depth > 0
    return await asyncio.to_thread(
        extract_post_fetch_discovery,
        html,
        final_url,
        js_base_url=js_base_url,
        max_links=policy.limits.max_discovered_links,
        include_diagnostics=discovery_block,
        include_links=status == "success" and want_links,
        include_js_endpoints=discovery_block,
        include_metadata=True,
        host_policy=host_policy,
    )


def _discovery_result_fields(
    post_fetch: dict[str, Any],
    *,
    classification_evidence: list[dict[str, Any]],
    error: CrawlError | None,
) -> dict[str, Any]:
    """The result fields both strategies take from one discovery envelope.

    Spread into ``build_engine_result`` as ``**_discovery_result_fields(...)``.
    The envelope's keys and the result field they feed are one decision, so it
    is written once: a discovered-links field that reached the HTTP result but
    not the browser result would be a divergence nothing else in the tree can
    see. ``classification_evidence`` and ``error`` ride along because both
    strategies pass them at the same point in the same block, and ``error`` is
    the one field of the tail that is not read off the envelope.
    """
    return {
        "classification_evidence": classification_evidence,
        "discovered_link_count": post_fetch["discovered_link_count"],
        "discovered_links_truncated": post_fetch["discovered_links_truncated"],
        "discovered_links": post_fetch["discovered_links"],
        "llm_metadata": post_fetch["llm_metadata"] or None,
        "error": error,
    }


def _post_fetch_diagnostics(
    post_fetch: dict[str, Any],
    final_url: str,
    host_policy: HostPolicy,
) -> dict[str, Any]:
    """The diagnostics every strategy takes from one discovery envelope.

    Merge into the strategy's own diagnostics dict. The two rules here are the
    ones that must not differ between the browser and HTTP answers to the same
    page: the pagination hint is surfaced only when the envelope found one, and
    the scraped JS endpoints leave through ``filter_js_endpoints`` rather than
    raw. Spelled per strategy, one of the two could stop filtering while the
    other kept passing its tests.
    """
    diagnostics: dict[str, Any] = {}
    if post_fetch["next_page_url"]:
        diagnostics["next_page_url"] = post_fetch["next_page_url"]
    diagnostics["js_endpoints"] = filter_js_endpoints(
        post_fetch["js_endpoints"],
        resolve_against=final_url,
        policy=host_policy,
    )
    return diagnostics


__all__: list[str] = []
