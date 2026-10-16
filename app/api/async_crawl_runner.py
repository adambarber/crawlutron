"""Background async-crawl runner for ``/v1/crawl`` webhook deliveries.

Queued when ``async_mode=True`` or a ``webhook_url`` is set: acquire the bounded
semaphore, run via :mod:`app.api.crawl_executor`, persist in both stores, POST to the webhook.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from app.api.crawl_executor import execute_crawl
from app.api.job_runner import arecord_terminal_failure
from app.api.response_shaping import (
    EnvelopeOptions,
    abuild_unified_crawl_response,
    build_unified_result,
)
from app.config.env import get_env_int
from app.config.settings import CrawlerSettings
from app.models.host_policy import HostPolicy
from app.runtime.metrics import get_metrics
from app.storage.job_store import TERMINAL_STATUSES, JobStatus

logger = logging.getLogger(__name__)

# Module-level concurrency semaphore. Bounded so a flood of 202 responses
# cannot spawn unbounded concurrent crawls and OOM the container.
WEBHOOK_CRAWL_CONCURRENCY = get_env_int("CRAWLUTRON_MAX_WEBHOOK_CONCURRENCY", 4, lo=1)
# The semaphore is loop-bound and rebuilt when the running loop changes (a
# harness driving many asyncio.run calls); the builder stays late-bound.
_async_crawl_semaphore: asyncio.Semaphore | None = None
_async_crawl_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _build_async_crawl_semaphore() -> asyncio.Semaphore:
    """Construct the webhook-crawl semaphore.

    Kept as its own module-level name so tests can patch the builder and a
    rebuild observe it, mirroring the pooled clients' late-bound ``build``.
    """
    return asyncio.Semaphore(WEBHOOK_CRAWL_CONCURRENCY)


def _get_async_crawl_semaphore() -> asyncio.Semaphore:
    """Return the webhook-crawl semaphore bound to the running event loop.

    Lazily constructed (no event-loop dependency at import) and rebuilt when the
    running loop is not the one the cached semaphore was built for.
    """
    global _async_crawl_semaphore, _async_crawl_semaphore_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _async_crawl_semaphore is None or _async_crawl_semaphore_loop is not loop:
        _async_crawl_semaphore = _build_async_crawl_semaphore()
        _async_crawl_semaphore_loop = loop
    return _async_crawl_semaphore


def reset_process_state() -> None:
    """Drop the cached webhook-crawl semaphore and its event-loop binding.

    The semaphore is process-wide: a test that swaps the builder needs the
    cache cleared, exactly as the pooled clients clear their session cache.
    """
    global _async_crawl_semaphore, _async_crawl_semaphore_loop
    _async_crawl_semaphore = None
    _async_crawl_semaphore_loop = None


async def run_async_crawl(
    request_id: str,
    payload_dict: dict[str, Any],
    webhook_url: str | None,
    crawl_settings: CrawlerSettings,
    job_store,
    result_store,
    shutdown_event: asyncio.Event,
    urls: list[str] | None = None,
) -> None:
    """Run a single-shot crawl in the background, persisting results to job_store.

    Updates the job through PENDING -> RUNNING -> COMPLETED/FAILED; POSTs the result
    on completion if ``webhook_url`` is given. ``urls`` carries the gated batch forward.
    """
    # Load job and mark RUNNING
    job = await job_store.aload(request_id)
    if job is None:
        logger.error("async_crawl_job_missing", extra={"request_id": request_id})
        return
    # Claim RUNNING on the control plane: status moves only via CAS;
    # a lost claim means the row was deleted or already transitioned).
    claimed = await job_store.aset_status(request_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    if not claimed:
        logger.error("async_crawl_claim_lost", extra={"request_id": request_id})
        return

    try:
        async with _get_async_crawl_semaphore():
            result = await execute_crawl(
                payload_dict,
                settings=crawl_settings,
                shutdown_event=shutdown_event,
                urls=urls,
            )
            result["request_id"] = request_id
            get_metrics().observe_crawl(results=result.get("results", []))
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

            # Store results in job_store (single source of truth for async jobs)
            job.results = [response_payload]

            # unified_results are UnifiedCrawlResult models (dict-tolerant: the
            # runner must never crash stats computation for a shape variant).
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
            # The terminal write is a CAS that can lose to pause_job, so its
            # result decides finished: an unchecked CAS let a paused job publish.
            published = await job_store.aset_status(
                request_id, JobStatus.COMPLETED, expected=JobStatus.RUNNING
            )
            if not published:
                # A second app instance can rewind the job to PENDING under a live
                # runner; leave PAUSED/CANCELLED and publish against the real state.
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
                    published = await job_store.aset_status(
                        request_id, JobStatus.COMPLETED, expected=stranded.status
                    )

            if published:
                try:
                    await result_store.aput(request_id, response_payload)
                except Exception:
                    logger.warning("crawl_result_store_failed", extra={"request_id": request_id})

                # The webhook rides with the terminal write in both branches, so it
                # means "finished" -- announcing a paused crawl hands back a dead one.
                if webhook_url:
                    from app.api.webhooks import post_webhook

                    await post_webhook(
                        webhook_url,
                        response_payload,
                        host_policy=HostPolicy.from_settings(crawl_settings),
                    )

    except Exception as exc:
        logger.error("async_crawl_failed", extra={"request_id": request_id, "error": str(exc)})
        await arecord_terminal_failure(job_store, request_id, str(exc))
        if webhook_url:
            with contextlib.suppress(Exception):
                error_payload = (
                    await abuild_unified_crawl_response(
                        [],
                        options=EnvelopeOptions(request_id=request_id, success=False),
                        adapt=build_unified_result,
                    )
                ).model_dump()
                from app.api.webhooks import post_webhook

                await post_webhook(
                    webhook_url,
                    error_payload,
                    host_policy=HostPolicy.from_settings(crawl_settings),
                )


__all__ = ["run_async_crawl"]
