from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import cast

from crawlutron.extraction.llm_format import (
    PageMetadata as ExtractedPageMetadata,
)
from crawlutron.extraction.llm_format import (
    extract_carried_metadata,
)
from crawlutron.models.classification_evidence import ClassificationEvidence, ClassificationEvidenceRecord
from crawlutron.models.content_validators import ContentValidators
from crawlutron.models.crawl_result import CrawlResult, EngineCrawlResult
from crawlutron.models.crawl_status import CrawlStatus
from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron.models.crawl_strategy_attempt import CrawlStrategyAttempt
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.models.discovered_link_response import DiscoveredLinkResponse as WireDiscoveredLink
from crawlutron.models.json_value import JsonValue
from crawlutron.models.page_metadata_response import PageMetadataResponse
from crawlutron.models.serialization import to_dict as _result_to_dict
from crawlutron.models.strategy_attempt import StrategyAttempt
from crawlutron.models.taxonomy import CrawlStrategyName
from crawlutron.models.unified_crawl_result import UnifiedCrawlResult
from crawlutron.vocabulary.taxonomy import CrawlClassification

#: The batch shaper is generic over its result shape so ``adapt`` and ``results``
#: agree: browser results with an engine adapter are no longer expressible.


def format_crawl_item(item: EngineCrawlResult) -> tuple[str | None, ExtractedPageMetadata]:
    """Unified output formatter: ``(markdown, metadata)`` for any result, through one
    path. Metadata comes from the carried ``llm_metadata`` when present, else from
    ``extract_llm_metadata`` over the HTML; the carried field adds no new wire keys.
    """
    carried = item.llm_metadata
    url = item.final_url or item.normalized_url
    html = item.html
    markdown = item.markdown
    if markdown:
        if isinstance(carried, dict) and carried:
            return markdown, extract_carried_metadata(carried)
        if html and url is not None:
            from crawlutron.extraction.llm_format import extract_llm_metadata

            return markdown, extract_llm_metadata(html, url)
        return markdown, {}
    text = item.text
    if not text:
        return None, {}
    return text, {}


async def aformat_crawl_item(item: EngineCrawlResult) -> tuple[str | None, ExtractedPageMetadata]:
    """Async :func:`format_crawl_item`: the whole formatter leaves the loop.
    The fallback tree-reparse would otherwise park the event loop once per item;
    runs the identical sync formatter on a worker thread.
    """
    return await asyncio.to_thread(format_crawl_item, item)


async def abuild_unified_result(item: EngineCrawlResult) -> UnifiedCrawlResult:
    """Async :func:`build_unified_result` (whole mapping off the event loop)."""
    return await asyncio.to_thread(build_unified_result, item)


def _coerce_strategy(value: CrawlStrategyName | None) -> CrawlStrategy | None:
    """The internal strategy literal as the wire enum, or None when none ran.
    No fallback branch: the internal and wire taxonomies are the same set
    (a test asserts it), so an unmapped strategy must not silently become "none attempted".
    """
    return CrawlStrategy(value) if value is not None else None


def _attempt_models(attempts: list[CrawlStrategyAttempt]) -> list[StrategyAttempt]:
    """The per-strategy attempt models, built from the dataclass's own attributes.
    ``from_attributes`` keeps this a conversion the checker can see, not a
    coercion performed on the way into a constructor.
    """
    return [StrategyAttempt.model_validate(attempt, from_attributes=True) for attempt in attempts]


def _link_models(links: list[DiscoveredLink]) -> list[WireDiscoveredLink]:
    """The discovered-link models. See :func:`_attempt_models`."""
    return [WireDiscoveredLink.model_validate(link, from_attributes=True) for link in links]


def _evidence_models(evidence: list[ClassificationEvidenceRecord]) -> list[ClassificationEvidence]:
    """The classification-evidence models.
    The one conversion still taking a raw dict -- the producer still builds
    evidence as plain JSON -- and the honest place to point a future wave.
    """
    return [ClassificationEvidence.model_validate(item) for item in evidence]


def _validator_model(validators: dict[str, str]) -> ContentValidators | None:
    """The origin-validator model, or None when the origin supplied none."""
    return ContentValidators.model_validate(validators) if validators else None


def _metadata_response(extracted: ExtractedPageMetadata) -> PageMetadataResponse:
    """Project an extractor metadata key-set to the wire model, loudly.

    A key the wire model does not declare is an error, never the silent drop the
    ``extra='ignore'`` splat would allow.
    """
    declared = set(PageMetadataResponse.model_fields)
    unknown = set(extracted) - declared
    if unknown:
        raise ValueError(f"unhandled extractor metadata keys: {sorted(unknown)}")
    return PageMetadataResponse(**extracted)


def build_unified_result(item: EngineCrawlResult) -> UnifiedCrawlResult:
    """Map an internal engine result into the unified API response model.
    The response carries both LLM-friendly ``markdown`` and full ``diagnostics``;
    page metadata comes from the carried ``llm_metadata`` or the shared extractor over the HTML.
    """
    markdown, extracted = format_crawl_item(item)
    url = item.final_url or item.normalized_url
    metadata = _metadata_response(extracted)
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
        # to_dict is a declared dict[str, JsonValue] producer; this cast names
        # that proven boundary — it asserts nothing new.
        diagnostics=cast(dict[str, JsonValue], _result_to_dict(item).get("diagnostics", {})),
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


def _json_str(value: JsonValue | None) -> str | None:
    """A JSON value narrowed to the string the wire field declares; else None."""
    return value if isinstance(value, str) else None


def _render_diagnostics(metadata: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """What the browser worker's ``metadata`` becomes on the render wire.
    Every key survives: the two carried in a declared field are read out, the rest
    pass through unchanged, so a new worker key lands in diagnostics by default.
    """
    diagnostics: dict[str, JsonValue] = {
        key: value
        for key, value in metadata.items()
        if key not in _RENDER_METADATA_AS_MODEL_FIELD | _RENDER_METADATA_AS_TOP_LEVEL
    }
    diagnostics["browser_profile"] = diagnostics.get("browser_profile") or {}
    return diagnostics


def render_result_to_unified(item: CrawlResult) -> UnifiedCrawlResult:
    """Adapt a browser ``CrawlResult`` into the unified result model.
    Returns the model, not a dict: every field has a default, so a dict could
    omit one invisibly (which is how an empty metadata shipped); the constructor names the field set.
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
        metadata=PageMetadataResponse(title=_json_str(metadata.get("title")), source_url=item.url.raw),
        html=item.html,
        text=item.text,
        quality_score=1.0 if item.status == "success" else 0.0,
        extraction_method=_json_str(metadata.get("extraction_method")),
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
