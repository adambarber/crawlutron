"""The extract-then-discover pipeline both strategies share, once spelled.

The shared text extractor, discovery envelope, and result fields mean a change
reaches both paths; policy stays with the caller, never fetch/decode/classify.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

from crawlutron.admission.policy import discovery_flags
from crawlutron.config.settings import CrawlerSettings
from crawlutron.extraction.discovery import (
    PostFetchDiscovery,
    extract_post_fetch_discovery,
    normalized_extraction,
)
from crawlutron.models.classification_evidence import ClassificationEvidenceRecord
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.models.page_metadata import PageMetadata
from crawlutron.safety.validation import filter_js_endpoints
from crawlutron_extract import extract as _package_extract
from crawlutron_extract.limits import ExtractLimits

if TYPE_CHECKING:
    from crawlutron.models.crawl_diagnostics import CrawlDiagnostics
    from crawlutron.models.crawl_extraction_method import CrawlExtractionMethod
    from crawlutron.models.crawl_policy import CrawlPolicy
    from safe_url.host_policy import HostPolicy
    from safe_url.normalized_url import NormalizedUrl


@dataclass(frozen=True)
class ExtractedContent:
    """A settled fact about a finished extraction, not a scratch value.

    Frozen because the receiving strategy has already decided what to do with it;
    the three fields are the three a crawl result carries.
    """

    text: str
    markdown: str | None
    extraction_method: str


def _extract_limits(settings: CrawlerSettings | None) -> ExtractLimits:
    """Map the crawl's resolved settings onto the package's extraction caps."""
    if settings is None:
        return ExtractLimits()
    return ExtractLimits(max_markdown_chars=settings.extraction_max_markdown)


async def run_extraction(
    *,
    extraction: CrawlExtractionMethod,
    content: str | bytes,
    decoded: str,
    content_type: str | None,
    final_url: NormalizedUrl | None,
    policy: CrawlPolicy,
    default_method: str,
    html: bool,
    markdown: bool,
    settings: CrawlerSettings | None = None,
) -> ExtractedContent:
    """Run extraction once off the loop through crawlutron_extract.extract.
    ``default_method`` stamps the result and ``settings`` maps onto
    ``ExtractLimits``; ``markdown`` gates retention of the package's markdown.
    """
    payload = content if isinstance(content, (bytes, bytearray)) else content.encode("utf-8", "replace")
    result = await asyncio.to_thread(
        _package_extract,
        payload,
        content_type or "",
        url=final_url.raw if final_url is not None else None,
        limits=_extract_limits(settings),
    )
    text, extracted_markdown = normalized_extraction(result)
    if not text:
        # Unparseable input degrades to the already-decoded body so nothing is
        # silently lost; the package's quality floor still marks it low/failed.
        text = decoded.strip() or ""
        extracted_markdown = text
    if not markdown:
        # Not requested: carry nothing (no retention, no wire).
        extracted_markdown = None
    elif not extracted_markdown:
        extracted_markdown = text
    return ExtractedContent(text=text, markdown=extracted_markdown, extraction_method=default_method)


async def discover_after_fetch(
    *,
    html: str | None,
    final_url: NormalizedUrl | None,
    js_base_url: NormalizedUrl | None,
    status: str,
    policy: CrawlPolicy,
    host_policy: HostPolicy,
) -> PostFetchDiscovery:
    """One post-fetch discovery parse shared by both strategies; switches are

    derived from the policy here, not passed in, so both paths cannot disagree
    on discovery for one page, and the metadata rides the same parse.
    """
    flags = discovery_flags(policy)
    discovery_block = any(flags)
    # channel on: a whole-site job is unbounded, and the links it follows are the whole
    # reason it exists.
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
        host_policy=host_policy,
    )


class DiscoveryResultFields(TypedDict):
    """The result fields both strategies take from one envelope, spread into
    ``build_engine_result`` so the key-to-field mapping is one decision."""

    classification_evidence: list[ClassificationEvidenceRecord]
    discovered_link_count: int
    discovered_links_truncated: bool
    discovered_links: list[DiscoveredLink]
    llm_metadata: PageMetadata | None


def discovery_result_fields(
    post_fetch: PostFetchDiscovery,
    *,
    classification_evidence: list[ClassificationEvidenceRecord],
) -> DiscoveryResultFields:
    """The result fields both strategies take from one envelope. Spreads into

    ``build_engine_result`` so the key-to-field mapping is one decision; a
    cross-path divergence would otherwise be invisible.
    """
    return {
        "classification_evidence": classification_evidence,
        "discovered_link_count": post_fetch["discovered_link_count"],
        "discovered_links_truncated": post_fetch["discovered_links_truncated"],
        "discovered_links": post_fetch["discovered_links"],
        "llm_metadata": post_fetch["llm_metadata"] or None,
    }


def post_fetch_diagnostics(
    post_fetch: PostFetchDiscovery,
    final_url: NormalizedUrl | None,
    host_policy: HostPolicy,
) -> CrawlDiagnostics:
    """The per-strategy diagnostics taken from one envelope, spelled once: the
    pagination hint rides only when the envelope found one, and the scraped JS
    endpoints always leave through ``filter_js_endpoints``.
    """
    diagnostics: CrawlDiagnostics = {}
    if post_fetch["next_page_url"]:
        # The envelope holds the admitted value; the diagnostics dict is a JSON
        # payload, so the wire projection happens here and nowhere else.
        diagnostics["next_page_url"] = post_fetch["next_page_url"].raw
    diagnostics["js_endpoints"] = filter_js_endpoints(
        post_fetch["js_endpoints"],
        resolve_against=final_url,
        policy=host_policy,
    )
    return diagnostics


__all__ = [
    "ExtractedContent",
    "PostFetchDiscovery",
    "discover_after_fetch",
    "discovery_result_fields",
    "post_fetch_diagnostics",
    "run_extraction",
]
