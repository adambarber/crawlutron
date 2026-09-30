"""Browser-render request orchestration (the ``/v1/render`` path).

Extracted from the previous ``app.service`` shim so the canonical entry point
for browser-render orchestration lives in ``app.api``. ``app.service`` is
deleted; existing callers import from ``app.api.render``.
"""

from __future__ import annotations

import time
from typing import Any

from app.api_models import POLICY_SCHEMA_VERSION
from app.crawl.ids import new_request_id
from app.crawl.payload_helpers import checked_payload_urls
from app.crawl.render_options import resolve_render_options
from app.crawl.result_helpers import (
    _classify_error_code,
    _crawl_result_from_engine_result,
    _failed_result_for,
    _terminal_crawl_result,
)
from app.crawl.worker import call_worker
from app.models import (
    CrawlJob,
    CrawlResult,
    CrawlWorker,
    EngineCrawlResult,
    RenderOptions,
    to_dict,
)
from app.scheduler import DomainScheduler
from app.settings import CrawlerSettings
from app.validation import HostPolicy, normalize_public_http_url, registrable_domain


def _render_options(payload: dict[str, Any], settings: CrawlerSettings) -> RenderOptions:
    options = payload.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("invalid_options")
    # The /v1/render caller legitimately owns every capture knob, including
    # browser_profile (forwarded because it used to be dropped, which
    # made every worker knob unreachable from this path).
    return resolve_render_options(
        options,
        {
            "timeout_seconds": settings.default_timeout_seconds,
            "max_retries": settings.default_max_retries,
            "respect_robots": settings.respect_robots,
        },
    )


def _failed_render_result(job: CrawlJob, exc: BaseException) -> CrawlResult:
    """Render-pipeline failure envelope for the domain scheduler.

    ``DomainScheduler.run`` is generic over the result shape a pipeline uses, so
    it asks the pipeline how to represent a job it never ran (an open circuit
    breaker) or whose worker raised. The crawl pipeline answers with an
    ``EngineCrawlResult``; this one answers with the ``CrawlResult`` the
    ``/v1/render`` contract is built on, adapting through the same converter
    ``_collect`` uses so classification and redaction stay centralized.
    """
    return _crawl_result_from_engine_result(
        _failed_result_for(job, exc, url_of=lambda j: j.url),
        url=job.url,
    )


async def render_request(
    payload: dict[str, Any],
    *,
    settings: CrawlerSettings | None = None,
    worker: CrawlWorker,
) -> dict[str, Any]:
    """Run a /v1/render-shaped request through the per-domain scheduler."""
    active_settings = settings or CrawlerSettings.from_mapping()
    host_policy = HostPolicy.from_settings(active_settings)
    # The single shared batch gate (app.crawl.payload_helpers.checked_payload_urls):
    # this route used to hand-roll the same cap against max_batch_size with a
    # different failure shape, so the two gates could drift.
    urls = checked_payload_urls(
        payload,
        active_settings.max_batch_size,
        host_policy=host_policy,
    )

    options = _render_options(payload, active_settings)
    started = time.monotonic()
    jobs: list[CrawlJob] = []

    for index, url in enumerate(urls):
        normalized = normalize_public_http_url(url, policy=host_policy)
        domain = registrable_domain(normalized)
        jobs.append(
            CrawlJob(
                index=index,
                url=normalized,
                domain=domain,
                options=options,
                host_policy=host_policy,
            )
        )

    scheduler = DomainScheduler(
        global_limit=active_settings.global_concurrency,
        per_domain_limit=active_settings.per_domain_concurrency,
        delay_seconds=active_settings.per_domain_delay_ms / 1000,
    )
    # The scheduler hands each completed result to on_result together with the
    # submitting job's index, so duplicate URLs in one batch map to their own
    # slots. Matching by URL here would collapse duplicates onto index 0 and
    # KeyError on the missing slot, and is O(n^2).
    # Worker exceptions are swallowed by the scheduler's gather, so capture
    # them per index: total pool exhaustion raises for a 503, any other
    # missing slot becomes a per-URL failed result instead of a 500.
    from app.crawl.strategies.browser_pool import BrowserPoolExhaustedError

    by_index: dict[int, CrawlResult] = {}
    worker_errors: dict[int, BaseException] = {}

    async def _collect(index: int, result: EngineCrawlResult | CrawlResult) -> None:
        # The single point the render pipeline receives results. The scheduler's
        # worker-exception envelope is an EngineCrawlResult (built inside the
        # scheduler, which cannot know which pipeline it is serving): correct
        # for /v1/crawl, but that shape carries no ``url``, and
        # render_result_to_unified reads it -- so an unnormalized entry used to
        # reach the endpoint and die with a KeyError. Normalize here so
        # ``by_index`` holds only the contracted CrawlResult, whatever the
        # scheduler handed back.
        if not isinstance(result, CrawlResult):
            result = _crawl_result_from_engine_result(result, url=urls[index])
        by_index[index] = result

    async def _call_and_capture(job: CrawlJob) -> CrawlResult:
        # Declared ``-> CrawlResult`` on purpose: the render pipeline's worker is
        # a ``CrawlWorker`` (the /v1/render contract), never the engine's
        # scheduler worker. The scheduler drives this through the
        # ``SchedulerWorker`` alias, which returns ``EngineCrawlResult`` for the
        # crawl pipeline -- and ``_collect`` converts either shape at the single
        # boundary that consumes both, so this function stays honest about the
        # one shape it actually returns.
        try:
            return await call_worker(worker, job)
        except Exception as capture_error:
            worker_errors[job.index] = capture_error
            raise

    if jobs:
        await scheduler.run(
            jobs,
            _call_and_capture,
            on_result=_collect,
            failed_result=_failed_render_result,
        )

    # Pool exhaustion is a capacity condition, not a per-URL page failure.
    # The worker returns a classified terminal result instead of raising, so
    # the 503 signal is read off the results: an exhausted slot means renderer
    # capacity was unavailable, the request cannot be served at all, and the
    # caller retries -- reporting a per-URL page failure for a capacity problem
    # would be a lie the render path cannot take back (it has no frontier to
    # re-queue onto). The classification is chokepoint-sourced, so this reads
    # it and never re-derives it; the endpoint maps the error to the 503.
    if any(r.classification == "browser_pool_exhausted" for r in by_index.values()):
        raise BrowserPoolExhaustedError(
            # Static text only: the endpoint puts this message on the wire, so
            # it must carry no host/path/stack detail (the redaction contract).
            "browser pool exhausted: no renderer became available within the acquire bound"
        )

    missing = [index for index in range(len(urls)) if index not in by_index]
    if missing:
        if all(isinstance(worker_errors.get(index), BrowserPoolExhaustedError) for index in missing):
            raise worker_errors[missing[0]]
        for index in missing:
            worker_error = worker_errors.get(index)
            by_index[index] = _terminal_crawl_result(
                url=urls[index],
                status="failed",
                classification="site_error",
                attempts=0,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                domain="",
                # The wire-visible error must be a stable classification, never
                # raw exception text: to_dict is pure serialization, so str(exc)
                # would leak DNS names / paths / secrets straight into the body.
                error=_classify_error_code(worker_error) if worker_error is not None else "unknown_error",
            )

    ordered_results = [by_index[index] for index in range(len(urls))]
    return {
        "request_id": new_request_id(),
        "submitted_count": len(urls),
        # The producer envelope carries the same key the engine's crawl
        # envelope does, so response_shaping's single adapter reads one shape
        # from both producers. /v1/render used to substitute the constant at
        # the endpoint instead, which is a second place a version could be
        # updated and a silent one (an unpassed field is dropped by the model).
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "results": [to_dict(result) for result in ordered_results],
    }


__all__ = ["_render_options", "render_request"]
