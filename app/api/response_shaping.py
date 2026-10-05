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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from app.constants import CrawlClassification
from app.crawl.llm_format import (
    PageMetadata as ExtractedPageMetadata,
)
from app.crawl.llm_format import (
    extract_carried_metadata,
)
from app.crawl.result_record import AnyResult, ProducerEnvelope, RenderEnvelope
from app.models.classification_evidence import ClassificationEvidence
from app.models.content_validators import ContentValidators
from app.models.crawl_result import CrawlResult, CrawlStrategyAttempt
from app.models.crawl_status import CrawlStatus
from app.models.crawl_strategy import CrawlStrategy
from app.models.discovered_link import DiscoveredLink
from app.models.discovered_link_response import DiscoveredLinkResponse as WireDiscoveredLink
from app.models.engine_crawl_result import EngineCrawlResult
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.models.page_metadata_response import PageMetadataResponse
from app.models.strategy_attempt import StrategyAttempt
from app.models.taxonomy import CrawlStrategyName
from app.models.unified_crawl_response import UnifiedCrawlResponse
from app.models.unified_crawl_result import UnifiedCrawlResult

_logger = logging.getLogger(__name__)

#: The batch shaper is generic over the result shape it is handed so that
#: ``adapt`` and ``results`` agree: a caller cannot pass browser results with a
#: engine adapter, which was expressible before and did nothing when it was.
_ResultT = TypeVar("_ResultT", bound=AnyResult)


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
            from app.crawl.llm_format import extract_llm_metadata

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
        # No fallback branch on any of these three: the internal and wire
        # taxonomies are the same set, asserted by
        # test_the_internal_and_wire_taxonomies_name_the_same_members, and the
        # engine result's own constructor already refused a value outside its
        # declared Literal. A conversion that cannot fail needs no story about
        # what it does when it does.
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
        # The render pipeline's funnel stamps every request-produced result
        # with the caller's submitted text, so the wire key means the same
        # thing here as it does at the crawl door. Results built outside a
        # request funnel keep None and fall back to the page's canonical text.
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
    resolve_against: NormalizedUrl | None = None,
) -> NormalizedUrl | None:
    """The only link field eligible for frontier/visited use: the validated URL.

    Extraction can legitimately emit a raw ``url`` href (``/rel``, ``#frag``,
    scheme-relative) alongside the policy-validated ``normalized_url``.
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
    if isinstance(normalized, NormalizedUrl):
        return normalized
    if not isinstance(normalized, str) or not normalized:
        return None
    if resolve_against is None:
        return admit_or_none(normalized, policy=policy)
    return resolve_against.resolve(normalized)


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
    results: list[EngineCrawlResult],
    *,
    options: EnvelopeOptions | None = None,
) -> UnifiedCrawlResponse:
    """Build the unified crawl response from a list of engine-result dicts.

    The async twin below is the same contract with the shaping off the event
    loop; both hand their options to :func:`_response_envelope`, so the response
    they build is decided in one place.
    """
    return _response_envelope(
        _shape_all(results, adapt=build_unified_result), len(results), options or EnvelopeOptions()
    )


def _shape_all[ResultT: AnyResult](
    results: Sequence[ResultT],
    *,
    adapt: Callable[[ResultT], UnifiedCrawlResult],
) -> list[UnifiedCrawlResult]:
    """Shape a whole batch in one pass (the worker-thread side of
    :func:`abuild_unified_crawl_response`).

    One malformed item must not fail the batch: the job-runner path guards
    per-item (a dropped item counts as a failed crawl, not silence), and the
    endpoint must hold the same contract. The guard lives here rather than
    at each caller so the two paths cannot drift apart again.

    ``adapt`` is the per-item transform a pipeline applies before the
    unified shaping (``/v1/render`` maps its browser-result dicts through
    :func:`render_result_to_unified`). It is REQUIRED rather than defaulting to
    the engine adapter: a default would pick a shape for the caller, and the
    caller's shape is the one thing here that cannot be inferred -- ``/v1/crawl``
    and ``/v1/render`` hold different records. It is typed as a transform INTO
    the model rather than into a dict-shaped one, so an adapter cannot return a
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
            shaped.append(adapt(item))
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


def _peek_failed_fields(item: Any) -> dict[str, Any]:
    """Best-effort field reads off whatever arrived at the batch's last guard.

    This is deliberately typed ``Any``: the guard above exists because the
    per-item path CAN raise, so whatever arrived is by definition not the
    record this module otherwise works in. Reading it is therefore best-effort
    by attribute with a default -- ``getattr`` rather than ``.get`` because
    the items are dataclasses now -- and whatever the item cannot answer
    degrades to the honest empty value instead of raising a second exception
    out of the guard and 500ing the whole batch.
    """
    if isinstance(item, NormalizedUrl):
        # A page the batch never answered, whose identity the runner still
        # holds as an admitted value. The value is passed as the identity --
        # not its text and not a two-key dict: this function reads attributes,
        # so a dict arrived here as an envelope with no URL at all, which is
        # the gap this branch exists to close.
        return {"normalized_url": item}
    if isinstance(item, str):
        # Text-only means no admitted identity exists: the submission text is
        # the whole of what is known. The value fields stay None -- admitting
        # the text here would need a policy the wire does not have, and a
        # guessed admission is exactly the hole the value class exists to close.
        return {"original_url": item}
    try:
        return {
            key: getattr(item, key, None)
            for key in ("normalized_url", "original_url", "url", "final_url", "error")
        }
    except Exception:
        _logger.debug("unshapable item refused even best-effort field reads", exc_info=True)
        return {}


def _failed_unified_result(item: Any) -> UnifiedCrawlResult:
    """A shape failure becomes a terminal failure for that item, not the batch.

    Mirrors the job runner's semantics (``failed += 1`` on a dropped item).
    Fields that require no interpretation are carried through so the caller
    still sees which URL failed.
    """
    peeked = _peek_failed_fields(item)

    def _value(key: str) -> NormalizedUrl | None:
        """Best-effort read of a value field: a value, or honestly nothing."""
        candidate = peeked.get(key)
        return candidate if isinstance(candidate, NormalizedUrl) else None

    def _text(*keys: str) -> str:
        """Best-effort read of a text field: text, a value's identity, or empty."""
        for key in keys:
            candidate = peeked.get(key)
            if isinstance(candidate, NormalizedUrl):
                return candidate.raw
            if isinstance(candidate, str) and candidate:
                return candidate
        return ""

    normalized = _value("normalized_url")
    return UnifiedCrawlResult(
        original_url=_text("original_url", "url") or (normalized.raw if normalized is not None else ""),
        normalized_url=normalized,
        final_url=_value("final_url"),
        status=CrawlStatus.failed,
        # Site_error is a FAILED_CLASSIFICATIONS member, so its status-table
        # entry is "failed" -- this keeps (status, classification) consistent
        # for any consumer that re-derives status from classification.
        # unknown_error maps to "blocked", which would disagree with the
        # hardcoded status above.
        classification=CrawlClassification.site_error,
        error=peeked.get("error") or "result_shaping_failed",
    )


async def abuild_unified_crawl_response[ResultT: AnyResult](
    results: Sequence[ResultT],
    *,
    options: EnvelopeOptions | None = None,
    adapt: Callable[[ResultT], UnifiedCrawlResult],
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


def _envelope_options(result: ProducerEnvelope | RenderEnvelope) -> EnvelopeOptions:
    """The three scalar keys, read once for both endpoints.

    This is the whole reason the two adapters below exist as one pair rather
    than two hand-written field lists: an envelope field added here reaches both
    endpoints, and ``UnifiedCrawlResponse`` drops an unpassed field rather than
    raising, which is how one shipped missing from a single endpoint.
    """
    return EnvelopeOptions(
        request_id=result["request_id"],
        submitted_count=result["submitted_count"],
        policy_schema_version=result["policy_schema_version"],
    )


async def aunified_response_from_result(result: ProducerEnvelope) -> UnifiedCrawlResponse:
    """The one adapter from a crawl producer's envelope to the unified response.

    Both wire endpoints read their envelope through this module, so an envelope
    field is passed once and cannot reach one path while being forgotten on the
    other. That mistake is silent by construction --
    ``UnifiedCrawlResponse`` drops an unpassed field rather than raising -- and
    a response field has already shipped missing from one endpoint and been
    caught only by curling the live endpoint.

    Paired with :func:`arender_response_from_result`, which is the same adapter
    for the other producer. Two functions rather than one overloaded on the
    envelope, because the envelope's ``results`` element type IS the adapter's
    input type: a single signature would have to accept either record and a
    matching callable, which is exactly the ``Any``-on-both-sides pairing the
    records exist to remove. Each body is its own call to
    :func:`abuild_unified_crawl_response` and they share
    :func:`_envelope_options`.
    """
    return await abuild_unified_crawl_response(
        result["results"],
        options=_envelope_options(result),
        adapt=build_unified_result,
    )


async def arender_response_from_result(result: RenderEnvelope) -> UnifiedCrawlResponse:
    """:func:`aunified_response_from_result` for the render producer's envelope.

    Same one-adapter-per-shape decision, same shared options builder, and the
    per-item guard in ``_shape_all`` still degrades one unshapable result to a
    terminal failure rather than 500ing the response.
    """
    return await abuild_unified_crawl_response(
        result["results"],
        options=_envelope_options(result),
        adapt=render_result_to_unified,
    )


__all__ = [
    "EnvelopeOptions",
    "abuild_unified_crawl_response",
    "abuild_unified_result",
    "aformat_crawl_item",
    "arender_response_from_result",
    "aunified_response_from_result",
    "build_unified_crawl_response",
    "build_unified_result",
    "format_crawl_item",
    "frontier_link_url",
    "render_result_to_unified",
]
