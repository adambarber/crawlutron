"""Background async-crawl runner for ``/v1/crawl`` webhook deliveries.

Queued when ``async_mode=True`` or a ``webhook_url`` is set: acquire the bounded
semaphore, run via :mod:`crawlutron.service.crawl_executor`, persist in both stores, POST to the webhook.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from crawlutron.config.env import get_env_int
from crawlutron.config.settings import CrawlerSettings
from crawlutron.delta import DeltaContext
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.runtime import process_state
from crawlutron.service.crawl_executor import execute_crawl
from crawlutron.service.effects import EffectBounds, EffectContainer
from crawlutron.service.job_runner import arecord_terminal_failure
from crawlutron.service.main_helpers import record_crawl_completion
from crawlutron.service.response_shaping import (
    EnvelopeOptions,
    abuild_unified_crawl_response,
)
from crawlutron.service.unified_shaping import build_unified_result
from crawlutron.service.webhooks import webhook_drain_deadline_seconds
from crawlutron.storage.job_store import (
    DEFAULT_JOB_CLAIM_SECONDS,
    CrawlJobState,
    JobStatus,
)
from crawlutron.storage.result_store import CrawlSpillStore
from crawlutron.transport.timeouts import Timeouts
from safe_url.normalized_url import NormalizedUrl

logger = logging.getLogger(__name__)


# Module-level holder: bounded so a flood of 202s cannot spawn unbounded
# concurrent crawls; loop-bound, rebuilt when the loop changes, width re-read from environ.
@dataclass(frozen=True)
class _SemaphoreHolder:
    """The webhook semaphore and the loop it was built for, as one value."""

    semaphore: asyncio.Semaphore
    loop: asyncio.AbstractEventLoop | None


_SEMAPHORE_HOLDER: _SemaphoreHolder | None = None


def _build_async_crawl_semaphore() -> asyncio.Semaphore:
    """Construct the webhook-crawl semaphore at the configured width.

    The width is read on every build (not cached at import) so a reset re-reads
    ``CRAWLUTRON_MAX_WEBHOOK_CONCURRENCY``; mirrors the pooled clients' late-bound ``build``.
    """
    return asyncio.Semaphore(get_env_int("CRAWLUTRON_MAX_WEBHOOK_CONCURRENCY", 4, lo=1))


def async_crawl_semaphore() -> asyncio.Semaphore:
    """Return the webhook-crawl semaphore bound to the running event loop.

    Lazily constructed (no event-loop dependency at import) and rebuilt whenever
    the running loop is not the one the holder's cached semaphore was built for.
    """
    global _SEMAPHORE_HOLDER
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    holder = _SEMAPHORE_HOLDER
    # Check-then-set is safe because asyncio is cooperative: one loop per thread
    # runs this to completion between awaits -- nothing can interleave.
    if holder is None or holder.loop is not loop:
        holder = _SemaphoreHolder(_build_async_crawl_semaphore(), loop)
        _SEMAPHORE_HOLDER = holder
    return holder.semaphore


def _reset() -> None:
    """Drop the webhook-crawl semaphore holder."""
    global _SEMAPHORE_HOLDER
    _SEMAPHORE_HOLDER = None


def _has() -> bool:
    """Whether a webhook-crawl semaphore holder is cached right now."""
    return _SEMAPHORE_HOLDER is not None


process_state.register(__name__, _reset, _has)


async def run_async_crawl(
    request_id: str,
    payload: CrawlRequest,
    webhook_url: WebhookTarget | None,
    crawl_settings: CrawlerSettings,
    job_store,
    result_store,
    shutdown_event: asyncio.Event,
    url_pairs: list[tuple[str, NormalizedUrl]] | None = None,
    spill_store: CrawlSpillStore | None = None,
    delta_context: DeltaContext | None = None,
) -> None:
    """Run a single-shot crawl in the background, persisting results to job_store
    through PENDING -> RUNNING -> COMPLETED/FAILED and POSTing when ``webhook_url``
    is set. ``url_pairs`` carries the gate's admitted batch, never rebuilt."""
    claim_token = f"async:{request_id}:{time.monotonic_ns()}"
    claim_seconds = DEFAULT_JOB_CLAIM_SECONDS
    container = EffectContainer()
    try:
        crawled = await _crawl_under_semaphore(
            request_id,
            payload,
            crawl_settings,
            job_store,
            result_store,
            shutdown_event,
            url_pairs,
            spill_store,
            delta_context,
            claim_token,
            claim_seconds,
        )
        if crawled is None:
            return
        job, response_payload = crawled
        await _publish_success(
            request_id,
            job_store=job_store,
            result_store=result_store,
            response_payload=response_payload,
            owner=job.owner,
            webhook_url=webhook_url,
            claim_token=claim_token,
            container=container,
        )
    except asyncio.CancelledError:
        await _record_failure(
            request_id, job_store, "cancelled_shutdown", webhook_url, claim_token, container
        )
        raise
    except Exception as exc:
        await _record_failure(request_id, job_store, str(exc), webhook_url, claim_token, container)


async def _crawl_under_semaphore(
    request_id: str,
    payload: CrawlRequest,
    crawl_settings: CrawlerSettings,
    job_store,
    result_store,
    shutdown_event: asyncio.Event,
    url_pairs: list[tuple[str, NormalizedUrl]] | None,
    spill_store: CrawlSpillStore | None,
    delta_context: DeltaContext | None,
    claim_token: str,
    claim_seconds: float,
) -> tuple[CrawlJobState, dict[str, object]] | None:
    """Claim and crawl under the bounded semaphore; return (job, payload) or None."""
    async with async_crawl_semaphore():
        job = await _claim_running(request_id, job_store, claim_token, claim_seconds)
        if job is None:
            return None
        stop = asyncio.Event()
        abort = asyncio.Event()
        forward = asyncio.create_task(_forward_shutdown(shutdown_event, abort))
        heartbeat = asyncio.create_task(
            _heartbeat_claim(request_id, job_store, claim_token, claim_seconds, stop, abort)
        )
        try:
            response_payload = await _crawl_and_build_response(
                request_id,
                payload,
                crawl_settings,
                abort,
                url_pairs,
                job,
                job_store,
                spill_store=spill_store,
                delta_context=delta_context,
            )
        finally:
            stop.set()
            forward.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await forward
            await heartbeat
        return job, response_payload


async def _claim_running(
    request_id: str, job_store, claim_token: str, claim_seconds: float
) -> CrawlJobState | None:
    """Load the job and claim RUNNING via CAS with a runner lease."""
    job = await job_store.aload(request_id)
    if job is None:
        logger.error("async_crawl_job_missing", extra={"request_id": request_id})
        return None
    claimed = await job_store.aset_status(
        request_id,
        JobStatus.RUNNING,
        expected=JobStatus.PENDING,
        claimed_by=claim_token,
        claim_now=time.time(),
        claim_seconds=claim_seconds,
    )
    if not claimed:
        logger.error("async_crawl_claim_lost", extra={"request_id": request_id})
        return None
    return await job_store.aload(request_id)


async def _forward_shutdown(shutdown_event: asyncio.Event, abort: asyncio.Event) -> None:
    await shutdown_event.wait()
    abort.set()


async def _heartbeat_claim(
    request_id: str,
    job_store,
    claim_token: str,
    claim_seconds: float,
    stop: asyncio.Event,
    abort: asyncio.Event,
) -> None:
    interval = max(1.0, min(30.0, claim_seconds / 3))
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            if not await job_store.arenew_job_claim(request_id, claim_token, now=time.time()):
                abort.set()
                return


__all__ = ["run_async_crawl", "async_crawl_semaphore"]


async def _crawl_and_build_response(
    request_id: str,
    payload: CrawlRequest,
    crawl_settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    url_pairs: list[tuple[str, NormalizedUrl]] | None,
    job,
    job_store,
    *,
    spill_store: CrawlSpillStore | None = None,
    delta_context: DeltaContext | None = None,
) -> dict[str, object]:
    """Execute the crawl, observe metrics, and build the unified response.

    Job results and stats are written here: the store is the single source of
    truth for async jobs; unified results are dict-tolerant (never crash stats).
    """
    _crawl_started = time.monotonic()
    result = await execute_crawl(
        payload,
        settings=crawl_settings,
        shutdown_event=shutdown_event,
        url_pairs=url_pairs,
        spill_store=spill_store,
    )
    result["request_id"] = request_id
    await record_crawl_completion(
        result,
        _crawl_started,
        event="crawl_request_completed",
        logger=logger,
        caller_id=job.owner,
    )
    response_model = await abuild_unified_crawl_response(
        result.get("results", []),
        adapt=build_unified_result,
        options=EnvelopeOptions(
            request_id=request_id,
            submitted_count=result.get("submitted_count"),
            policy_schema_version=result.get("policy_schema_version", ""),
        ),
        delta_context=delta_context,
    )
    _persist_unified_job_results(job, response_model)
    await job_store.asave(job)
    return response_model.model_dump()


def _persist_unified_job_results(job, response_model: UnifiedCrawlResponse) -> None:
    """Write the unified results and the two single-shot stats back onto ``job``.

    Single-shot jobs have no frontier, so "discovered" is undefined and only
    what happened is reported; the whole-site runner owns discovery stats.
    """
    response_payload = response_model.model_dump()

    def _failed(item) -> bool:
        status = item.get("status") if isinstance(item, dict) else getattr(item, "status", None)
        return status == "failed"

    unified_results = response_model.results
    failed = sum(1 for item in unified_results if _failed(item))
    job.results = [response_payload]
    job.stats = {
        "total_crawled": len(unified_results),
        "total_failed": failed,
    }


async def _publish_success(
    request_id: str,
    *,
    job_store,
    result_store,
    response_payload: dict[str, object],
    owner,
    webhook_url: WebhookTarget | None,
    claim_token: str,
    container: EffectContainer,
) -> None:
    """CAS COMPLETED, store the payload, POST the webhook. Bounded by the container."""
    published = await job_store.aset_status(
        request_id,
        JobStatus.COMPLETED,
        expected=JobStatus.RUNNING,
        claimed_by=claim_token,
        require_owner=True,
        clear_claim=True,
    )
    if not published:
        return
    try:
        await result_store.aput(request_id, response_payload, owner=owner)
    except Exception:
        logger.warning("crawl_result_store_failed", extra={"request_id": request_id})
    await _deliver_webhook(webhook_url, response_payload, container)


async def _deliver_webhook(
    webhook_url: WebhookTarget | None, payload: dict[str, object], container: EffectContainer
) -> None:
    """One bounded delivery of the admitted target; admission lived at the door."""
    if webhook_url is None:
        return
    from crawlutron.service.webhooks import post_webhook_guarded

    read_timeout = Timeouts.defaults().read

    async def _webhook_op() -> None:
        await post_webhook_guarded(webhook_url, payload)

    await container.webhook_post(
        target=webhook_url,
        payload=payload,
        op=_webhook_op,
        timeout_seconds=read_timeout,
        caller_timeout_seconds=read_timeout,
        bounds=EffectBounds(
            timeout_seconds=0.0,
            caller_timeout_seconds=read_timeout,
            floor_seconds=read_timeout,
            min_seconds=webhook_drain_deadline_seconds(),
        ),
    )


async def _record_failure(
    request_id: str,
    job_store,
    error: str,
    webhook_url: WebhookTarget | None,
    claim_token: str,
    container: EffectContainer,
) -> None:
    """Record the terminal FAILED write and POST the error webhook when it won."""
    logger.error("async_crawl_failed", extra={"request_id": request_id, "error": error})
    recorded = await arecord_terminal_failure(job_store, request_id, error, claimed_by=claim_token)
    if webhook_url is not None and recorded:
        with contextlib.suppress(Exception):
            error_payload = (
                await abuild_unified_crawl_response(
                    [],
                    options=EnvelopeOptions(request_id=request_id, success=False),
                    adapt=build_unified_result,
                )
            ).model_dump()
            await _deliver_webhook(webhook_url, error_payload, container)
