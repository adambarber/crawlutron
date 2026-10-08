from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx

from app.models.browser_pool_exhausted import BrowserPoolExhaustedError
from app.models.crawl_result import CrawlResult, CrawlStrategyAttempt
from app.models.engine_crawl_result import EngineCrawlResult
from app.models.normalized_url import NormalizedUrl
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from app.orchestration.scheduler_exceptions import _CircuitBreakerOpen
from app.transport.decompression import DecompressionError, DecompressionTooLargeError
from app.vocabulary.taxonomy import ResultStatus, is_transient_classification

_JobT = TypeVar("_JobT")


_logger = logging.getLogger(__name__)


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
    # The server declared an encoding and then did not deliver a body that
    # decodes under it. It subclasses ValueError only as an implementation
    # detail of zlib's error, so without this row the caller-error catch-all
    # below claimed it: a truncated response or an interfering proxy was
    # reported as "value_error" -- a terminal code reading as "the caller asked
    # for something invalid", which in a durable job marks the URL visited
    # instead of retrying a fault that a second attempt often clears.
    ((DecompressionError,), "http_error"),
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


def classify_error_code(exc: BaseException) -> Classification:
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


def _classify_and_log(
    error: BaseException,
    *,
    event: str,
    strategy: CrawlStrategyName | None,
    known_classification: Classification | None = None,
) -> tuple[Classification, bool]:
    """Classify one exception, log it once, and return ``(classification, retryable)``.

    The shared head of both failure primitives, because they are the same
    operation at two layers -- ``engine_result_from_exception`` builds an
    ``EngineCrawlResult``, ``crawl_result_from_exception`` a ``CrawlResult`` --
    and were each spelling out the log payload themselves. Split, the two
    ``extra`` dicts could drift: an operator grepping the failure stream would
    find ``error_detail`` on the engine path and not on the render path, and
    conclude one of them stops logging. The event name is the only thing that
    distinguishes them, and it is a parameter precisely because it is the only
    thing that should.

    ``retryable`` is the taxonomy's verdict, not a comparison against a label
    spelled out at the call site: a capacity skip is not a dead page, so the
    URL was never attempted and the frontier can re-queue instead of recording a
    permanent failure, and the render layer has to answer 503 for a transport
    blip too or a retrying client is handed a cached permanent failure. Reading
    it from ``is_transient_classification`` is what stops the two from
    disagreeing, which is also why a caller cannot pass one in.

    ``known_classification`` is the one case where the failure mode is not
    derived from the exception type at all (the per-URL wall-clock budget raises
    bare ``TimeoutError`` but classifies as ``url_budget_exceeded``).
    """
    classification = known_classification if known_classification is not None else classify_error_code(error)
    _logger.warning(
        event,
        extra={
            "strategy": strategy,
            "error_code": classification,
            "error_detail": str(error),
        },
    )
    return classification, is_transient_classification(classification)


def engine_result_from_exception(
    *,
    error: BaseException,
    original_url: str,
    normalized_url: NormalizedUrl,
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
    classification, retryable = _classify_and_log(
        error,
        event="strategy_result_error",
        strategy=strategy,
        known_classification=known_classification,
    )
    elapsed = elapsed_ms if elapsed_ms is not None else 0
    # A capacity skip is not a dead page: the URL was never attempted, so the
    # frontier can re-queue rather than record a permanent failure.
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
    return _failed_engine_result(
        TerminalResult(
            original_url=original_url,
            normalized_url=normalized_url,
            strategy=strategy,
            classification=classification,
            error=classification,
            diagnostics=diagnostics,
            http_status=None,
            attempt_elapsed_ms=elapsed,
        )
    )


def _job_url_value(job: object) -> NormalizedUrl | None:
    """The job's own URL as an admitted value, whichever record it was dispatched with.

    ``CrawlJob`` holds it as ``url``, ``CrawlTask`` as ``normalized_url``: same
    thing, two names. A reader reaching for one attribute gets the other
    record's shape, so the two read sites name this once.
    """
    for attribute in ("url", "normalized_url"):
        found = getattr(job, attribute, None)
        if isinstance(found, NormalizedUrl):
            return found
    return None


def failed_result_for[JobT](
    job: JobT,
    error: BaseException,
    *,
    url_of: Callable[[JobT], str],
) -> EngineCrawlResult:
    """Build the terminal ``EngineCrawlResult`` for a job the scheduler skipped.

    The domain scheduler used to construct this envelope inline, which silently
    assumed every pipeline it serves returns an ``EngineCrawlResult``. The
    render pipeline does not, so the envelope now comes from the pipeline:
    crawl pipelines route here, the render pipeline supplies its own builder.

    ``_CircuitBreakerOpen`` is the one exception the scheduler raises itself; it
    reaches the same ``engine_result_from_exception`` chokepoint so the
    ``network_error`` / ``circuit_breaker_open`` classification and the redaction
    contract stay in one place.
    """
    original_url = url_of(job)
    normalized = _job_url_value(job)
    if normalized is None:
        raise TypeError(f"{type(job).__name__} carries no NormalizedUrl")
    return engine_result_from_exception(
        error=error,
        original_url=original_url,
        normalized_url=normalized,
        strategy=None,
    )


def terminal_crawl_result(
    *,
    url: NormalizedUrl,
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
    (``classify_error_code``), never raw exception text -- which is what the
    browser path already passed correctly.

    ``selected_strategy`` is the browser mode that was in play, or None when no
    browser produced the result (a policy refusal, a pool that never handed out a
    renderer): the render shaper carries it to the wire instead of inventing
    a member.
    """
    return CrawlResult(
        url=url,
        attempt=terminal_attempt(
            strategy=selected_strategy,
            status=status,
            classification=classification,
            elapsed_ms=elapsed_ms,
            error=error,
        ),
        final_url=None,
        html=None,
        text=None,
        attempts=attempts,
        domain=domain,
        metadata=metadata or {},
    )


def terminal_attempt(
    *,
    strategy: CrawlStrategyName | None,
    status: ResultStatus,
    classification: Classification,
    elapsed_ms: int,
    error: CrawlError | None,
) -> CrawlStrategyAttempt:
    """The attempt a terminal result reports: no HTTP status, no body, no content type.

    Public because a result built outside this module needs the same attempt --
    the render endpoint's paced-task result and the browser strategy's own
    success path both have axes, timings and an error to place, and building one
    by hand at each site is how they would drift apart from the pair this
    primitive is checked against.
    """
    return CrawlStrategyAttempt(
        strategy=strategy,
        status=status,
        classification=classification,
        http_status=None,
        content_type=None,
        elapsed_ms=elapsed_ms,
        error=error,
    )


def crawl_result_from_engine_result(engine: EngineCrawlResult, *, url: NormalizedUrl) -> CrawlResult:
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
        # The engine record carries the submission text under the same name;
        # the render funnel restamps it from its own request texts.
        original_url=engine.original_url,
        attempt=terminal_attempt(
            strategy=engine.selected_strategy,
            status=engine.status,
            classification=engine.classification,
            elapsed_ms=elapsed_ms,
            error=engine.error,
        ),
        final_url=engine.final_url,
        html=engine.html,
        text=engine.text,
        attempts=len(engine.attempted_strategies),
        domain="",
        metadata=engine.diagnostics or {},
    )


def crawl_result_from_exception(
    *,
    error: BaseException,
    url: NormalizedUrl,
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
    through the same chokepoint as the engine path: ``classify_error_code``
    owns the taxonomy, this primitive owns the redaction (raw exception text is
    logged server-side only).

    ``strategy`` is the browser mode the worker was configured for, or None
    when no strategy was attempted; it rides the server-side log and the
    result metadata, never an invented enum member. ``retryable`` survives into
    ``metadata`` so the render layer can answer 503 for capacity exhaustion and
    the at-least-once frontier layer can re-queue the URL.
    """
    classification, retryable = _classify_and_log(
        error,
        event="render_result_error",
        strategy=strategy,
    )
    return terminal_crawl_result(
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


def build_engine_result(
    *,
    original_url: str,
    normalized_url: NormalizedUrl,
    final_url: NormalizedUrl | None,
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
    next_page_url: NormalizedUrl | None = None,
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


@dataclass(frozen=True)
class TerminalResult:
    """The per-attempt fields every terminal result carries, bound as one value.

    Four constructors each spelled this same list of keyword-only parameters.
    That is four signatures to widen when a field is added, and the duplicated
    text was exactly those signatures -- the duplication was never
    the logic, it was the argument list. Binding them into a value the
    constructors take positionally means the field list exists once, and a call
    site passes one object instead of nine keywords to say one thing.
    """

    original_url: str
    normalized_url: NormalizedUrl
    strategy: CrawlStrategyName | None
    classification: Classification
    error: CrawlError
    diagnostics: dict | None = None
    http_status: int | None = None
    final_url: NormalizedUrl | None = None
    attempt_elapsed_ms: int = 0


def _terminal_engine_result(result: TerminalResult, *, status: ResultStatus) -> EngineCrawlResult:
    """Construct a terminal (no payload) EngineCrawlResult with one attempt.

    The three terminal outcomes -- failed, blocked, skipped -- differ only in
    the status string and how the error defaults: a blocked result reports the
    classification itself as the reason and a skipped result never carries a
    final URL or status. The thin wrappers below pin those per-status defaults
    so no call site has to spell them out.
    """
    error = result.error or result.classification
    attempt = CrawlStrategyAttempt(
        strategy=result.strategy,
        status=status,
        classification=result.classification,
        http_status=result.http_status,
        content_type=None,
        elapsed_ms=result.attempt_elapsed_ms,
        error=error,
    )
    return build_engine_result(
        original_url=result.original_url,
        normalized_url=result.normalized_url,
        final_url=result.final_url,
        status=status,
        classification=result.classification,
        attempt=attempt,
        http_status=result.http_status,
        error=error,
        diagnostics=result.diagnostics or {},
    )


def _failed_engine_result(result: TerminalResult) -> EngineCrawlResult:
    """Construct a failed EngineCrawlResult with a single attempt.

    One of two per-status names over :func:`_terminal_engine_result`, and the
    duplication between them is irreducible in Python: ``status`` is
    keyword-only and must stay supplied, because ``CONTEXT_VARYING_STATUS``
    classifications legitimately need a status other than the table default, so
    the value cannot be derived here. Binding the status with
    ``functools.partial`` was tried and is worse, not better -- a partial
    merges keywords rather than owning them, so a caller could pass
    ``status="skipped"`` to this function and get a skipped result, and mypy
    does not close that hole. So each name is a real function.

    What makes it safe is not the shape but the guard underneath:
    :func:`status_agrees_with_classification` rejects any ``(classification,
    status)`` pair the taxonomy does not allow, at the point the attempt is
    built. That is what these names are for -- pinning a status and having it
    checked -- and it is why a third copy of this forwarding call, inlined in
    :func:`_classify_and_build`, was the one that actually needed removing.
    """
    return _terminal_engine_result(result, status="failed")


def _skipped_engine_result(result: TerminalResult) -> EngineCrawlResult:
    """Construct a skipped EngineCrawlResult (e.g., robots_disallowed).

    The sibling of :func:`_failed_engine_result`; see that one for why these
    two are two functions rather than one bound status.
    """
    # A skipped page never carries a final URL or an HTTP status, and the
    # shared value defaults both to None, so the forward is total.
    return _terminal_engine_result(result, status="skipped")


def binary_no_text_result(
    *,
    original_url: str,
    normalized_url: NormalizedUrl,
    final_url: NormalizedUrl | None,
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
    return build_engine_result(
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


__all__ = [
    "TerminalResult",
    "binary_no_text_result",
    "build_engine_result",
    "classify_error_code",
    "crawl_result_from_engine_result",
    "crawl_result_from_exception",
    "engine_result_from_exception",
    "failed_result_for",
    "terminal_attempt",
    "terminal_crawl_result",
]
