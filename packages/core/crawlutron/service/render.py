"""Browser-render request orchestration: the /v1/render core lives in service.

service never imports api; the api layer keeps only the FastAPI door.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from crawlutron.admission.admission_links import gather_deadline_bounded
from crawlutron.admission.payload_helpers import checked_payload_urls
from crawlutron.admission.render_options import resolve_render_options
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_job import CrawlJob
from crawlutron.models.crawl_policy import POLICY_SCHEMA_VERSION
from crawlutron.models.crawl_result import CrawlResult
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.render_request import RenderRequest
from crawlutron.models.result_factory import terminal_crawl_result
from crawlutron.models.taxonomy import Classification
from crawlutron.runtime.worker_dispatch import call_worker
from crawlutron.storage.ids import new_request_id
from crawlutron.transport import robots as _robots_policy
from crawlutron.transport.classification import classify_error_code
from crawlutron.transport.fetchers import default_http_fetcher
from crawlutron.transport.results import (
    crawl_result_from_engine_result,
    failed_result_for,
)
from crawlutron.transport.robots import RobotsRules
from crawlutron.transport.scheduler import DomainScheduler
from crawlutron.vocabulary.robots import RobotsSource

#: The policy a render batch that does not honor robots is judged against: one
#: that forbids nothing, so a denial can never be reported from it.
_ALLOWED_POLICY = RobotsRules([], [], None, RobotsSource.policy)

if TYPE_CHECKING:
    from crawlutron.models.crawl_result import EngineCrawlResult
    from crawlutron.models.crawl_worker import CrawlWorker
    from crawlutron.models.json_value import JsonValue
    from crawlutron.models.normalized_url import NormalizedUrl
    from crawlutron.models.render_options import RenderOptions
    from crawlutron.models.result_record import RenderEnvelope


def render_options_for(payload: RenderRequest, settings: CrawlerSettings) -> RenderOptions:
    """The validated request model's knobs merged over the settings defaults;
    ``RenderRequest`` is the wire boundary, so this funnel never sees raw shape.
    """
    options: Mapping[str, JsonValue] = (
        payload.options.model_dump(exclude_none=True) if payload.options is not None else {}
    )
    # The /v1/render caller owns every capture knob, browser_profile included.
    return resolve_render_options(
        options,
        {
            "timeout_seconds": settings.default_timeout_seconds,
            "max_retries": settings.default_max_retries,
            "respect_robots": settings.respect_robots,
        },
    )


async def _robots_allow_set(
    urls: list[NormalizedUrl],
    options: RenderOptions,
    settings: CrawlerSettings,
    host_policy: HostPolicy,
) -> list[tuple[bool, RobotsRules]]:
    """One robots-admission pass over the render batch: True where the origin
    allows the target (or robots are not honored), and the loaded policy, so a
    refusal is not reported as a forbidding policy.
    """
    if not options.respect_robots:
        return [(True, _ALLOWED_POLICY)] * len(urls)
    # An unresolved origin fails closed as refused: the law is never to continue
    # past an unresolved denial, and refused names that posture to the caller.
    _DENYING = RobotsRules([(False, "/")], [], None, RobotsSource.refused)

    async def one(url: NormalizedUrl) -> tuple[bool, RobotsRules]:
        try:
            loaded = await _robots_policy.load_robots_rules(
                url,
                default_http_fetcher,
                options.timeout_seconds,
                policy=host_policy,
            )
        except Exception:
            # One origin's robots fetch must not abort the batch: fail that URL
            # closed and let the rest of the admission set complete.
            return False, _DENYING
        return (_robots_policy.robots_allows(url, loaded.rules), loaded)

    return list(
        await gather_deadline_bounded(
            urls,
            one,
            (False, _DENYING),
            timeout_seconds=float(options.timeout_seconds),
        )
    )


def _failed_render_result(job: CrawlJob, exc: BaseException, *, submitted_url: str) -> CrawlResult:
    """Render-pipeline failure envelope for the domain scheduler.

    ``DomainScheduler.run`` is generic over result shape; this answers ``CrawlResult``,
    the ``/v1/render`` contract, via the same converter ``_collect`` uses.
    """
    return crawl_result_from_engine_result(
        failed_result_for(job, exc, url_of=lambda _job: submitted_url),
        url=job.url,
    )


async def render_request(
    payload: RenderRequest,
    *,
    settings: CrawlerSettings | None = None,
    worker: CrawlWorker,
) -> RenderEnvelope:
    """Run one /v1/render request through the per-domain scheduler.

    ``payload`` is the request the router already validated and admitted; the
    door's type lives there, so this funnel never melts back to wire shape.
    """
    active_settings = settings or CrawlerSettings.from_mapping()
    host_policy = HostPolicy.from_settings(active_settings)
    submitted_pairs = _admitted_pairs(payload, active_settings, host_policy)
    submitted = [text for text, _ in submitted_pairs]
    urls = [normalized for _, normalized in submitted_pairs]
    options = render_options_for(payload, active_settings)
    started = time.monotonic()

    robots_allowed = await _robots_allow_set(urls, options, active_settings, host_policy)
    jobs = _admitted_jobs(urls, robots_allowed, options, host_policy)
    by_index = _terminal_robot_refusals(urls, robots_allowed, started)
    worker_errors: dict[int, BaseException] = {}

    await _run_render_batch(
        jobs,
        worker=worker,
        scheduler=render_scheduler(active_settings),
        urls=urls,
        submitted=submitted,
        by_index=by_index,
        worker_errors=worker_errors,
    )
    _fill_missing_results(by_index, worker_errors, urls, started)

    return _render_envelope(by_index, submitted, urls)


def _admitted_pairs(
    payload: RenderRequest,
    settings: CrawlerSettings,
    host_policy: HostPolicy,
) -> list[tuple[str, NormalizedUrl]]:
    """The batch cap shared with the engine, and the admitted pairs.

    Submitted texts stay beside admitted values so the wire's ``original_url``
    echoes text; the NormalizedUrl is reused, never rebuilt.
    """
    return checked_payload_urls(
        payload,
        settings.max_batch_size,
        host_policy=host_policy,
    )


def _admitted_jobs(
    urls: list[NormalizedUrl],
    robots_allowed: list[tuple[bool, RobotsRules]],
    options: RenderOptions,
    host_policy: HostPolicy,
) -> list[CrawlJob]:
    """Jobs for the robots-admitted slots; refusals never reach the worker."""
    return [
        CrawlJob(index=index, url=url, options=options, host_policy=host_policy)
        for index, url in enumerate(urls)
        if robots_allowed[index][0]
    ]


def _terminal_robot_refusals(
    urls: list[NormalizedUrl],
    robots_allowed: list[tuple[bool, RobotsRules]],
    started: float,
) -> dict[int, CrawlResult]:
    """Terminal robots_disallowed results for the slots admission skipped."""
    by_index: dict[int, CrawlResult] = {}
    for index, url in enumerate(urls):
        allowed, loaded = robots_allowed[index]
        if allowed:
            continue
        by_index[index] = terminal_crawl_result(
            url=url,
            status="skipped",
            classification=Classification.robots_disallowed,
            attempts=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            domain=url.origin,
            error=loaded.source.error_code,
            metadata={"robots": {"allowed": False, "evaluation": loaded.source.value}},
        )
    return by_index


def render_scheduler(settings: CrawlerSettings) -> DomainScheduler:
    """The render batch's per-domain scheduler, built through from_settings.

    Every knob reads its settings home (adaptive throttling honored, RPS the
    0.0 service posture -- render carries no policy carrier).
    """
    return DomainScheduler.from_settings(
        settings,
        global_limit=settings.global_concurrency,
        max_rps=0.0,
    )


async def _run_render_batch(
    jobs: list[CrawlJob],
    *,
    worker: CrawlWorker,
    scheduler: DomainScheduler,
    urls: list[NormalizedUrl],
    submitted: list[str],
    by_index: dict[int, CrawlResult],
    worker_errors: dict[int, BaseException],
) -> None:
    """Run the admitted jobs; results carry the submitting job's index so
    duplicates keep their own slots, and per-index capture maps pool exhaustion
    to a 503, not a 500."""

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


def _fill_missing_results(
    by_index: dict[int, CrawlResult],
    worker_errors: dict[int, BaseException],
    urls: list[NormalizedUrl],
    started: float,
) -> None:
    """Terminal site_error results for the slots no worker answered.

    Pool exhaustion is a capacity condition, not a per-URL failure: the 503 is
    the endpoint's mapping. Never raw exception text: str(exc) would leak.
    """
    from crawlutron.models.browser_pool_exhausted import BrowserPoolExhaustedError

    if any(r.classification == "browser_pool_exhausted" for r in by_index.values()):
        raise BrowserPoolExhaustedError(
            # Static text only: the endpoint puts this message on the wire, so
            # it must carry no host/path/stack detail (the redaction contract).
            "browser pool exhausted: no renderer became available within the acquire bound"
        )
    missing = [index for index in range(len(urls)) if index not in by_index]
    if missing and all(isinstance(worker_errors.get(index), BrowserPoolExhaustedError) for index in missing):
        raise worker_errors[missing[0]]
    for index in missing:
        worker_error = worker_errors.get(index)
        by_index[index] = terminal_crawl_result(
            url=urls[index],
            status="failed",
            classification=Classification.site_error,
            attempts=0,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            domain="",
            error=(
                classify_error_code(worker_error)
                if worker_error is not None
                else Classification.unknown_error
            ),
        )


def _render_envelope(
    by_index: dict[int, CrawlResult],
    submitted: list[str],
    urls: list[NormalizedUrl],
) -> RenderEnvelope:
    """The wire envelope, every result stamped with its caller's submitted text."""
    ordered_results = [replace(by_index[index], original_url=submitted[index]) for index in range(len(urls))]
    return {
        "request_id": new_request_id(),
        "submitted_count": len(urls),
        # Same envelope key as the engine crawl envelope, so one adapter reads
        # both producers; a per-endpoint substitution would be a second source.
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        # The worker's own objects, never a serialization: no dict crosses a disk,
        # and the record's key set stays declared once, by the class it is.
        "results": list(ordered_results),
    }


__all__ = ["render_options_for", "render_request", "render_scheduler"]
