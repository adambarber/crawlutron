"""Unified response shaping for ``/v1/crawl`` and ``/v1/render``.

These pure-data transformation helpers were extracted from ``app/main.py``
so the routers (which own HTTP plumbing) and the response shapers (which
own the unified-schema mapping) live in different modules.

Public surface:
    :func:`format_crawl_item` — unified (markdown, metadata) formatter.
    :func:`build_unified_result` — engine-result dict → UnifiedCrawlResult.
    :func:`render_result_to_unified` — browser-render result dict → unified dict.
    :func:`build_unified_crawl_response` — wrap a list of results.
    :func:`frontier_link_url` — the only frontier/visited-eligible link field
        (validated ``normalized_url``; raw hrefs are unqueue-able).
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.api_models import PageMetadata, UnifiedCrawlResponse, UnifiedCrawlResult
from app.constants import CrawlClassification


def format_crawl_item(item: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Unified output formatter: produce ``(markdown, metadata_dict)`` for any result.

    Every access strategy (HTTP, browser, document/PDF, Trafilatura) funnels through
    this single path. The ``markdown`` field is populated by the unified extraction
    pipeline (trafilatura for web HTML, markitdown for documents, pass-through for plain
    text). For HTML pages the HTML metadata is extracted from the raw markup; results
    without HTML fall back to the strategy's extracted ``text`` so the consumer always
    receives a consistent Markdown payload regardless of how the content was acquired.

    PERF TODO (P2, deferred): this is the third HTML scan for an HTTP page -- the
    strategy already parsed the body once for trafilatura (``_extract_html``) and
    once more for link/frame/form discovery (``extract_post_fetch_discovery``, one
    shared lxml tree). ``_extract_llm_metadata`` reparses here to read title/meta/
    JSON-LD. The natural fix is to read the metadata from the discovery tree and
    thread it to shaping; that is deferred because the engine result this shapes is
    persisted (job store, recursive-crawl spill) and reaches the API response and
    webhook payloads, so a carried-along field needs an explicit, audited exclusion
    from every serialization site, and consuming the discovery tree would invert the
    llm_format -> extraction lazy-import discipline (circular-import risk). The parse
    is already off the event loop (aformat_crawl_item / the batched offload), so this
    is CPU saved, not latency.
    """
    from app.crawl.llm_format import _extract_llm_metadata

    url = item.get("final_url") or item.get("normalized_url") or item.get("original_url")
    html = item.get("html")
    markdown = item.get("markdown")
    if markdown:
        if html and url:
            return markdown, _extract_llm_metadata(html, url)
        return markdown, {}
    text = item.get("text")
    if not text:
        return None, {}
    return text, {}


async def aformat_crawl_item(item: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Async :func:`format_crawl_item`: the whole formatter leaves the loop.

    ``_extract_llm_metadata`` builds a second lxml tree per item; on a batch
    of N results the sync path parks the event loop N times. The async path
    runs the identical sync formatter in a worker thread (perf throughput
    #2). Results without HTML take the same early returns (no thread hop
    cost beyond the wrapper). Byte-identical to :func:`format_crawl_item`
    for every item shape.
    """
    return await asyncio.to_thread(format_crawl_item, item)


async def abuild_unified_result(item: dict[str, Any]) -> UnifiedCrawlResult:
    """Async :func:`build_unified_result` (whole mapping off the event loop)."""
    return await asyncio.to_thread(build_unified_result, item)


def build_unified_result(item: dict[str, Any]) -> UnifiedCrawlResult:
    """Map an internal engine result into the unified API response model.

    The unified response carries both the LLM-friendly ``markdown`` and the
    full ``diagnostics`` payload. Page metadata is produced by the
    shared page-metadata extractor (``_extract_llm_metadata``) from the
    raw HTML when available.
    """
    markdown, metadata_dict = format_crawl_item(item)
    url = item.get("final_url") or item.get("normalized_url") or item.get("original_url")
    metadata = PageMetadata(
        **{key: value for key, value in metadata_dict.items() if key in PageMetadata.model_fields}
    )
    if metadata.source_url is None:
        metadata.source_url = url
    return UnifiedCrawlResult(
        original_url=item["original_url"],
        normalized_url=item.get("normalized_url"),
        final_url=item.get("final_url"),
        status=item["status"],
        classification=item["classification"],
        selected_strategy=item.get("selected_strategy"),
        attempted_strategies=item.get("attempted_strategies", []),
        error=item.get("error"),
        markdown=markdown,
        metadata=metadata,
        html=item.get("html"),
        text=item.get("text"),
        quality_score=item.get("quality_score", 0.0),
        classification_evidence=item.get("classification_evidence", []),
        http_status=item.get("http_status"),
        headers=item.get("headers", {}),
        content_type=item.get("content_type"),
        extraction_method=item.get("extraction_method"),
        diagnostics=item.get("diagnostics", {}),
        discovered_link_count=item.get("discovered_link_count", 0),
        discovered_links_truncated=item.get("discovered_links_truncated", False),
        discovered_links=item.get("discovered_links", []),
        next_page_url=item.get("next_page_url"),
        validators=item.get("validators") or None,
    )


def render_result_to_unified(item: dict[str, Any]) -> dict[str, Any]:
    """Adapt a browser ``CrawlResult`` dict into the unified result shape.

    ``/v1/render`` reuses the same browser worker as the universal crawl path,
    so its output is normalized through the identical ``build_unified_result``
    pipeline. Browser-specific discovery (intercepted XHR, screenshots) lives in
    the result ``metadata`` dict and is carried into ``diagnostics`` so the
    unified schema stays the single source of truth for both endpoints.
    """
    metadata = item.get("metadata") or {}
    diagnostics: dict[str, Any] = {
        key: value
        for key, value in metadata.items()
        if key not in {"title", "content_length", "browser_reused", "challenge_resolved", "extraction_method"}
    }
    diagnostics.setdefault("browser_profile", {})
    return {
        "original_url": item["url"],
        "normalized_url": item["url"],
        "final_url": item.get("final_url"),
        "status": item["status"],
        "classification": item["classification"],
        "selected_strategy": None,
        "attempted_strategies": [
            {
                "strategy": "BROWSER_HEADLESS",
                "status": item["status"],
                "classification": item["classification"],
                "http_status": None,
                "content_type": None,
                "elapsed_ms": int(item.get("elapsed_ms", 0)),
                "error": item.get("error"),
            }
        ],
        "error": item.get("error"),
        "markdown": None,
        "metadata": {},
        "html": item.get("html"),
        "text": item.get("text"),
        "quality_score": 1.0 if item["status"] == "success" else 0.0,
        "classification_evidence": [],
        "http_status": None,
        "headers": {},
        "content_type": None,
        "extraction_method": metadata.get("extraction_method"),
        "diagnostics": diagnostics,
        "discovered_link_count": 0,
        "discovered_links_truncated": False,
        "discovered_links": [],
        "next_page_url": None,
    }


def frontier_link_url(link: Any) -> str | None:
    """The only link field eligible for frontier/visited use: the validated URL.

    Extraction can legitimately emit a raw ``url`` href (``/rel``, ``#frag``,
    scheme-relative) alongside the SafeUrl-policy-validated ``normalized_url``.
    Frontier and visited sets are keyed on absolute, validated URLs, so the raw
    ``url`` must never enter them. A link without a usable ``normalized_url``
    (or a non-link string) yields None: unqueue-able, not a raw-href fallback.
    Attribute-held links (e.g. ``DiscoveredLink`` dataclasses) read the same
    validated field; the raw href is never consulted in any shape.
    """
    if isinstance(link, dict):
        normalized = link.get("normalized_url")
    else:
        normalized = getattr(link, "normalized_url", None)
    if isinstance(normalized, str) and normalized:
        return normalized
    return None


def build_unified_crawl_response(
    results: list[dict[str, Any]],
    *,
    request_id: str = "",
    policy_schema_version: str = "",
    success: bool = True,
    submitted_count: int | None = None,
) -> UnifiedCrawlResponse:
    """Build the unified crawl response from a list of engine-result dicts."""
    return UnifiedCrawlResponse(
        request_id=request_id,
        submitted_count=submitted_count if submitted_count is not None else len(results),
        policy_schema_version=policy_schema_version,
        success=success,
        results=[build_unified_result(item) for item in results],
    )


def _shape_all(results: list[dict[str, Any]]) -> list[UnifiedCrawlResult]:
    """Shape a whole batch in one pass (the worker-thread side of
    :func:`abuild_unified_crawl_response`).

    One malformed item must not fail the batch: the job-runner path guards
    per-item (a dropped item counts as a failed crawl, not silence), and the
    endpoint must hold the same contract. The guard lives here rather than
    at each caller so the two paths cannot drift apart again.
    """
    shaped: list[UnifiedCrawlResult] = []
    for item in results:
        try:
            shaped.append(build_unified_result(item))
        except Exception:
            shaped.append(_failed_unified_result(item))
    return shaped


def _failed_unified_result(item: dict[str, Any]) -> UnifiedCrawlResult:
    """A shape failure becomes a terminal failure for that item, not the batch.

    Mirrors the job runner's semantics (``failed += 1`` on a dropped item).
    Fields that require no interpretation are carried through so the caller
    still sees which URL failed.
    """
    url = item.get("normalized_url") or item.get("original_url") or ""
    return UnifiedCrawlResult(
        original_url=item.get("original_url") or url,
        normalized_url=url,
        final_url=item.get("final_url"),
        status="failed",
        # site_error is a FAILED_CLASSIFICATIONS member, so its status-table
        # entry is "failed" -- this keeps (status, classification) consistent
        # for any consumer that re-derives status from classification.
        # unknown_error maps to "blocked", which would disagree with the
        # hardcoded status above.
        classification=CrawlClassification.site_error.value,
        error=item.get("error") or "result_shaping_failed",
    )


async def abuild_unified_crawl_response(
    results: list[dict[str, Any]],
    *,
    request_id: str = "",
    policy_schema_version: str = "",
    success: bool = True,
    submitted_count: int | None = None,
) -> UnifiedCrawlResponse:
    """Async :func:`build_unified_crawl_response` for use inside async endpoints.

    The whole batch is shaped in ONE event-loop offload (perf throughput #2):
    fanning one ``to_thread`` per item made every response pay N Future +
    thread-dispatch overheads, and the default executor serializes those
    submissions anyway once N exceeds the pool. Output is identical to the
    sync contract, which stays for sync callers and tests.
    """
    shaped = await asyncio.to_thread(_shape_all, results) if results else []
    return UnifiedCrawlResponse(
        request_id=request_id,
        submitted_count=submitted_count if submitted_count is not None else len(results),
        policy_schema_version=policy_schema_version,
        success=success,
        results=list(shaped),
    )


__all__ = [
    "aformat_crawl_item",
    "abuild_unified_result",
    "abuild_unified_crawl_response",
    "format_crawl_item",
    "build_unified_result",
    "render_result_to_unified",
    "build_unified_crawl_response",
]
