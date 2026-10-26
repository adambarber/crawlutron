"""Background async-crawl runner for ``/v1/crawl`` webhook deliveries.

Queued when ``async_mode=True`` or a ``webhook_url`` is set: acquire the bounded
semaphore, run via :mod:`app.api.crawl_executor`, persist in both stores, POST to the webhook.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from app.api.crawl_executor import execute_crawl
from app.api.job_runner import arecord_terminal_failure
from app.api.response_shaping import (
    EnvelopeOptions,
    abuild_unified_crawl_response,
)
from app.api.unified_shaping import build_unified_result
from app.config.env import get_env_int
from app.config.settings import CrawlerSettings
from app.models.crawl_request import CrawlRequest
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.runtime.metrics import get_metrics
from app.storage.job_store import TERMINAL_STATUSES, CrawlJobState, JobStatus

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
    if holder is None or holder.loop is not loop:
        holder = _SemaphoreHolder(_build_async_crawl_semaphore(), loop)
        _SEMAPHORE_HOLDER = holder
    return holder.semaphore


def reset_process_state() -> None:
    """Drop the webhook-crawl semaphore holder.

    The semaphore is process-wide: a test that swaps the builder needs the
    cache cleared, exactly as the pooled clients clear their session cache.
    """
    global _SEMAPHORE_HOLDER
    _SEMAPHORE_HOLDER = None


async def run_async_crawl(
    request_id: str,
    payload: CrawlRequest,
    webhook_url: str | None,
    crawl_settings: CrawlerSettings,
    job_store,
    result_store,
    shutdown_event: asyncio.Event,
    url_pairs: list[tuple[str, NormalizedUrl]] | None = None,
) -> None:
    """Run a single-shot crawl in the background, persisting results to job_store
    through PENDING -> RUNNING -> COMPLETED/FAILED and POSTing when ``webhook_url``
    is set. ``url_pairs`` carries the gate's admitted batch, never rebuilt."""
    webhook_policy = HostPolicy.from_settings(crawl_settings)
    try:
        # #462: claim RUNNING only after the semaphore is ours; a queued job must
        # not read RUNNING nor count in aactive_count_for_caller while it waits.
        async with async_crawl_semaphore():
            job = await _claim_running(request_id, job_store)
            if job is None:
                return
            response_payload = await _crawl_and_build_response(
                request_id, payload, crawl_settings, shutdown_event, url_pairs, job, job_store
            )
        # #463: deliver outside the semaphore -- a retrying webhook (up to 60s
        # of backoff) must not gate another caller's crawl capacity.
        await _publish_success(
            request_id,
            job_store=job_store,
            result_store=result_store,
            response_payload=response_payload,
            owner=job.owner,
            webhook_url=webhook_url,
            webhook_policy=webhook_policy,
        )
    except Exception as exc:
        await _record_failure(request_id, job_store, str(exc), webhook_url, webhook_policy)


async def _claim_running(request_id: str, job_store) -> CrawlJobState | None:
    """Load the job and claim RUNNING via CAS; a lost claim means the row was
    deleted or already transitioned (post-semaphore-acquire, per #462)."""
    job = await job_store.aload(request_id)
    if job is None:
        logger.error("async_crawl_job_missing", extra={"request_id": request_id})
        return None
    # Claim RUNNING on the control plane: status moves only via CAS;
    # a lost claim means the row was deleted or already transitioned).
    claimed = await job_store.aset_status(request_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    if not claimed:
        logger.error("async_crawl_claim_lost", extra={"request_id": request_id})
        return None
    return job


__all__ = ["run_async_crawl", "async_crawl_semaphore"]


async def _crawl_and_build_response(
    request_id: str,
    payload: CrawlRequest,
    crawl_settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    url_pairs: list[tuple[str, NormalizedUrl]] | None,
    job,
    job_store,
) -> dict[str, object]:
    """Execute the crawl, observe metrics, and build the unified response.

    Job results and stats are written here: the store is the single source of
    truth for async jobs; unified results are dict-tolerant (never crash stats).
    """
    result = await execute_crawl(
        payload,
        settings=crawl_settings,
        shutdown_event=shutdown_event,
        url_pairs=url_pairs,
    )
    result["request_id"] = request_id
    get_metrics().observe_crawl(results=result.get("results", []), caller_id=job.owner)
    response_model = await abuild_unified_crawl_response(
        result.get("results", []),
        adapt=build_unified_result,
        options=EnvelopeOptions(
            request_id=request_id,
            submitted_count=result.get("submitted_count"),
            policy_schema_version=result.get("policy_schema_version", ""),
        ),
    )
    response_payload = response_model.model_dump()

    job.results = [response_payload]

    def _failed(item) -> bool:
        status = item.get("status") if isinstance(item, dict) else getattr(item, "status", None)
        return status == "failed"

    unified_results = response_model.results
    failed = sum(1 for item in unified_results if _failed(item))
    # Single-shot jobs have no frontier, so "discovered" is undefined
    # and only what happened is reported; the whole-site runner owns it.
    job.stats = {
        "total_crawled": len(unified_results),
        "total_failed": failed,
    }
    await job_store.asave(job)
    return response_payload


async def _publish_success(
    request_id: str,
    *,
    job_store,
    result_store,
    response_payload: dict[str, object],
    owner,
    webhook_url: str | None,
    webhook_policy: HostPolicy,
) -> None:
    """CAS COMPLETED, store the payload, and POST the webhook.

    The terminal write is a CAS that can lose to pause_job, so its result
    decides finished; a stranded re-claim publishes against the real state.
    """
    published = await job_store.aset_status(request_id, JobStatus.COMPLETED, expected=JobStatus.RUNNING)
    if not published:
        stranded = await job_store.aload(request_id)
        if stranded is not None and stranded.status not in {
            JobStatus.PAUSED,
            JobStatus.CANCELLED,
            *TERMINAL_STATUSES,
        }:
            logger.warning(
                "async_crawl_terminal_cas_lost",
                extra={"request_id": request_id, "status": stranded.status.value},
            )
            published = await job_store.aset_status(request_id, JobStatus.COMPLETED, expected=stranded.status)

    if not published:
        return
    try:
        await result_store.aput(request_id, response_payload, owner=owner)
    except Exception:
        logger.warning("crawl_result_store_failed", extra={"request_id": request_id})
    # The webhook rides with the terminal write in both branches, so it
    # means "finished" -- announcing a paused crawl hands back a dead one.
    if webhook_url:
        from app.api.webhooks import post_webhook_guarded

        await post_webhook_guarded(
            webhook_url,
            response_payload,
            host_policy=webhook_policy,
        )


async def _record_failure(
    request_id: str,
    job_store,
    error: str,
    webhook_url: str | None,
    webhook_policy: HostPolicy,
) -> None:
    """Record the terminal FAILED write and POST the error webhook when it won.

    The error webhook announces a terminal FAILED write; only deliver it when
    the CAS won. A paused/cancelled job's terminal write lost, so no callback.
    """
    logger.error("async_crawl_failed", extra={"request_id": request_id, "error": error})
    recorded = await arecord_terminal_failure(job_store, request_id, error)
    if webhook_url and recorded:
        with contextlib.suppress(Exception):
            error_payload = (
                await abuild_unified_crawl_response(
                    [],
                    options=EnvelopeOptions(request_id=request_id, success=False),
                    adapt=build_unified_result,
                )
            ).model_dump()
            from app.api.webhooks import post_webhook_guarded

            await post_webhook_guarded(
                webhook_url,
                error_payload,
                host_policy=webhook_policy,
            )
