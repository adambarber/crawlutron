"""Unified response shaping for ``/v1/crawl`` and ``/v1/render``.

These pure-data transformation helpers were extracted from ``app/main.py``
so the routers (which own HTTP plumbing) and the response shapers (which
own the unified-schema mapping) live in different modules.

Public surface:
    :func:`format_crawl_item` — unified (markdown, metadata) formatter.
    :func:`build_unified_result` — engine-result dict → UnifiedCrawlResult.
    :func:`render_result_to_unified` — browser-render result dict → unified dict.
    :func:`build_unified_crawl_response` — wrap a list of results.
"""

from __future__ import annotations

from typing import Any

from app.api_models import PageMetadata, UnifiedCrawlResponse, UnifiedCrawlResult


def format_crawl_item(item: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Unified output formatter: produce ``(markdown, metadata_dict)`` for any result.

    Every access strategy (HTTP, browser, document/PDF, Trafilatura) funnels through
    this single path. The ``markdown`` field is populated by the unified extraction
    pipeline (trafilatura for web HTML, markitdown for documents, pass-through for plain
    text). For HTML pages the HTML metadata is extracted from the raw markup; results
    without HTML fall back to the strategy's extracted ``text`` so the consumer always
    receives a consistent Markdown payload regardless of how the content was acquired.
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


__all__ = [
    "format_crawl_item",
    "build_unified_result",
    "render_result_to_unified",
    "build_unified_crawl_response",
]
