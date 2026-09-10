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
import logging
from typing import Any

from app.api.crawl_executor import execute_crawl
from app.api.response_shaping import build_unified_result
from app.api_models import UnifiedCrawlResponse
from app.crawl.job_store import JobStatus
from app.metrics import get_metrics
from app.settings import CrawlerSettings
from app.validation import get_env_int

logger = logging.getLogger(__name__)

# Module-level concurrency semaphore. Bounded so a flood of 202 responses
# cannot spawn unbounded concurrent crawls and OOM the container
# (adversarial review F-no-concurrency-cap).
_WEBHOOK_CRAWL_CONCURRENCY = get_env_int(
    "BROWSER_CRAWLER_MAX_WEBHOOK_CONCURRENCY", 4, lo=1
)
_async_crawl_semaphore: asyncio.Semaphore | None = None


def _get_async_crawl_semaphore() -> asyncio.Semaphore:
    """Lazily construct the async-crawl semaphore (avoids event-loop dep at import)."""
    global _async_crawl_semaphore
    if _async_crawl_semaphore is None:
        _async_crawl_semaphore = asyncio.Semaphore(_WEBHOOK_CRAWL_CONCURRENCY)
    return _async_crawl_semaphore


async def run_async_crawl(
    request_id: str,
    payload_dict: dict[str, Any],
    webhook_url: str | None,
    crawl_settings: CrawlerSettings,
    job_store,
    result_store,
    shutdown_event: asyncio.Event,
) -> None:
    """Run a single-shot crawl in the background, persisting results to job_store.

    Updates the job through PENDING -> RUNNING -> COMPLETED/FAILED.
    If ``webhook_url`` is provided, POSTs the result on completion.
    """
    # Load job and mark RUNNING
    job = job_store.load(request_id)
    if job is None:
        logger.error("async_crawl_job_missing", extra={"request_id": request_id})
        return
    job.status = JobStatus.RUNNING
    job_store.save(job)

    try:
        async with _get_async_crawl_semaphore():
            result = await execute_crawl(
                payload_dict,
                settings=crawl_settings,
                shutdown_event=shutdown_event,
            )
            result["request_id"] = request_id
            get_metrics().observe_crawl(results=result.get("results", []))
            unified_results = [build_unified_result(item) for item in result.get("results", [])]
            response_payload = UnifiedCrawlResponse(
                request_id=request_id,
                submitted_count=result.get("submitted_count", len(unified_results)),
                policy_schema_version=result.get("policy_schema_version", ""),
                success=True,
                results=unified_results,
            ).model_dump()

            # Store results in job_store (single source of truth for async jobs)
            job.results = [response_payload]
            job.status = JobStatus.COMPLETED

            # unified_results are UnifiedCrawlResult models (dict-tolerant: the
            # runner must never crash stats computation for a shape variant).
            def _failed(item) -> bool:
                status = item.get("status") if isinstance(item, dict) else getattr(item, "status", None)
                return status == "failed"

            failed = sum(1 for item in unified_results if _failed(item))
            job.stats = {
                "total_discovered": len(unified_results),
                "total_crawled": len(unified_results),
                "total_failed": failed,
            }
            job_store.save(job)

            # Also store in result_store for backward compat (sync path reads from here)
            try:
                result_store.put(request_id, response_payload)
            except Exception:
                logger.warning("crawl_result_store_failed", extra={"request_id": request_id})

            # Deliver webhook if provided
            if webhook_url:
                from app.api.webhooks import post_webhook

                await post_webhook(webhook_url, response_payload)

    except Exception as exc:
        logger.error("async_crawl_failed", extra={"request_id": request_id, "error": str(exc)})
        job = job_store.load(request_id)
        if job is not None:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            job_store.save(job)
        if webhook_url:
            try:
                error_payload = UnifiedCrawlResponse(
                    request_id=request_id,
                    submitted_count=0,
                    policy_schema_version="",
                    success=False,
                    results=[],
                ).model_dump()
                from app.api.webhooks import post_webhook

                await post_webhook(webhook_url, error_payload)
            except Exception:
                pass


__all__ = ["run_async_crawl", "_get_async_crawl_semaphore", "_WEBHOOK_CRAWL_CONCURRENCY"]
