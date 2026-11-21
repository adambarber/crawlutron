"""Batch-boundary result absorption for durable job crawls."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from crawlutron.admission.admission import (
    JobAdmissionContext,
    admit_job_links,
    requeue_admitted_pages,
)
from crawlutron.admission.admission_links import AdmissionOutcome
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.crawl_status import CrawlStatus
from crawlutron.models.unified_crawl_result import UnifiedCrawlResult
from crawlutron.service.response_shaping import failed_unified_result
from crawlutron.service.unified_shaping import abuild_unified_result
from crawlutron.storage.job_store import CrawlJobState, FrontierEntry
from crawlutron.vocabulary.limits import MAX_URL_TRANSIENT_ATTEMPTS
from crawlutron.vocabulary.taxonomy import CrawlClassification, is_transient_classification
from safe_url.normalized_url import NormalizedUrl
from safe_url.url_redaction import redact_url

_logger = logging.getLogger(__name__)


async def _entry_result(
    job: CrawlJobState,
    identity: NormalizedUrl,
    item: EngineCrawlResult | None,
    depth: int,
    context: JobAdmissionContext,
    known: set[NormalizedUrl],
) -> int:
    if item is None:
        job.results.append(failed_unified_result(identity).model_dump())
        return 1
    if is_transient_classification(item.classification):
        item = replace(item, status="failed", error="retries_exhausted")
    try:
        unified = await abuild_unified_result(item)
    except Exception:
        unified = failed_unified_result(item)
    job.results.append(unified.model_dump())
    failed = 1 if item.status == "failed" else 0
    try:
        admitted = await admit_job_links(
            job, item.discovered_links, source_depth=depth, context=context, known=known
        )
    except Exception:
        url_attr = item.normalized_url.raw if item.normalized_url else "<unknown>"
        _logger.warning("link_admission_failed", extra={"url": redact_url(url_attr)})
        admitted = AdmissionOutcome()
    known |= admitted.known_delta  # unioning the delta copies nothing whole
    job.stats["total_discovered"] = job.stats.get("total_discovered", 0) + admitted.accepted
    return failed


async def absorb_batch(
    job: CrawlJobState,
    batch: Sequence[FrontierEntry],
    resolved: dict[NormalizedUrl, EngineCrawlResult],
    context: JobAdmissionContext,
    known: set[NormalizedUrl],
    *,
    timed_out: bool = False,
) -> int:
    """Absorb one batch, keeping timeout failures out of the transient ladder."""
    failed = 0
    for entry in batch:
        identity = entry.url
        job.in_flight = [pending for pending in job.in_flight if pending.url != identity]
        known.discard(identity)
        item = resolved.get(identity)
        if timed_out and item is None:
            # The batch bound, not the URL, expired: name that outcome and keep
            # these URLs out of the transient ladder a whole-batch retry would feed.
            job.transient_retries.pop(identity, None)
            job.visited.add(identity)
            known.add(identity)
            job.results.append(
                UnifiedCrawlResult(
                    original_url=identity.raw,
                    normalized_url=identity,
                    final_url=None,
                    status=CrawlStatus.failed,
                    classification=CrawlClassification.url_budget_exceeded,
                    error="effect_timeout",
                ).model_dump()
            )
            failed += 1
            continue
        transient = item is None or is_transient_classification(item.classification)
        if transient and job.transient_retries.get(identity, 0) + 1 < MAX_URL_TRANSIENT_ATTEMPTS:
            job.transient_retries[identity] = job.transient_retries.get(identity, 0) + 1
            requeue_admitted_pages(
                job,
                (FrontierEntry(url=identity, depth=entry.depth),),
                context,
                known=known,
            )
            continue
        job.transient_retries.pop(identity, None)
        job.visited.add(identity)
        known.add(identity)
        failed += await _entry_result(job, identity, item, entry.depth, context, known)
    job.stats["total_crawled"] = len(job.visited)
    if failed:
        job.stats["total_failed"] = job.stats.get("total_failed", 0) + failed
    return failed
