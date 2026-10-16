"""The engine-side funnels: an attempt becomes a reportable record.
Every engine result is built here (one construction chokepoint), so the three
outcome axes stay the wire fields the attempt declared; exceptions enter via engine_result_from_exception.
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
    "failed_engine_result",
    "failed_result_for",
    "skipped_engine_result",
    "terminal_engine_result",
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
    """Convert ANY strategy exception into a classified terminal result -- the one
    chokepoint "a strategy raised". Raw exception text is logged server-side only
    and never reaches the wire; ``known_classification`` pins the budget-exceeded case.
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
    return failed_engine_result(
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
    ``CrawlJob`` holds it as ``url``, ``CrawlTask`` as ``normalized_url``: one thing,
    two names, so the two read sites name this once.
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
    The envelope comes from the pipeline (crawl routes here; render its own), so
    a skipped job still reports; the scheduler's single raise reaches the same exception chokepoint.
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
    Status, classification and error are DERIVED from the attempt, not accepted
    as parameters -- a funnel that took them too held a second spelling nothing checked.
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
    Four constructors used to spell this list as nine keyword parameters, so a
    field addition widened four signatures; now the list exists once.
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


def terminal_engine_result(result: TerminalResult, *, status: ResultStatus) -> EngineCrawlResult:
    """Construct a terminal (no payload) EngineCrawlResult with one attempt.
    The three terminal outcomes (failed/blocked/skipped) differ only in status
    and error defaults; the thin wrappers pin those per-status defaults.
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


def failed_engine_result(result: TerminalResult) -> EngineCrawlResult:
    """Construct a failed EngineCrawlResult with a single attempt.
    One of two per-status names over terminal_engine_result; ``functools.partial``
    was tried and is worse -- it merges keywords, so a caller could mis-ask for a skipped result.
    """
    return terminal_engine_result(result, status="failed")


def skipped_engine_result(result: TerminalResult) -> EngineCrawlResult:
    """Construct a skipped EngineCrawlResult (e.g., robots_disallowed).

    The sibling of :func:`failed_engine_result`; see that one for why these
    two are two functions rather than one bound status.
    """
    # A skipped page never carries a final URL or an HTTP status, and the
    # shared value defaults both to None, so the forward is total.
    return terminal_engine_result(result, status="skipped")


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
