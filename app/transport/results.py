"""One result-construction surface for both the engine and render funnels.
The two wire models (``EngineCrawlResult`` / ``CrawlResult``) stay one-home and
coexist here -- distinct named constructors over one classification chokepoint.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar, cast

from app.models.crawl_diagnostics import CrawlDiagnostics
from app.models.crawl_result import CrawlResult, EngineCrawlResult
from app.models.crawl_strategy_attempt import CrawlStrategyAttempt
from app.models.json_value import JsonValue
from app.models.normalized_url import NormalizedUrl
from app.models.result_factory import (
    build_engine_result,
    terminal_attempt,
    terminal_crawl_result,
)
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from app.transport.classification import classify_and_log
from app.vocabulary.taxonomy import ResultStatus

_JobT = TypeVar("_JobT")


__all__ = [
    "TerminalResult",
    "engine_result_from_exception",
    "failed_engine_result",
    "failed_result_for",
    "skipped_engine_result",
    "terminal_engine_result",
    "crawl_result_from_engine_result",
    "crawl_result_from_exception",
]


def engine_result_from_exception(
    *,
    error: BaseException,
    original_url: str,
    normalized_url: NormalizedUrl,
    strategy: CrawlStrategyName | None,
    elapsed_ms: int | None = None,
    extra_diagnostics: CrawlDiagnostics | None = None,
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
    diagnostics: CrawlDiagnostics = {"retryable": retryable, "error_code": classification}
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
    diagnostics: CrawlDiagnostics | None = None
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
        diagnostics=result.diagnostics,
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


def crawl_result_from_engine_result(engine: EngineCrawlResult, *, url: NormalizedUrl) -> CrawlResult:
    """Adapt an ``EngineCrawlResult`` into the render pipeline's ``CrawlResult``
    at its single receive point. Classification, error, and diagnostics survive
    (redaction-safe); the caller supplies the URL, named differently there.
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
        metadata=cast(dict[str, JsonValue], dict(engine.diagnostics)) or {},
    )


def crawl_result_from_exception(
    *,
    error: BaseException,
    url: NormalizedUrl,
    domain: str,
    strategy: CrawlStrategyName | None,
    elapsed_ms: int,
) -> CrawlResult:
    """Convert a render-path exception into a classified terminal ``CrawlResult``:
    no catch site can handle it, so the worker returns the contracted shape,
    classified through the same chokepoint, with ``retryable`` in ``metadata``.
    """
    classification, retryable = classify_and_log(
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
