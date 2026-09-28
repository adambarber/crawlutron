"""Background async-crawl runner for ``/v1/crawl`` webhook deliveries.

When ``/v1/crawl`` receives ``async_mode=True`` or a ``webhook_url``, it
queues a :func:`run_async_crawl` task that:

1. Acquires the bounded webhook-crawl concurrency semaphore.
2. Runs the crawl via the shared :mod:`app.api.crawl_executor` helper.
3. Persists results in both the durable job store and the bounded result
   cache (for backward compatibility with ``/v1/crawl/{request_id}``).
4. POSTs the result to ``webhook_url`` via the pinned-delivery helper from
   :mod:`app.api.webhooks`.

Extracted from ``app/main.py`` so the background-task implementation and
the request handlers live in different modules. The runner accesses shared
state via the ``Depends(get_settings)`` / ``Depends(get_shutdown_event)``
providers wired in :mod:`app.api.dependencies`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from app.api.crawl_executor import execute_crawl
from app.api.response_shaping import EnvelopeOptions, abuild_unified_crawl_response
from app.crawl.job_store import JobStatus
from app.metrics import get_metrics
from app.settings import CrawlerSettings
from app.validation import HostPolicy, get_env_int

logger = logging.getLogger(__name__)

# Module-level concurrency semaphore. Bounded so a flood of 202 responses
# cannot spawn unbounded concurrent crawls and OOM the container
# (adversarial review F-no-concurrency-cap).
_WEBHOOK_CRAWL_CONCURRENCY = get_env_int("BROWSER_CRAWLER_MAX_WEBHOOK_CONCURRENCY", 4, lo=1)
# P2-C: a lazily-built module-global asyncio.Semaphore binds to whichever event
# loop first uses it, and asyncio's loop-bound primitives cannot be awaited from
# a loop other than the one they were created on -- the same hazard
# ``_LoopBoundPooledClient`` was built for in app.crawl.strategies.http. The
# instance therefore tracks the loop it was built for and is rebuilt when the
# running loop changes (for example a test harness driving many asyncio.run
# calls in sequence).
#
# The pooled instance stays in this module global, and the builder stays a
# separate late-bound function, for the same reasons the pooled client does:
# service teardown and the suites reset pooled state by assigning the global,
# and a test that monkeypatches ``_build_async_crawl_semaphore`` must still be
# observed. Nothing is constructed at import time, so importing this module
# never touches an event loop.
_async_crawl_semaphore: asyncio.Semaphore | None = None
_async_crawl_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _build_async_crawl_semaphore() -> asyncio.Semaphore:
    """Construct the webhook-crawl semaphore.

    Kept as its own module-level name (rather than inline in
    ``_get_async_crawl_semaphore``) so tests can patch the builder and have a
    rebuild observe it, mirroring the pooled clients' late-bound ``build``.
    """
    return asyncio.Semaphore(_WEBHOOK_CRAWL_CONCURRENCY)


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

    Updates the job through PENDING -> RUNNING -> COMPLETED/FAILED.
    If ``webhook_url`` is provided, POSTs the result on completion.
    ``urls`` carries the endpoint's already-gated batch forward (tier-2 T3).
    """
    # Load job and mark RUNNING
    job = await job_store.aload(request_id)
    if job is None:
        logger.error("async_crawl_job_missing", extra={"request_id": request_id})
        return
    # Claim RUNNING on the control plane (H3: status moves only via CAS;
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
            # Single-shot jobs never enqueue a frontier, so "discovered" is
            # undefined for them: report only what actually happened. The
            # whole-site runner owns total_discovered (frontier-derived);
            # fabricating it here would overload one field name with two
            # different meanings.
            job.stats = {
                "total_crawled": len(unified_results),
                "total_failed": failed,
            }
            await job_store.asave(job)
            await job_store.aset_status(request_id, JobStatus.COMPLETED, expected=JobStatus.RUNNING)

            # Also store in result_store for backward compat (sync path reads from here)
            try:
                await result_store.aput(request_id, response_payload)
            except Exception:
                logger.warning("crawl_result_store_failed", extra={"request_id": request_id})

            # Deliver webhook if provided
            if webhook_url:
                from app.api.webhooks import post_webhook

                await post_webhook(
                    webhook_url,
                    response_payload,
                    host_policy=HostPolicy.from_settings(crawl_settings),
                )

    except Exception as exc:
        logger.error("async_crawl_failed", extra={"request_id": request_id, "error": str(exc)})
        job = await job_store.aload(request_id)
        if job is not None:
            job.error = str(exc)
            await job_store.asave(job)
            await job_store.aset_status(request_id, JobStatus.FAILED, expected=JobStatus.RUNNING)
        if webhook_url:
            with contextlib.suppress(Exception):
                error_payload = (
                    await abuild_unified_crawl_response(
                        [], options=EnvelopeOptions(request_id=request_id, success=False)
                    )
                ).model_dump()
                from app.api.webhooks import post_webhook

                await post_webhook(
                    webhook_url,
                    error_payload,
                    host_policy=HostPolicy.from_settings(crawl_settings),
                )


__all__ = ["_WEBHOOK_CRAWL_CONCURRENCY", "_get_async_crawl_semaphore", "run_async_crawl"]
