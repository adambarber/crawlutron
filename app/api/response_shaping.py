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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from app.api_models import (
    CrawlStatus,
    PageMetadata,
    StrategyAttempt,
    UnifiedCrawlResponse,
    UnifiedCrawlResult,
)
from app.constants import CrawlClassification
from app.validation import HostPolicy, safe_public_url_or_none

_logger = logging.getLogger(__name__)


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
    path runs the identical sync formatter in a worker thread;
    engine-produced items skip the tree entirely. Byte-
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
    carried ``llm_metadata`` or, when absent, the shared
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


#: Browser ``CrawlResult.metadata`` keys the wire model carries in a declared
#: field, so they are read out of ``diagnostics`` rather than duplicated there.
#: This set and :data:`_RENDER_METADATA_AS_TOP_LEVEL` together name EVERY key
#: the browser worker emits; anything else in ``metadata`` passes through. The
#: pair is the whole browser->wire mapping, so a new worker key lands in
#: ``diagnostics`` by default instead of being filtered away by name.
_RENDER_METADATA_AS_MODEL_FIELD = frozenset({"title"})
_RENDER_METADATA_AS_TOP_LEVEL = frozenset({"extraction_method"})


def _render_diagnostics(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """What the browser worker's ``metadata`` becomes on the render wire.

    Every key survives: the two the model carries in a declared field are read
    out of here, the rest pass through untouched. Naming the two exclusions by
    the field that holds them -- rather than excluding five keys and re-adding
    one -- is what makes a dropped key visible, and it is why a new worker key
    lands in ``diagnostics`` by default instead of being filtered away.
    """
    diagnostics: dict[str, Any] = {
        key: value
        for key, value in metadata.items()
        if key not in _RENDER_METADATA_AS_MODEL_FIELD | _RENDER_METADATA_AS_TOP_LEVEL
    }
    diagnostics["browser_profile"] = diagnostics.get("browser_profile") or {}
    return diagnostics


def render_result_to_unified(item: dict[str, Any]) -> UnifiedCrawlResult:
    """Adapt a browser ``CrawlResult`` into the unified result model.

    Returns the model rather than a dict shaped like one. Every field of
    ``UnifiedCrawlResult`` carries a default, so a dict that omitted one was a
    valid dict and no checker could see the omission -- which is how
    ``metadata={}`` shipped a ``/v1/render`` whose ``PageMetadata.title`` no
    input could fill. The constructor names the field set, so the fields this
    endpoint genuinely has nothing to say about are left at their defaults
    rather than transcribed here.
    """
    metadata = item.get("metadata") or {}
    status = _coerce_status(item["status"])
    classification = _coerce_classification(item["classification"])
    selected_strategy = item.get("selected_strategy")
    return UnifiedCrawlResult(
        original_url=item["url"],
        normalized_url=item["url"],
        final_url=item.get("final_url"),
        status=status,
        classification=classification,
        selected_strategy=selected_strategy,
        attempted_strategies=[
            StrategyAttempt(
                strategy=selected_strategy,
                status=status,
                classification=classification,
                http_status=None,
                content_type=None,
                elapsed_ms=int(item.get("elapsed_ms", 0)),
                error=item.get("error"),
            )
        ],
        error=item.get("error"),
        metadata=PageMetadata(title=metadata.get("title"), source_url=item["url"]),
        html=item.get("html"),
        text=item.get("text"),
        quality_score=1.0 if item["status"] == "success" else 0.0,
        extraction_method=metadata.get("extraction_method"),
        diagnostics=_render_diagnostics(metadata),
        # The browser worker surfaces no origin validators. The field is typed
        # ``ContentValidators | None`` and has no model default, so this was a
        # real omission until the adapter started constructing the model: the
        # old dict path let ``build_unified_result`` supply the fallback
        # silently, and pyright only saw it once the constructor was explicit.
        validators=None,
    )


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


@dataclass(frozen=True, slots=True)
class EnvelopeOptions:
    """The scalar fields a response envelope carries, beside its shaped results.

    One record rather than four keyword parameters on each of two builders: the
    parameters were the reason the sync and async signatures were a six-line
    identical fragment, and a twin signature is a contract that has to be edited
    twice. The fields stay named and explicit -- a misspelled or unpassed
    envelope field is a field the response silently lacks, because
    ``UnifiedCrawlResponse`` drops one rather than raising -- so this is a place
    to add a field, not a ``**options`` bag that would take any.
    """

    request_id: str = ""
    policy_schema_version: str = ""
    success: bool = True
    submitted_count: int | None = None


def _response_envelope(
    shaped: list[UnifiedCrawlResult],
    result_count: int,
    options: EnvelopeOptions,
) -> UnifiedCrawlResponse:
    """The one construction of the response envelope: shaped results plus every scalar field.

    The sync builder, the async builder, and every endpoint's raw-result
    adaptation funnel through here, so the field list exists once. Every field
    of :class:`UnifiedCrawlResponse` is required by the model, so a new one has
    to be passed HERE or the whole tree starts raising at the first request --
    which is the loud outcome; the quiet one is a builder that drops it.

    The ``submitted_count`` fallback -- the count the wire reports when the
    caller could not say how many URLs were admitted -- used to be spelled in
    both builders, and two copies of a fallback is how two paths start
    disagreeing about a batch's size. ``test_abuild_unified_crawl_response_matches_sync``
    is what proves they still agree.

    ``result_count`` is the length of the RAW result list, passed separately
    from ``shaped`` on purpose: counting the shaped list instead would make a
    dropped item silently change a number the response claims was submitted.
    """
    return UnifiedCrawlResponse(
        results=shaped,
        request_id=options.request_id,
        policy_schema_version=options.policy_schema_version,
        success=options.success,
        submitted_count=(result_count if options.submitted_count is None else options.submitted_count),
    )


def build_unified_crawl_response(
    results: list[dict[str, Any]],
    *,
    options: EnvelopeOptions | None = None,
) -> UnifiedCrawlResponse:
    """Build the unified crawl response from a list of engine-result dicts.

    The async twin below is the same contract with the shaping off the event
    loop; both hand their options to :func:`_response_envelope`, so the response
    they build is decided in one place.
    """
    return _response_envelope(_shape_all(results), len(results), options or EnvelopeOptions())


def _shape_all(
    results: list[dict[str, Any]], *, adapt: Callable[[dict[str, Any]], UnifiedCrawlResult] | None = None
) -> list[UnifiedCrawlResult]:
    """Shape a whole batch in one pass (the worker-thread side of
    :func:`abuild_unified_crawl_response`).

    One malformed item must not fail the batch: the job-runner path guards
    per-item (a dropped item counts as a failed crawl, not silence), and the
    endpoint must hold the same contract. The guard lives here rather than
    at each caller so the two paths cannot drift apart again.

    ``adapt`` is the optional per-item transform a pipeline applies before the
    unified shaping (``/v1/render`` maps its browser-result dicts through
    :func:`render_result_to_unified`). It is typed as a transform INTO the
    model rather than into a dict-shaped one, so an adapter cannot return a
    partial item for ``build_unified_result`` to reinterpret -- that round trip
    through a dict is where a required field could be omitted without any
    checker noticing. It runs INSIDE the same per-item guard,
    so an item the adapter cannot read degrades to a terminal failure for that
    item instead of failing the batch -- the same resilience ``/v1/crawl``
    already had, now extended to the render endpoint's adaptation step, which
    used to run unguarded in a list comprehension at the call site.
    """
    shaped: list[UnifiedCrawlResult] = []
    for item in results:
        try:
            shaped.append(adapt(item) if adapt is not None else build_unified_result(item))
        except Exception:
            # Log loudly: the original bug was a bare except that swallowed
            # the exception, degraded the item to site_error, and left no
            # trace of which shape failed or why.
            _logger.warning(
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
            key: item.get(key) for key in ("normalized_url", "original_url", "url", "final_url", "error")
        }
    except Exception:
        _logger.debug("unshapable item refused even best-effort field reads", exc_info=True)
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
    )


async def abuild_unified_crawl_response(
    results: list[dict[str, Any]],
    *,
    options: EnvelopeOptions | None = None,
    adapt: Callable[[dict[str, Any]], UnifiedCrawlResult] | None = None,
) -> UnifiedCrawlResponse:
    """Async :func:`build_unified_crawl_response` for use inside async endpoints.

    The whole batch is shaped in ONE event-loop offload:
    fanning one ``to_thread`` per item made every response pay N Future +
    thread-dispatch overheads, and the default executor serializes those
    submissions anyway once N exceeds the pool. Output is identical to the
    sync contract, which stays for sync callers and tests.

    ``adapt`` reaches ``_shape_all``'s per-item guard, so an item the adapter
    rejects degrades instead of failing the response.
    """
    shaped = await asyncio.to_thread(_shape_all, results, adapt=adapt) if results else []
    return _response_envelope(shaped, len(results), options or EnvelopeOptions())


async def _aunified_response_from_result(
    result: dict[str, Any], *, adapt: Callable[[dict[str, Any]], UnifiedCrawlResult] | None = None
) -> UnifiedCrawlResponse:
    """The one adapter from a raw producer envelope to the unified response.

    Both wire endpoints (``/v1/crawl`` and ``/v1/render``) read their envelope
    here, so an envelope field is passed once and cannot reach one path while
    being forgotten on the other. That mistake is silent by construction --
    ``UnifiedCrawlResponse`` drops an unpassed field rather than raising -- and
    a response field has already shipped missing from one endpoint and been
    caught only by curling the live endpoint.

    ``adapt`` is the per-item transform a producer needs (render maps its
    browser-result dicts through :func:`render_result_to_unified`); it runs
    inside ``_shape_all``'s per-item guard either way, so a result dict the
    adapter cannot read degrades to a terminal failure for that item instead of
    500ing the response.
    """
    return await abuild_unified_crawl_response(
        result.get("results", []),
        options=EnvelopeOptions(
            request_id=result["request_id"],
            submitted_count=result["submitted_count"],
            policy_schema_version=result["policy_schema_version"],
        ),
        adapt=adapt,
    )


__all__ = [
    "EnvelopeOptions",
    "abuild_unified_crawl_response",
    "abuild_unified_result",
    "aformat_crawl_item",
    "build_unified_crawl_response",
    "build_unified_result",
    "format_crawl_item",
    "frontier_link_url",
    "render_result_to_unified",
]
