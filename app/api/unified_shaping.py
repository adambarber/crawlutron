from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from app.extraction.llm_format import (
    PageMetadata as ExtractedPageMetadata,
)
from app.extraction.llm_format import (
    extract_carried_metadata,
)
from app.models.classification_evidence import ClassificationEvidence
from app.models.content_validators import ContentValidators
from app.models.crawl_result import CrawlResult, CrawlStrategyAttempt, EngineCrawlResult
from app.models.crawl_status import CrawlStatus
from app.models.crawl_strategy import CrawlStrategy
from app.models.discovered_link import DiscoveredLink
from app.models.discovered_link_response import DiscoveredLinkResponse as WireDiscoveredLink
from app.models.page_metadata_response import PageMetadataResponse
from app.models.strategy_attempt import StrategyAttempt
from app.models.taxonomy import CrawlStrategyName
from app.models.unified_crawl_result import UnifiedCrawlResult
from app.vocabulary.taxonomy import CrawlClassification

#: The batch shaper is generic over its result shape so ``adapt`` and ``results``
#: agree: browser results with an engine adapter are no longer expressible.


def format_crawl_item(item: EngineCrawlResult) -> tuple[str | None, ExtractedPageMetadata]:
    """Unified output formatter: produce ``(markdown, metadata)`` for any result.

    Every access strategy (HTTP, browser, document/PDF, Trafilatura) funnels through
    this single path. The ``markdown`` field is populated by the unified extraction
    pipeline (trafilatura for web HTML, markitdown for documents, pass-through for plain
    text). For HTML pages the HTML metadata is extracted from the raw markup; results
    without HTML fall back to the strategy's extracted ``text`` so the consumer always
    receives a consistent Markdown payload regardless of how the content was acquired.

    Engine-produced items carry ``llm_metadata`` -- page
    metadata computed ONCE from the discovery tree in the strategy stage --
    and this formatter reads that plain-data dict instead of rebuilding an
    lxml tree from the stored HTML. The reparse (``extract_llm_metadata``)
    stays as the honest fallback for items without a carried dict: a
    single-shot engine page (no discovery tree existed to read from) and
    any caller-built item that predates the field. Serialization audit for
    the carried field: it persists only in the engine spill (internal
    JSONL) and the job-runner's stored result is the already-shaped
    UnifiedCrawlResponse, so API responses, webhook payloads, and the MCP
    frontier carry ZERO new keys -- the metadata replaced its computation
    site, never the wire shape.
    """
    carried = item.llm_metadata
    url = item.final_url or item.normalized_url
    html = item.html
    markdown = item.markdown
    if markdown:
        if isinstance(carried, dict) and carried:
            return markdown, extract_carried_metadata(carried)
        if html and url is not None:
            from app.extraction.llm_format import extract_llm_metadata

            return markdown, extract_llm_metadata(html, url)
        return markdown, {}
    text = item.text
    if not text:
        return None, {}
    return text, {}


async def aformat_crawl_item(item: EngineCrawlResult) -> tuple[str | None, ExtractedPageMetadata]:
    """Async :func:`format_crawl_item`: the whole formatter leaves the loop.

    The fallback ``extract_llm_metadata`` path (items without a carried
    ``llm_metadata`` dict) still builds an lxml tree per item; on a batch of
    N such results the sync path parks the event loop N times. The async
    path runs the identical sync formatter in a worker thread;
    engine-produced items skip the tree entirely. Byte-
    identical to :func:`format_crawl_item` for every item shape.
    """
    return await asyncio.to_thread(format_crawl_item, item)


async def abuild_unified_result(item: EngineCrawlResult) -> UnifiedCrawlResult:
    """Async :func:`build_unified_result` (whole mapping off the event loop)."""
    return await asyncio.to_thread(build_unified_result, item)


def _coerce_strategy(value: CrawlStrategyName | None) -> CrawlStrategy | None:
    """The internal strategy literal as the wire enum, or None when none ran.

    No fallback branch, and that is the point: the internal and wire strategy
    taxonomies are the same set, which
    ``test_the_internal_and_wire_taxonomies_name_the_same_members`` asserts
    rather than assumes. The old ``except ValueError -> None`` existed to cover
    a member one vocabulary had and the other did not, and it hid exactly that:
    an unmapped strategy silently became "no strategy attempted" on the wire.
    """
    return CrawlStrategy(value) if value is not None else None


def _attempt_models(attempts: list[CrawlStrategyAttempt]) -> list[StrategyAttempt]:
    """The per-strategy attempt models, built from the dataclass's own attributes.

    ``from_attributes`` rather than a dict: the attempt is a declared record
    already, and asking pydantic to read it by attribute is what keeps this a
    conversion the checker can see instead of a coercion performed on the way
    into a constructor.
    """
    return [StrategyAttempt.model_validate(attempt, from_attributes=True) for attempt in attempts]


def _link_models(links: list[DiscoveredLink]) -> list[WireDiscoveredLink]:
    """The discovered-link models. See :func:`_attempt_models`."""
    return [WireDiscoveredLink.model_validate(link, from_attributes=True) for link in links]


def _evidence_models(evidence: list[dict[str, Any]]) -> list[ClassificationEvidence]:
    """The classification-evidence models.

    The one conversion still taking a raw dict, because the producer still
    builds evidence as plain JSON rather than as a declared record. It is the
    last such site in this module and the honest place to point a future wave.
    """
    return [ClassificationEvidence.model_validate(item) for item in evidence]


def _validator_model(validators: dict[str, str]) -> ContentValidators | None:
    """The origin-validator model, or None when the origin supplied none."""
    return ContentValidators.model_validate(validators) if validators else None


def build_unified_result(item: EngineCrawlResult) -> UnifiedCrawlResult:
    """Map an internal engine result into the unified API response model.

    The unified response carries both the LLM-friendly ``markdown`` and the
    full ``diagnostics`` payload. Page metadata comes from the result's
    carried ``llm_metadata`` or, when absent, the shared
    page-metadata extractor (``extract_llm_metadata``) over the raw HTML.
    """
    markdown, extracted = format_crawl_item(item)
    url = item.final_url or item.normalized_url
    metadata = PageMetadataResponse(**extracted)
    if metadata.source_url is None:
        metadata.source_url = url.raw if url is not None else item.original_url
    return UnifiedCrawlResult(
        original_url=item.original_url,
        normalized_url=item.normalized_url,
        final_url=item.final_url,
        # No fallback branch: the internal and wire taxonomies are the same set, and
        # the engine constructor already refuses a value outside its declared Literal.
        status=CrawlStatus(item.status),
        classification=CrawlClassification(item.classification),
        selected_strategy=_coerce_strategy(item.selected_strategy),
        attempted_strategies=_attempt_models(item.attempted_strategies),
        error=item.error,
        markdown=markdown,
        metadata=metadata,
        html=item.html,
        text=item.text,
        quality_score=item.quality_score,
        classification_evidence=_evidence_models(item.classification_evidence),
        http_status=item.http_status,
        headers=item.headers,
        content_type=item.content_type,
        extraction_method=item.extraction_method,
        diagnostics=item.diagnostics,
        discovered_link_count=item.discovered_link_count,
        discovered_links_truncated=item.discovered_links_truncated,
        discovered_links=_link_models(item.discovered_links),
        next_page_url=item.next_page_url,
        validators=_validator_model(item.validators),
    )


#: Browser metadata keys the wire model carries in a declared field, read from
#: ``diagnostics``; with the pair above, a new worker key maps by default, not away.
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


def render_result_to_unified(item: CrawlResult) -> UnifiedCrawlResult:
    """Adapt a browser ``CrawlResult`` into the unified result model.

    Returns the model rather than a dict shaped like one. Every field of
    ``UnifiedCrawlResult`` carries a default, so a dict that omitted one was a
    valid dict and no checker could see the omission -- which is how
    ``metadata={}`` shipped a ``/v1/render`` whose ``PageMetadata.title`` no
    input could fill. The constructor names the field set, so the fields this
    endpoint genuinely has nothing to say about are left at their defaults
    rather than transcribed here.
    """
    metadata = item.metadata
    status = CrawlStatus(item.status)
    classification = CrawlClassification(item.classification)
    selected_strategy = _coerce_strategy(item.selected_strategy)
    return UnifiedCrawlResult(
        # The render funnel stamps every request-produced result with the caller's
        # submitted text, so the wire key means the same thing at both doors.
        original_url=item.original_url if item.original_url is not None else item.url.raw,
        normalized_url=item.url,
        final_url=item.final_url,
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
                elapsed_ms=item.elapsed_ms,
                error=item.error,
            )
        ],
        error=item.error,
        metadata=PageMetadataResponse(title=metadata.get("title"), source_url=item.url.raw),
        html=item.html,
        text=item.text,
        quality_score=1.0 if item.status == "success" else 0.0,
        extraction_method=metadata.get("extraction_method"),
        diagnostics=_render_diagnostics(metadata),
        # The browser worker surfaces no origin validators: the field is typed
        # ``ContentValidators | None``, so the adapter constructs the model.
        validators=None,
    )


__all__ = [
    "abuild_unified_result",
    "aformat_crawl_item",
    "build_unified_result",
    "format_crawl_item",
    "render_result_to_unified",
]
