"""The render-side funnels: a worker's outcome becomes the /v1/render wire
shape, holding a ``CrawlResult`` attempt rather than restating it. The failure
primitives mirror the engine side -- classify through the same chokepoint.
"""

from __future__ import annotations

from typing import Any

from app.models.crawl_result import CrawlResult, CrawlStrategyAttempt, EngineCrawlResult
from app.models.normalized_url import NormalizedUrl
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from app.orchestration.classification import classify_and_log
from app.vocabulary.taxonomy import ResultStatus

__all__ = [
    "crawl_result_from_engine_result",
    "crawl_result_from_exception",
    "terminal_attempt",
    "terminal_crawl_result",
]


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
    """Construct a terminal ``CrawlResult`` (no payload) -- the single site where
    ``error`` must be a stable ``classify_error_code``, never raw exception text
    that once reached the body. ``selected_strategy`` is the mode, or None.
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
    """The attempt a terminal result reports -- no HTTP status, body, or content
    type. Public because results built elsewhere need the same attempt, and
    building it by hand per site is how they would drift.
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
