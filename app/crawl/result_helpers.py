from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from collections.abc import Callable
from typing import Any, TypeVar

import httpx

from app.constants import is_transient_classification, status_agrees_with_classification
from app.crawl.decompression import DecompressionTooLargeError
from app.crawl.scheduler_exceptions import _CircuitBreakerOpen
from app.models import (
    BrowserPoolExhaustedError,
    Classification,
    CrawlError,
    CrawlResult,
    CrawlStrategyAttempt,
    CrawlStrategyName,
    EngineCrawlResult,
    ResultStatus,
)

_JobT = TypeVar("_JobT")


_logger = logging.getLogger(__name__)


def _enforce_status_classification(classification: Classification, status: ResultStatus) -> None:
    """Reject a ``(classification, status)`` pair the taxonomy does not allow.

    The terminal-result primitives take both axes as free parameters, which is
    exactly the shape that lets the wire assert one status while
    ``status_for_classification`` re-derives another for the same label. This
    check at the construction chokepoint makes the disagreeing pair
    unrepresentable instead of merely undesirable: it passes when the status is
    the table default for the classification or one of the documented
    context-dependent statuses, and raises otherwise, so the two axes can never
    silently drift apart again.
    """
    if not status_agrees_with_classification(classification, status):
        raise ValueError(
            f"status {status!r} disagrees with the taxonomy status for "
            f"classification {classification!r}"
        )


# The exception -> classification table, in precedence order: the first row
# whose types match wins. Written as data rather than an if-ladder because this
# function is the chokepoint every error in the crawl path passes through, and
# an if-ladder had outgrown the function-length ceiling -- a ladder that cannot
# hold its own contents is a ladder whose ordering has stopped being reviewable.
# Order carries the semantics, so it is the one thing to preserve here:
#   * ``DecompressionError`` (ours) subclasses ``ValueError``, so the
#     ``payload_too_large`` row has to precede ``value_error`` or an oversized
#     body would be reported as a caller error.
#   * ``httpx.TimeoutException`` and ``httpx.TransportError`` both subclass
#     ``httpx.HTTPError``, so the ``http_error`` catch-all has to come after the
#     ``connection_error`` row or every transport failure would flatten into it.
#   * ``socket.gaierror`` and ``ssl.SSLError`` are both ``OSError`` subclasses;
#     keeping their rows above the broad fallbacks is what stops a name that
#     would not resolve from being reported as a generic network error.
_CLASSIFICATION_BY_EXCEPTION: tuple[tuple[tuple[type[BaseException], ...], Classification], ...] = (
    ((BrowserPoolExhaustedError,), "browser_pool_exhausted"),
    # Capacity, not a page failure: the URL was never attempted, which is why it
    # stays distinct from the transport rows below and stays retryable.
    ((_CircuitBreakerOpen,), "circuit_breaker_open"),
    ((DecompressionTooLargeError,), "payload_too_large"),
    ((httpx.TooManyRedirects,), "too_many_redirects"),
    # Name resolution never got off the host. This row is why ``dns_error`` is a
    # live member: before it existed, a name that would not resolve was reported
    # as "connection_error", a category error that also left the declared
    # dns_error member with no producer anywhere in app/.
    ((socket.gaierror, socket.herror), "dns_error"),
    (
        (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.CloseError,
            httpx.RemoteProtocolError,
            httpx.ProtocolError,
            ConnectionError,
            ConnectionRefusedError,
            ConnectionResetError,
            TimeoutError,
            asyncio.TimeoutError,
        ),
        "connection_error",
    ),
    ((ssl.SSLError,), "tls_error"),
    ((httpx.HTTPStatusError,), "http_status_error"),
    ((httpx.HTTPError,), "http_error"),
    ((ValueError,), "value_error"),
)


def _classify_error_code(exc: BaseException) -> Classification:
    """Map an internal exception to a stable, non-leaking error code.

    Raw exception text can leak DNS hostnames, internal filesystem paths, or
    stack detail. Callers must never see ``str(exc)``; these codes are safe to
    surface in user-facing ``error``/``diagnostics`` fields.

    The classifier is strategy-neutral and lives here (not inside a strategy
    module) because both the HTTP and browser strategies classify through it:
    housing it in one strategy would make the other reach across module
    boundaries for a shared taxonomy decision.

    The mapping itself is the ordered table
    :data:`_CLASSIFICATION_BY_EXCEPTION`; the unrecognized case falls through to
    ``network_error``.

    Every row returns a :data:`Classification` taxonomy member; the return type
    states that so the callers that feed this straight into an
    ``EngineCrawlResult`` get a statically-enforced classification instead of an
    unproven string.
    """
    for types, classification in _CLASSIFICATION_BY_EXCEPTION:
        if isinstance(exc, types):
            return classification
    return "network_error"


def _engine_result_from_exception(
    *,
    error: BaseException,
    original_url: str,
    normalized_url: str,
    strategy: CrawlStrategyName | None,
    elapsed_ms: int | None = None,
    extra_diagnostics: dict | None = None,
    known_classification: Classification | None = None,
) -> EngineCrawlResult:
    """Convert ANY strategy exception into a classified terminal result.

    This is the single chokepoint for "a strategy raised": the HTTP strategy's
    broad-except sink, the browser strategy's outer guard, the engine's
    budget-timeout path, and the scheduler's worker-exception recovery all
    route through here, so an exception can never become an ad-hoc result
    shape (or vanish entirely) at one of them.

    Raw exception text is logged server-side only and never reaches the wire
    (it can leak DNS names, paths, or stack detail); the caller sees the stable
    code.

    ``strategy`` is the strategy this failure belongs to, or None when the
    failure preceded any attempt (the scheduler's worker envelope, the
    per-URL budget overrunning the whole chain): "no strategy was attempted"
    is not a strategy, and None is the value the strict wire enum accepts.

    ``known_classification`` is for the one case where the failure mode is not
    derived from the exception type at all -- the per-URL wall-clock budget,
    which raises bare ``TimeoutError`` and whose real classification is
    ``url_budget_exceeded``. It must be a taxonomy member.
    """
    classification = known_classification if known_classification is not None else _classify_error_code(error)
    elapsed = elapsed_ms if elapsed_ms is not None else 0
    _logger.warning(
        "strategy_result_error",
        extra={
            "strategy": strategy,
            "error_code": classification,
            "error_detail": str(error),
        },
    )
    # A capacity skip is not a dead page: the URL was never attempted, so the
    # frontier can re-queue rather than record a permanent failure. The verdict
    # is the taxonomy's, so it cannot disagree with the other three call sites'.
    retryable = is_transient_classification(classification)
    diagnostics: dict = {"retryable": retryable, "error_code": classification}
    # A domain circuit opening is a scheduler capacity decision, not a page
    # failure: callers and dashboards key off this marker to tell "the site is
    # unreachable" from "we chose not to try".
    if classification == "circuit_breaker_open":
        diagnostics["circuit_breaker"] = True
    # Callers may merge strategy-specific diagnostics that are ALREADY
    # redacted (e.g. the HTTP path's request_profile). The primitive owns
    # classification and raw-text redaction; it must not silently drop
    # diagnostics a caller built under the redaction contract.
    if extra_diagnostics:
        diagnostics.update(extra_diagnostics)
    return _terminal_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=strategy,
        status="failed",
        classification=classification,
        error=classification,
        http_status=None,
        diagnostics=diagnostics,
        attempt_elapsed_ms=elapsed,
    )


def _failed_result_for(
    job: _JobT,
    error: BaseException,
    *,
    url_of: Callable[[_JobT], str],
) -> EngineCrawlResult:
    """Build the terminal ``EngineCrawlResult`` for a job the scheduler skipped.

    The domain scheduler used to construct this envelope inline, which silently
    assumed every pipeline it serves returns an ``EngineCrawlResult``. The
    render pipeline does not, so the envelope now comes from the pipeline:
    crawl pipelines route here, the render pipeline supplies its own builder.

    ``_CircuitBreakerOpen`` is the one exception the scheduler raises itself; it
    reaches the same ``_engine_result_from_exception`` chokepoint so the
    ``network_error`` / ``circuit_breaker_open`` classification and the redaction
    contract stay in one place.
    """
    original_url = url_of(job)
    normalized = getattr(job, "normalized_url", None) or original_url
    return _engine_result_from_exception(
        error=error,
        original_url=original_url,
        normalized_url=normalized,
        strategy=None,
    )


def _terminal_crawl_result(
    *,
    url: str,
    status: ResultStatus,
    classification: Classification,
    attempts: int,
    elapsed_ms: int,
    domain: str,
    error: CrawlError | None,
    metadata: dict[str, Any] | None = None,
    selected_strategy: CrawlStrategyName | None = None,
) -> CrawlResult:
    """Construct a terminal (no-payload) CrawlResult -- the /v1/render wire shape.

    Three sites hand-built this same ``final_url=None, html=None, text=None``
    result: the browser strategy's failure path, the render endpoint's
    robots-disallowed path, and its worker-failure path. The render copy
    drifted: it set ``error=str(error)`` from a caught worker exception, and
    ``to_dict`` is pure serialization, so raw exception text (DNS names,
    filesystem paths, secrets) reached the response body. This primitive is
    the single construction site; ``error`` must be a stable classification
    (``_classify_error_code``), never raw exception text -- which is what the
    browser path already passed correctly.

    ``selected_strategy`` is the browser mode that was in play, or None when no
    browser produced the result (a policy refusal, a pool that never handed out
    a renderer): the render shaper carries it to the wire instead of inventing
    a member.
    """
    _enforce_status_classification(classification, status)
    return CrawlResult(
        url=url,
        status=status,
        classification=classification,
        final_url=None,
        html=None,
        text=None,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        domain=domain,
        error=error,
        metadata=metadata or {},
        selected_strategy=selected_strategy,
    )


def _crawl_result_from_engine_result(engine: EngineCrawlResult, *, url: str) -> CrawlResult:
    """Adapt an ``EngineCrawlResult`` into the render pipeline's ``CrawlResult``.

    :meth:`DomainScheduler.run` builds its worker-exception envelope as an
    ``EngineCrawlResult`` -- the right shape for ``/v1/crawl``, whose pipeline
    reads ``original_url``/``normalized_url``, and the wrong shape for
    ``/v1/render``, whose pipeline reads ``url``
    (:func:`render_result_to_unified`). The scheduler cannot know which pipeline
    it is serving, so the render pipeline normalizes at its single receive
    point (``_collect``) rather than making the scheduler polymorphic.

    Classification, error and diagnostics survive the adaptation (the
    classification was already derived through the shared classifier, so it is
    a taxonomy member and redaction-safe); the caller supplies the URL its slot
    was submitted with, since the engine shape carries it under a different
    name. "no payload" fields mirror the other terminal ``CrawlResult``
    primitives: this result represents a page that was never rendered.
    """
    elapsed_ms = int(engine.attempted_strategies[0].elapsed_ms) if engine.attempted_strategies else 0
    return CrawlResult(
        url=url,
        status=engine.status,
        classification=engine.classification,
        final_url=engine.final_url,
        html=engine.html,
        text=engine.text,
        attempts=len(engine.attempted_strategies),
        elapsed_ms=elapsed_ms,
        domain="",
        error=engine.error,
        metadata=engine.diagnostics or {},
    )


def _crawl_result_from_exception(
    *,
    error: BaseException,
    url: str,
    domain: str,
    strategy: CrawlStrategyName | None,
    elapsed_ms: int,
) -> CrawlResult:
    """Convert a render-path exception into a classified terminal CrawlResult.

    The render worker contract (``CrawlWorker -> CrawlResult``) has no catch
    site above it that can handle the failure: the scheduler's envelope builds
    an ``EngineCrawlResult``, which is the wrong shape for the render pipeline
    (``to_dict`` then ``render_result_to_unified`` reads ``item["url"]``, which
    ``EngineCrawlResult`` does not have), and the engine's browser consumer
    reads ``result.metadata`` / ``result.elapsed_ms``, which
    ``EngineCrawlResult`` equally lacks. A render worker that fails must
    therefore return the contracted ``CrawlResult`` shape itself, classified
    through the same chokepoint as the engine path: ``_classify_error_code``
    owns the taxonomy, this primitive owns the redaction (raw exception text is
    logged server-side only).

    ``strategy`` is the browser mode the worker was configured for, or None
    when no strategy was attempted; it rides the server-side log and the
    result metadata, never an invented enum member. ``retryable`` survives into
    ``metadata`` so the render layer can answer 503 for capacity exhaustion and
    the at-least-once frontier layer can re-queue the URL.
    """
    classification = _classify_error_code(error)
    _logger.warning(
        "render_result_error",
        extra={"strategy": strategy, "error_code": classification, "error_detail": str(error)},
    )
    # The taxonomy's verdict, not a comparison against the one label this line
    # originally spelled out: the render layer must answer 503 for a transport
    # blip too, or a retrying client is handed a cached permanent failure.
    retryable = is_transient_classification(classification)
    return _terminal_crawl_result(
        url=url,
        status="failed",
        classification=classification,
        attempts=1,
        elapsed_ms=elapsed_ms,
        domain=domain,
        error=classification,
        metadata={"retryable": retryable, "error_code": classification},
        selected_strategy=strategy,
    )


def _make_engine_result(
    *,
    original_url: str,
    normalized_url: str,
    final_url: str | None,
    status: ResultStatus,
    classification: Classification,
    attempt: CrawlStrategyAttempt,
    http_status: int | None,
    error: CrawlError | None,
    selected_strategy: CrawlStrategyName | None = None,
    headers: dict[str, str] | None = None,
    content_type: str | None = None,
    html: str | None = None,
    text: str | None = None,
    markdown: str | None = None,
    extraction_method: str | None = None,
    quality_score: float = 0.0,
    classification_evidence: list | None = None,
    diagnostics: dict | None = None,
    discovered_link_count: int = 0,
    discovered_links_truncated: bool = False,
    discovered_links: list | None = None,
    next_page_url: str | None = None,
    validators: dict[str, str] | None = None,
    llm_metadata: dict[str, Any] | None = None,
) -> EngineCrawlResult:
    """Construct an EngineCrawlResult from a pre-built attempt.

    Many parameters have defaults for the common "empty result" case.
    """
    if headers is None:
        headers = {}
    if classification_evidence is None:
        classification_evidence = []
    if diagnostics is None:
        diagnostics = {}
    if discovered_links is None:
        discovered_links = []
    _enforce_status_classification(classification, status)
    return EngineCrawlResult(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=final_url,
        status=status,
        classification=classification,
        selected_strategy=selected_strategy,
        attempted_strategies=[attempt],
        http_status=http_status,
        headers=headers,
        content_type=content_type,
        html=html,
        text=text,
        markdown=markdown,
        extraction_method=extraction_method,
        quality_score=quality_score,
        classification_evidence=classification_evidence,
        diagnostics=diagnostics,
        discovered_link_count=discovered_link_count,
        discovered_links_truncated=discovered_links_truncated,
        discovered_links=discovered_links,
        error=error,
        next_page_url=next_page_url,
        validators=validators or {},
        llm_metadata=llm_metadata,
    )


def _terminal_engine_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: CrawlStrategyName | None,
    status: ResultStatus,
    classification: Classification,
    error: CrawlError | None = None,
    diagnostics: dict | None = None,
    http_status: int | None = None,
    final_url: str | None = None,
    attempt_elapsed_ms: int = 0,
) -> EngineCrawlResult:
    """Construct a terminal (no payload) EngineCrawlResult with one attempt.

    The three terminal outcomes -- failed, blocked, skipped -- differ only in
    the status string and how the error defaults: a blocked result reports the
    classification itself as the reason and a skipped result never carries a
    final URL or status. The thin wrappers below pin those per-status defaults
    so no call site has to spell them out.
    """
    if error is None:
        error = classification
    attempt = CrawlStrategyAttempt(
        strategy=strategy,
        status=status,
        classification=classification,
        http_status=http_status,
        content_type=None,
        elapsed_ms=attempt_elapsed_ms,
        error=error,
    )
    return _make_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=final_url,
        status=status,
        classification=classification,
        attempt=attempt,
        http_status=http_status,
        error=error,
        diagnostics=diagnostics or {},
    )


def _failed_engine_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: CrawlStrategyName | None,
    classification: Classification,
    error: CrawlError,
    diagnostics: dict | None = None,
    http_status: int | None = None,
    final_url: str | None = None,
) -> EngineCrawlResult:
    """Construct a failed EngineCrawlResult with a single attempt."""
    return _terminal_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=strategy,
        status="failed",
        classification=classification,
        error=error,
        diagnostics=diagnostics,
        http_status=http_status,
        final_url=final_url,
    )


def _skipped_engine_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: CrawlStrategyName | None,
    classification: Classification,
    error: CrawlError,
    diagnostics: dict | None = None,
) -> EngineCrawlResult:
    """Construct a skipped EngineCrawlResult (e.g., robots_disallowed)."""
    return _terminal_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=strategy,
        status="skipped",
        classification=classification,
        error=error,
        diagnostics=diagnostics,
    )


def _binary_no_text_result(
    *,
    original_url: str,
    normalized_url: str,
    final_url: str | None,
    strategy: CrawlStrategyName | None,
    http_status: int | None,
    headers: dict[str, str],
    content_type: str | None,
    elapsed_ms: int,
) -> EngineCrawlResult:
    """Construct a binary/no-text EngineCrawlResult."""
    attempt = CrawlStrategyAttempt(
        strategy=strategy,
        status="failed",
        classification="binary_no_text",
        http_status=http_status,
        content_type=content_type,
        elapsed_ms=elapsed_ms,
        error="binary_no_text",
    )
    return _make_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=final_url,
        status="failed",
        classification="binary_no_text",
        attempt=attempt,
        http_status=http_status,
        error="binary_no_text",
        headers=headers,
        content_type=content_type,
        extraction_method="binary_no_text",
        diagnostics={},
    )


__all__ = []
