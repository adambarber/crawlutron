"""The render-side funnels: a worker's outcome becomes the /v1/render wire shape.

The render pipeline reports a ``CrawlResult`` -- what one browser strategy did to
one URL -- so its primitives hold an attempt rather than restating it. The
failure primitives mirror the engine side: an exception classifies through the
same chokepoint and the raw text never reaches the wire.
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
