"""Unified response shaping: engine results and render output to unified dicts.

The pure-data mapping lives in :mod:`crawlutron.service.unified_shaping`; this module
keeps the response envelope builders around them."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl, admit_or_none

from crawlutron.admission.admission_links import LinkCandidate
from crawlutron.delta import DeltaContext, delta_summary_for, removed_entry_keys
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.crawl_status import CrawlStatus
from crawlutron.models.result_record import AnyResult, ProducerEnvelope, RenderEnvelope
from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
from crawlutron.models.unified_crawl_result import UnifiedCrawlResult
from crawlutron.service.unified_shaping import (
    build_unified_result,
    render_result_to_unified,
)
from crawlutron.vocabulary.taxonomy import CrawlClassification

_logger = logging.getLogger(__name__)


def frontier_link_url(
    link: LinkCandidate,
    *,
    policy: HostPolicy,
    resolve_against: NormalizedUrl | None = None,
) -> NormalizedUrl | None:
    """The only link field eligible for frontier/visited use: the validated URL.
    A raw href (/rel, #frag, scheme-relative) is not an absolute URL, so a link
    with no usable normalized_url yields None -- never a raw-href fallback.
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
    One record instead of the twin six-line builder signatures (a contract edited
    twice); fields stay named and explicit, since a dropped field is silently absent.
    """

    request_id: str = ""
    policy_schema_version: str = ""
    success: bool = True
    submitted_count: int | None = None
    delta_context: DeltaContext | None = None


def _removed_urls(shaped: list[UnifiedCrawlResult], delta_context: DeltaContext | None) -> tuple[str, ...]:
    """Baseline entries this run never answered: the removed set.
    An entry is served when ANY of its identities (normalized or original)
    answered, so an alias spelling cannot report an answered page removed.
    """
    if delta_context is None:
        return ()
    served: set[str] = set()
    for result in shaped:
        if result.normalized_url is not None:
            served.add(str(result.normalized_url))
        if result.original_url:
            served.add(result.original_url)
    return removed_entry_keys(delta_context.entries, served)


def _response_envelope(
    shaped: list[UnifiedCrawlResult],
    result_count: int,
    options: EnvelopeOptions,
) -> UnifiedCrawlResponse:
    """The one construction of the response envelope: shaped results + every scalar.
    Every UnifiedCrawlResponse field is required, so a new one is passed here;
    result_count is the RAW list length, so a dropped item cannot hide in it.
    """
    return UnifiedCrawlResponse(
        results=shaped,
        request_id=options.request_id,
        policy_schema_version=options.policy_schema_version,
        success=options.success,
        submitted_count=(result_count if options.submitted_count is None else options.submitted_count),
        delta_summary=delta_summary_for(
            shaped,
            removed_urls=_removed_urls(shaped, options.delta_context),
            delta_requested=options.delta_context is not None,
            degraded_baseline=bool(options.delta_context and options.delta_context.baseline_expired),
        ),
    )


def _adapt_with_delta(
    adapt: Callable[..., UnifiedCrawlResult], delta_context: DeltaContext | None
) -> Callable[[object], UnifiedCrawlResult]:
    """Wrap ``adapt`` so every per-item shape threads ``delta_context`` into the
    result. When no delta knob is set, ``delta_context`` is ``None`` and the
    shaper behaves exactly as before (no classification, no wire change)."""

    def _adapted(item: object) -> UnifiedCrawlResult:
        return adapt(item, delta_context=delta_context)

    return _adapted


def build_unified_crawl_response(
    results: list[EngineCrawlResult],
    *,
    options: EnvelopeOptions | None = None,
    adapt: Callable[[EngineCrawlResult], UnifiedCrawlResult] = build_unified_result,
    delta_context: DeltaContext | None = None,
) -> UnifiedCrawlResponse:
    """Build the unified crawl response from a list of engine-result dicts.
    The async twin is the same contract with shaping off the event loop; both
    hand their options to :func:`_response_envelope`, so the response is decided in one place.
    """
    options = options or EnvelopeOptions()
    if delta_context is not None:
        options = replace(options, delta_context=delta_context)
    return _response_envelope(
        _shape_all(results, adapt=_adapt_with_delta(adapt, delta_context)), len(results), options
    )


def _shape_all[ResultT: AnyResult](
    results: Sequence[ResultT],
    *,
    adapt: Callable[[ResultT], UnifiedCrawlResult],
) -> list[UnifiedCrawlResult]:
    """Shape a whole batch in one pass (the worker-thread side), so one malformed
    item degrades to a terminal failure for that item, not the batch.
    ``adapt`` is the required per-item transform, run inside the guard (the caller's shape can't be inferred).
    """
    shaped: list[UnifiedCrawlResult] = []
    for item in results:
        try:
            shaped.append(adapt(item))
        except Exception:
            # Log loudly: a bare except used to swallow this, degrade the item to
            # site_error, and leave no trace of which shape failed or why.
            _logger.warning(
                "response shaping failed for one item; degrading it to a terminal failure",
                exc_info=True,
            )
            shaped.append(failed_unified_result(item))
    return shaped


def _peek_failed_fields(item: object) -> dict[str, NormalizedUrl | str | None]:
    """Best-effort field reads off whatever arrived at the batch's last guard.
    Reads by ``getattr`` with a default -- the items are dataclasses now -- never
    raising out of the guard, so the parameter stays ``object``, not a pretend type.
    """
    if isinstance(item, NormalizedUrl):
        # A page the batch never answered, held by the runner as an admitted
        # value -- passed as the value, not its text, because this reads attributes.
        return {"normalized_url": item}
    if isinstance(item, str):
        # Text-only means no admitted identity exists: the text is all that is
        # known, and guessing admission here is the hole the value class closes.
        return {"original_url": item}
    try:
        return {
            key: getattr(item, key, None)
            for key in ("normalized_url", "original_url", "url", "final_url", "error")
        }
    except Exception:
        _logger.debug("unshapable item refused even best-effort field reads", exc_info=True)
        return {}


def failed_unified_result(item: object) -> UnifiedCrawlResult:
    """A shape failure becomes a terminal failure for that item, not the batch,
    mirroring the job runner's ``failed += 1`` on a dropped item. Fields that
    need no interpretation are carried through, so the caller still sees which URL failed.
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
        # site_error is a FAILED_CLASSIFICATIONS member, so its status entry is
        # "failed"; mapping it as unknown_error ("blocked") would disagree.
        classification=CrawlClassification.site_error,
        error=error if isinstance(error := peeked.get("error"), str) and error else "result_shaping_failed",
    )


async def abuild_unified_crawl_response[ResultT: AnyResult](
    results: Sequence[ResultT],
    *,
    options: EnvelopeOptions | None = None,
    adapt: Callable[[ResultT], UnifiedCrawlResult],
    delta_context: DeltaContext | None = None,
) -> UnifiedCrawlResponse:
    """Async :func:`build_unified_crawl_response` for use inside async endpoints.
    Shapes the whole batch in ONE ``to_thread`` offload (per-item fan-out pays N
    Future + dispatch overheads), matching the sync contract; ``adapt`` keeps the per-item guard.
    """
    options = options or EnvelopeOptions()
    if delta_context is not None:
        options = replace(options, delta_context=delta_context)
    shaped = (
        await asyncio.to_thread(_shape_all, results, adapt=_adapt_with_delta(adapt, delta_context))
        if results
        else []
    )
    return _response_envelope(shaped, len(results), options)


def _envelope_options(result: ProducerEnvelope | RenderEnvelope) -> EnvelopeOptions:
    """The three scalar keys, read once for both endpoints.
    This is why the two adapters below share one pair of reads, not two
    hand-written lists: a field added here reaches both endpoints, and a dropped one is silent.
    """
    return EnvelopeOptions(
        request_id=result["request_id"],
        submitted_count=result["submitted_count"],
        policy_schema_version=result["policy_schema_version"],
    )


async def aunified_response_from_result(
    result: ProducerEnvelope,
    *,
    delta_context: DeltaContext | None = None,
) -> UnifiedCrawlResponse:
    """The one adapter from a crawl producer's envelope to the unified response.
    Both wire endpoints read the envelope here, so an envelope field is passed once
    and reaches both (a dropped field is silent). Paired with :func:`arender_response_from_result`.
    """
    return await abuild_unified_crawl_response(
        result["results"],
        options=_envelope_options(result),
        adapt=build_unified_result,
        delta_context=delta_context,
    )


async def arender_response_from_result(
    result: RenderEnvelope,
    *,
    delta_context: DeltaContext | None = None,
) -> UnifiedCrawlResponse:
    """:func:`aunified_response_from_result` for the render producer's envelope.
    Same one-adapter-per-shape decision and shared options builder; the per-item
    guard in ``_shape_all`` still degrades one unshapable result to a terminal failure.
    """
    return await abuild_unified_crawl_response(
        result["results"],
        options=_envelope_options(result),
        adapt=render_result_to_unified,
        delta_context=delta_context,
    )


__all__ = [
    "EnvelopeOptions",
    "abuild_unified_crawl_response",
    "failed_unified_result",
    "arender_response_from_result",
    "aunified_response_from_result",
    "build_unified_crawl_response",
    "frontier_link_url",
]
