"""The engine-side funnels: an attempt becomes a reportable record.

Every engine result is built here, so the record's construction is one chokepoint:
the three outcome axes come from the attempt, which keeps a flat wire field and
the attempt's own copy of the same value from disagreeing. Exceptions enter
through ``engine_result_from_exception`` -- the same door whether the failure
was a strategy raising, the scheduler's worker envelope, or the per-URL budget.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from app.models.crawl_result import CrawlStrategyAttempt, EngineCrawlResult
from app.models.normalized_url import NormalizedUrl
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from app.orchestration.classification import classify_and_log
from app.vocabulary.taxonomy import ResultStatus

_JobT = TypeVar("_JobT")


__all__ = [
    "TerminalResult",
    "binary_no_text_result",
    "build_engine_result",
    "engine_result_from_exception",
    "failed_result_for",
]


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
    classification, retryable = classify_and_log(
        error,
        event="strategy_result_error",
        strategy=strategy,
        known_classification=known_classification,
    )
    elapsed = elapsed_ms if elapsed_ms is not None else 0
    # A capacity skip is not a dead page: the URL was never attempted, so the
    # frontier can re-queue rather than record a permanent failure.
    diagnostics: dict = {"retryable": retryable, "error_code": classification}
    # A domain circuit opening is scheduler capacity, not a page failure:
    # dashboards key off it to tell "unreachable" from "not tried".
    if classification == "circuit_breaker_open":
        diagnostics["circuit_breaker"] = True
    # Callers may merge diagnostics that are ALREADY redacted; the primitive
    # owns classification and redaction and must not drop them silently.
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


def build_engine_result(
    *,
    original_url: str,
    normalized_url: NormalizedUrl,
    final_url: NormalizedUrl | None,
    attempt: CrawlStrategyAttempt,
    http_status: int | None,
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

    The outcome axes -- status, classification and error -- are DERIVED from
    the attempt rather than accepted as parameters: the record re-declares
    them as flat wire fields, and a funnel that also took them by keyword
    held a second spelling of the same three values that nothing compared.
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
        status=attempt.status,
        classification=attempt.classification,
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
        error=attempt.error,
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
        attempt=attempt,
        http_status=result.http_status,
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
        attempt=attempt,
        http_status=http_status,
        headers=headers,
        content_type=content_type,
        extraction_method="binary_no_text",
        diagnostics={},
    )
