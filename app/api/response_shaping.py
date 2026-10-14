"""Unified response shaping: engine results and render output to unified dicts.

The pure-data mapping lives in :mod:`app.api.unified_shaping`; this module
keeps the response envelope builders around them."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.api.unified_shaping import (
    build_unified_result,
    render_result_to_unified,
)
from app.models.crawl_result import EngineCrawlResult
from app.models.crawl_status import CrawlStatus
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.models.result_record import AnyResult, ProducerEnvelope, RenderEnvelope
from app.models.unified_crawl_response import UnifiedCrawlResponse
from app.models.unified_crawl_result import UnifiedCrawlResult
from app.vocabulary.taxonomy import CrawlClassification

_logger = logging.getLogger(__name__)


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
            # Log loudly: a bare except used to swallow this, degrade the item to
            # site_error, and leave no trace of which shape failed or why.
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
        # site_error is a FAILED_CLASSIFICATIONS member, so its status entry is
        # "failed"; mapping it as unknown_error ("blocked") would disagree.
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
    "arender_response_from_result",
    "aunified_response_from_result",
    "build_unified_crawl_response",
    "frontier_link_url",
]
