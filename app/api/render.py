"""Browser-render request orchestration (the ``/v1/render`` path).

Extracted from the previous ``app.service`` shim so the canonical entry point
for browser-render orchestration lives in ``app.api``. ``app.service`` is
deleted; existing callers import from ``app.api.render``.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from app.admission.payload_helpers import checked_payload_urls
from app.admission.render_options import resolve_render_options
from app.config.settings import CrawlerSettings
from app.models.crawl_job import CrawlJob
from app.models.crawl_policy import POLICY_SCHEMA_VERSION
from app.models.crawl_result import CrawlResult, EngineCrawlResult
from app.models.crawl_worker import CrawlWorker
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.models.render_options import RenderOptions
from app.models.result_record import RenderEnvelope
from app.orchestration.classification import classify_error_code
from app.orchestration.engine_result import failed_result_for
from app.orchestration.render_result import (
    crawl_result_from_engine_result,
    terminal_crawl_result,
)
from app.orchestration.scheduler import DomainScheduler
from app.orchestration.worker import call_worker
from app.storage.ids import new_request_id


def _render_options(payload: dict[str, Any], settings: CrawlerSettings) -> RenderOptions:
    options = payload.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("invalid_options")
    # The /v1/render caller owns every capture knob, browser_profile included.
    return resolve_render_options(
        options,
        {
            "timeout_seconds": settings.default_timeout_seconds,
            "max_retries": settings.default_max_retries,
            "respect_robots": settings.respect_robots,
        },
    )


def _failed_render_result(job: CrawlJob, exc: BaseException, *, submitted_url: str) -> CrawlResult:
    """Render-pipeline failure envelope for the domain scheduler.

    ``DomainScheduler.run`` is generic over the result shape a pipeline uses, so
    it asks the pipeline how to represent a job it never ran (an open circuit
    breaker) or whose worker raised. The crawl pipeline answers with an
    ``EngineCrawlResult``; this one answers with the ``CrawlResult`` the
    ``/v1/render`` contract is built on, adapting through the same converter
    ``_collect`` uses so classification and redaction stay centralized.
    """
    return crawl_result_from_engine_result(
        failed_result_for(job, exc, url_of=lambda _job: submitted_url),
        url=job.url,
    )


async def render_request(
    payload: dict[str, Any],
    *,
    settings: CrawlerSettings | None = None,
    worker: CrawlWorker,
) -> RenderEnvelope:
    """Run a /v1/render-shaped request through the per-domain scheduler."""
    active_settings = settings or CrawlerSettings.from_mapping()
    host_policy = HostPolicy.from_settings(active_settings)
    # One shared batch cap (a hand-rolled copy used to drift from it); submitted
    # texts stay beside admitted values so the wire's original_url echoes text.
    submitted = list(
        checked_payload_urls(
            payload,
            active_settings.max_batch_size,
            host_policy=host_policy,
        )
    )
    urls = [NormalizedUrl(u, policy=host_policy) for u in submitted]

    options = _render_options(payload, active_settings)
    started = time.monotonic()
    jobs: list[CrawlJob] = []

    for index, url in enumerate(urls):
        normalized = url
        jobs.append(
            CrawlJob(
                index=index,
                url=normalized,
                options=options,
                host_policy=host_policy,
            )
        )

    scheduler = DomainScheduler(
        global_limit=active_settings.global_concurrency,
        per_domain_limit=active_settings.per_domain_concurrency,
        delay_seconds=active_settings.per_domain_delay_ms / 1000,
    )
    # Results carry the submitting job's index, so duplicates keep their own
    # slots; per-index capture maps pool exhaustion to a 503, not a 500.
    from app.transport.browser_pool import BrowserPoolExhaustedError

    by_index: dict[int, CrawlResult] = {}
    worker_errors: dict[int, BaseException] = {}

    async def _collect(index: int, result: EngineCrawlResult | CrawlResult) -> None:
        # The scheduler's worker-exception envelope is an EngineCrawlResult with no
        # ``url``; normalize here so ``by_index`` holds only contracted results.
        if not isinstance(result, CrawlResult):
            result = crawl_result_from_engine_result(result, url=urls[index])
        by_index[index] = result

    async def _call_and_capture(job: CrawlJob) -> CrawlResult:
        # A CrawlWorker for the /v1/render contract, never the engine scheduler;
        # the returned shape is the honest one -- conversion happens in ``_collect``.
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
            failed_result=lambda job, exc: _failed_render_result(
                job, exc, submitted_url=submitted[job.index]
            ),
        )

    # Pool exhaustion is a capacity condition, not a per-URL failure: the worker
    # returns a classified terminal result and the endpoint maps it to the 503.
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
            by_index[index] = terminal_crawl_result(
                url=urls[index],
                status="failed",
                classification="site_error",
                attempts=0,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                domain="",
                # Never raw exception text: str(exc) would leak DNS names, paths
                # and secrets into the body.
                error=classify_error_code(worker_error) if worker_error is not None else "unknown_error",
            )

    ordered_results = [
        # Every result is stamped with the caller's submitted text as
        # ``original_url`` (the wire's "before normalization" field), right here.
        replace(by_index[index], original_url=submitted[index])
        for index in range(len(urls))
    ]
    response: RenderEnvelope = {
        "request_id": new_request_id(),
        "submitted_count": len(urls),
        # Same envelope key as the engine crawl envelope, so one adapter reads
        # both producers; a per-endpoint substitution would be a second source.
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        # The worker's own objects, never a serialization: no dict crosses a disk,
        # and the record's key set stays declared once, by the class it is.
        "results": list(ordered_results),
    }
    return response


__all__ = ["_render_options", "render_request"]
