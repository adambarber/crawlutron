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
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin

from app.api_models import CrawlStatus, PageMetadata, UnifiedCrawlResponse, UnifiedCrawlResult
from app.constants import CrawlClassification
from app.validation import HostPolicy, safe_public_url_or_none

logger = logging.getLogger(__name__)


def format_crawl_item(item: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Unified output formatter: produce ``(markdown, metadata_dict)`` for any result.

    Every access strategy (HTTP, browser, document/PDF, Trafilatura) funnels through
    this single path. The ``markdown`` field is populated by the unified extraction
    pipeline (trafilatura for web HTML, markitdown for documents, pass-through for plain
    text). For HTML pages the HTML metadata is extracted from the raw markup; results
    without HTML fall back to the strategy's extracted ``text`` so the consumer always
    receives a consistent Markdown payload regardless of how the content was acquired.

    H2 (solved): engine-produced items carry ``llm_metadata`` -- page
    metadata computed ONCE from the discovery tree in the strategy stage --
    and this formatter reads that plain-data dict instead of rebuilding an
    lxml tree from the stored HTML. The reparse (``_extract_llm_metadata``)
    stays as the honest fallback for items without a carried dict: a
    single-shot engine page (no discovery tree existed to read from) and
    any caller-built item that predates the field. Serialization audit for
    the carried field: it persists only in the engine spill (internal
    JSONL) and the job-runner's stored result is the already-shaped
    UnifiedCrawlResponse, so API responses, webhook payloads, and the MCP
    frontier carry ZERO new keys -- the metadata replaced its computation
    site, never the wire shape.
    """
    carried = item.get("llm_metadata")
    url = item.get("final_url") or item.get("normalized_url") or item.get("original_url")
    html = item.get("html")
    markdown = item.get("markdown")
    if markdown:
        if isinstance(carried, dict) and carried:
            return markdown, dict(carried)
        if html and url:
            from app.crawl.llm_format import _extract_llm_metadata

            return markdown, _extract_llm_metadata(html, url)
        return markdown, {}
    text = item.get("text")
    if not text:
        return None, {}
    return text, {}


async def aformat_crawl_item(item: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Async :func:`format_crawl_item`: the whole formatter leaves the loop.

    The fallback ``_extract_llm_metadata`` path (items without a carried
    ``llm_metadata`` dict) still builds an lxml tree per item; on a batch of
    N such results the sync path parks the event loop N times. The async
    path runs the identical sync formatter in a worker thread (perf
    throughput #2); engine-produced items skip the tree entirely. Byte-
    identical to :func:`format_crawl_item` for every item shape.
    """
    return await asyncio.to_thread(format_crawl_item, item)


async def abuild_unified_result(item: dict[str, Any]) -> UnifiedCrawlResult:
    """Async :func:`build_unified_result` (whole mapping off the event loop)."""
    return await asyncio.to_thread(build_unified_result, item)


def _coerce_status(value: Any) -> CrawlStatus:
    """Read a per-URL status from an engine-result dict into the wire enum.

    The engine's internal ``EngineCrawlResult.status`` is a ``ResultStatus``
    literal and the spill round-trips it as a bare string; the wire model is
    the strict enum. A value outside the taxonomy is "failed", not a
    validation error -- the batch-shaping guard already degrades a malformed
    item to a terminal failure, so reaching this with an unknown member means
    the caller built a result the service itself never emits.
    """
    try:
        return CrawlStatus(value)
    except ValueError:
        return CrawlStatus.failed


def _coerce_classification(value: Any) -> CrawlClassification:
    """Read a per-URL classification from an engine-result dict into the wire enum.

    Mirrors :func:`_coerce_status`: the engine emits only taxonomy members, so
    a value that is not one means a malformed item, and "site_error" keeps the
    (status, classification) pair consistent (its status-table entry is
    "failed") rather than pairing an arbitrary classification with the failed
    status.
    """
    try:
        return CrawlClassification(value)
    except ValueError:
        return CrawlClassification.site_error


def build_unified_result(item: dict[str, Any]) -> UnifiedCrawlResult:
    """Map an internal engine result into the unified API response model.

    The unified response carries both the LLM-friendly ``markdown`` and the
    full ``diagnostics`` payload. Page metadata comes from the result's
    carried ``llm_metadata`` (H2) or, when absent, the shared
    page-metadata extractor (``_extract_llm_metadata``) over the raw HTML.
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
        status=_coerce_status(item["status"]),
        classification=_coerce_classification(item["classification"]),
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
    # The browser mode that actually served this render, threaded from the pool
    # by the worker. Falls back to None -- never an invented member -- when no
    # browser produced the item (a policy refusal, a worker that never ran).
    selected_strategy = item.get("selected_strategy")
    return {
        "original_url": item["url"],
        "normalized_url": item["url"],
        "final_url": item.get("final_url"),
        "status": item["status"],
        "classification": item["classification"],
        "selected_strategy": selected_strategy,
        "attempted_strategies": [
            {
                "strategy": selected_strategy,
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


def frontier_link_url(
    link: Any,
    *,
    policy: HostPolicy,
    resolve_against: str | None = None,
) -> str | None:
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
    if not isinstance(normalized, str) or not normalized:
        return None
    candidate = urljoin(resolve_against, normalized) if resolve_against else normalized
    validated = safe_public_url_or_none(candidate, policy=policy)
    return str(validated) if validated is not None else None


def _unified_response(results: list[UnifiedCrawlResult], **fields: Any) -> UnifiedCrawlResponse:
    return UnifiedCrawlResponse(results=results, **fields)


def build_unified_crawl_response(
    results: list[dict[str, Any]],
    *,
    request_id: str = "",
    policy_schema_version: str = "",
    success: bool = True,
    submitted_count: int | None = None,
) -> UnifiedCrawlResponse:
    """Build the unified crawl response from a list of engine-result dicts."""
    return _unified_response(
        _shape_all(results), request_id=request_id, policy_schema_version=policy_schema_version,
        success=success, submitted_count=submitted_count if submitted_count is not None else len(results),
    )


def _shape_all(results: list[dict[str, Any]], *, adapt: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> list[UnifiedCrawlResult]:
    """Shape a whole batch in one pass (the worker-thread side of
    :func:`abuild_unified_crawl_response`).

    One malformed item must not fail the batch: the job-runner path guards
    per-item (a dropped item counts as a failed crawl, not silence), and the
    endpoint must hold the same contract. The guard lives here rather than
    at each caller so the two paths cannot drift apart again.

    ``adapt`` is the optional per-item transform a pipeline applies before the
    unified shaping (``/v1/render`` maps its browser-result dicts through
    :func:`render_result_to_unified`). It runs INSIDE the same per-item guard,
    so an item the adapter cannot read degrades to a terminal failure for that
    item instead of failing the batch -- the same resilience ``/v1/crawl``
    already had, now extended to the render endpoint's adaptation step, which
    used to run unguarded in a list comprehension at the call site.
    """
    shaped: list[UnifiedCrawlResult] = []
    for item in results:
        try:
            adapted = adapt(item) if adapt is not None else item
            shaped.append(build_unified_result(adapted))
        except Exception:
            # Log loudly: the original bug was a bare except that swallowed
            # the exception, degraded the item to site_error, and left no
            # trace of which shape failed or why.
            logger.warning(
                "response shaping failed for one item; degrading it to a terminal failure",
                exc_info=True,
            )
            shaped.append(_failed_unified_result(item))
    return shaped


def _failed_unified_result(item: Any) -> UnifiedCrawlResult:
    """A shape failure becomes a terminal failure for that item, not the batch.

    Mirrors the job runner's semantics (``failed += 1`` on a dropped item).
    Fields that require no interpretation are carried through so the caller
    still sees which URL failed.

    This is the batch's last line of defense, so ``item`` is not trusted to
    be a dict: the guard already proved the per-item path raises, and a
    non-mapping item (``None``, a scalar, a mapping whose ``get`` itself
    explodes) must not turn the fallback into a second exception that
    escapes the guard and 500s the whole batch. Item reads are best-effort:
    whatever the item cannot answer degrades to the honest empty value.
    """
    peeked: dict[str, Any] = {}
    try:
        peeked = {
            key: item.get(key)
            for key in ("normalized_url", "original_url", "url", "final_url", "error")
        }
    except Exception:
        logger.debug("unshapable item refused even best-effort field reads", exc_info=True)
    url = peeked.get("normalized_url") or peeked.get("original_url") or peeked.get("url") or ""
    return UnifiedCrawlResult(
        original_url=peeked.get("original_url") or peeked.get("url") or url,
        normalized_url=url,
        final_url=peeked.get("final_url"),
        status=CrawlStatus.failed,
        # site_error is a FAILED_CLASSIFICATIONS member, so its status-table
        # entry is "failed" -- this keeps (status, classification) consistent
        # for any consumer that re-derives status from classification.
        # unknown_error maps to "blocked", which would disagree with the
        # hardcoded status above.
        classification=CrawlClassification.site_error,
        error=peeked.get("error") or "result_shaping_failed",
        # The remaining fields are declared without defaults in the pydantic
        # model even though the runtime treats them as optional (a bare
        # ``Field()`` accepts None), so state them explicitly: the item is
        # already malformed, the only honest payload is "nothing was extracted".
        selected_strategy=None,
        attempted_strategies=[],
        markdown=None,
        html=None,
        text=None,
        quality_score=0.0,
        http_status=None,
        content_type=None,
        extraction_method=None,
        discovered_link_count=0,
        discovered_links_truncated=False,
        next_page_url=None,
        validators=None,
    )


async def abuild_unified_crawl_response(
    results: list[dict[str, Any]],
    **options: Any,
) -> UnifiedCrawlResponse:
    request_id = options.pop("request_id", "")
    policy_schema_version = options.pop("policy_schema_version", "")
    success = options.pop("success", True)
    submitted_count = options.pop("submitted_count", None)
    adapt = options.pop("adapt", None)
    """Async :func:`build_unified_crawl_response` for use inside async endpoints.

    The whole batch is shaped in ONE event-loop offload (perf throughput #2):
    fanning one ``to_thread`` per item made every response pay N Future +
    thread-dispatch overheads, and the default executor serializes those
    submissions anyway once N exceeds the pool. Output is identical to the
    sync contract, which stays for sync callers and tests.

    ``adapt`` reaches ``_shape_all``'s per-item guard, so an item the adapter
    rejects degrades instead of failing the response.
    """
    shaped = await asyncio.to_thread(_shape_all, results, adapt=adapt) if results else []
    fields = dict(
        request_id=request_id, policy_schema_version=policy_schema_version, success=success,
        submitted_count=submitted_count if submitted_count is not None else len(results),
    )
    return _unified_response(list(shaped), **fields)


__all__ = [
    "aformat_crawl_item",
    "abuild_unified_result",
    "abuild_unified_crawl_response",
    "format_crawl_item",
    "build_unified_result",
    "render_result_to_unified",
    "build_unified_crawl_response",
]
