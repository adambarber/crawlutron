"""Background whole-site job runner for ``/v1/jobs``.

Consumes the persisted frontier in persistent batches through the shared
engine; every transition leaves the store resumable."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Any

from app.admission.admission import (
    JobAdmissionContext,
    admit_job_links,
    admitted_page_identities,
    fetch_job_sitemap_candidates,
    record_cap_overcommitted,
    record_cap_truncated,
    requeue_admitted_pages,
)
from app.admission.admission_links import AdmissionOutcome, remaining_page_capacity
from app.admission.policy import read_policy
from app.api.crawl_executor import execute_crawl as _real_execute_crawl
from app.api.job_lifecycle import (
    cancel_job_task,
    is_running,
    reset_job_runner,
    shutdown_job_runner,
    start_job,
    start_tracked_task,
    tracked_task,
)
from app.api.job_recovery import (
    recover_and_start_pending_jobs,
    recover_orphaned_running_jobs,
)
from app.api.response_shaping import failed_unified_result
from app.api.unified_shaping import abuild_unified_result
from app.models.crawl_request import CrawlRequest
from app.models.crawl_result import CrawlResult, EngineCrawlResult
from app.models.crawl_task import CrawlTask
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.models.result_factory import terminal_attempt
from app.models.result_record import ProducerEnvelope
from app.models.taxonomy import Classification
from app.orchestration.robots import FetchAndDecode
from app.storage.job_store import (
    FrontierEntry,
    JobStatus,
    JobStore,
    job_max_pages,
    normalize_job_state,
)
from app.vocabulary.limits import MAX_URL_TRANSIENT_ATTEMPTS
from app.vocabulary.taxonomy import is_transient_classification, status_for_classification

logger = logging.getLogger(__name__)


BATCH_SIZE = 8


ExecuteCrawl = Callable[..., Awaitable[ProducerEnvelope]]


_execute_crawl: ExecuteCrawl = _real_execute_crawl


def install_execute_crawl(fn: ExecuteCrawl) -> None:
    """Swap the engine seam (tests only)."""
    global _execute_crawl
    _execute_crawl = fn


async def arecord_terminal_failure(job_store: JobStore, job_id: str, error: str) -> bool:
    """Flip a RUNNING job to FAILED with ``error`` in the same CAS write; True iff
    it landed. A job past RUNNING is left as-is and reports False: no partial
    stamp, so a lost CAS cannot leave the error on a non-FAILED row (#365).
    """
    job = await job_store.aload(job_id)
    if job is None or job.status is not JobStatus.RUNNING:
        return False
    return await job_store.aset_status(job_id, JobStatus.FAILED, expected=JobStatus.RUNNING, error=error)


def reset_execute_crawl() -> None:
    global _execute_crawl
    _execute_crawl = _real_execute_crawl


def _pop_frontier(job: Any) -> list[FrontierEntry]:
    normalize_job_state(job)
    max_pages = job_max_pages(job.policy, job.tier)
    if job.in_flight:
        fixed = set(job.visited) | {entry.url for entry in job.in_flight}
        if max_pages > 0 and len(fixed) > max_pages:
            record_cap_overcommitted(job)
        return list(job.in_flight[:BATCH_SIZE])
    capacity = remaining_page_capacity(len(job.visited), max_pages)
    if capacity is not None and capacity <= 0:
        if job.frontier:
            record_cap_truncated(job)
        del job.frontier[:]
        return []
    count = min(BATCH_SIZE, len(job.frontier))
    if capacity is not None:
        count = min(count, capacity)
    if capacity is not None and count == capacity and count < len(job.frontier):
        record_cap_truncated(job)
        del job.frontier[count:]
    batch = list(job.frontier[:count])
    del job.frontier[:count]
    return batch


def job_engine_payload(job: Any, batch: Sequence[FrontierEntry]) -> CrawlRequest:
    """Build one non-recursive engine batch from a whole-site job.

    The runner owns the durable frontier; the engine is a single-page fetcher per
    batch. Two owners of one property was the defect, so engine recursion is off.
    """
    # The engine's wire shape carries text in ``urls``; the entry's URL is an
    # admitted value by construction, so this projects identity, not validation.
    urls = [entry.url.raw for entry in batch]
    policy = read_policy(job.policy, job.tier)
    # Model_copy is safe because max_depth=0 is a declared value and this line
    # is ours, not a caller's: one engine batch is one page deep by definition.
    policy = policy.model_copy(update={"limits": policy.limits.model_copy(update={"max_depth": 0})})
    return CrawlRequest(urls=urls, policy=policy)


async def pace_delayed_batch(
    context: JobAdmissionContext,
    batch: Sequence[FrontierEntry],
) -> None:
    tasks: list[CrawlTask] = []
    for index, entry in enumerate(batch):
        # The membership test uses the same canonical key commit_crawl_delay
        # recorded (#445/#497): a host must never miss its own site's delay.
        if entry.url.site in context.delayed_hosts:
            tasks.append(
                CrawlTask(
                    index=index,
                    original_url=entry.url.raw,
                    normalized_url=entry.url,
                )
            )
    if not tasks:
        return

    def pace_result(job: CrawlTask) -> CrawlResult:
        # The status is derived from the label, not spelled beside it: two literals
        # go stale the day the taxonomy moves, and nothing in the suite sees it.
        classification: Classification = "rendered"
        return CrawlResult(
            url=job.normalized_url,
            attempt=terminal_attempt(
                strategy=None,
                status=status_for_classification(classification),
                classification=classification,
                elapsed_ms=0,
                error=None,
            ),
            final_url=job.normalized_url,
            html=None,
            text=None,
            attempts=0,
            domain=job.domain,
        )

    async def no_op(job: CrawlTask) -> CrawlResult:
        return pace_result(job)

    await context.scheduler.run(
        tasks,
        no_op,
        failed_result=lambda job, exc: pace_result(job),
    )


async def _seed_job_sitemaps(job: Any, context: JobAdmissionContext) -> None:
    candidates = await fetch_job_sitemap_candidates(job.root_url, context)
    outcome = await admit_job_links(job, candidates, source_depth=-1, context=context)
    job.stats["total_discovered"] = job.stats.get("total_discovered", 0) + outcome.accepted


async def _load_and_claim(job_id: str, job_store: JobStore, *, preclaimed: bool) -> Any | None:
    """Load a job and claim it, or ``None`` when there is nothing to run."""
    job = await job_store.aload(job_id)
    if job is None:
        return None
    if not preclaimed:
        # The claim is the whole decision: a row that is not PENDING here
        # belongs to another process or to an operator who stopped it.
        if not await job_store.aset_status(job_id, JobStatus.RUNNING, expected=JobStatus.PENDING):
            return None
    await job_store.asave(job)
    return job


async def _seed_sitemaps(job: Any, context: JobAdmissionContext, job_store: JobStore) -> None:
    """Seed discovery sitemaps when the policy calls for them."""
    if not (context.max_depth > 0 and context.seed_sitemaps):
        return
    await _seed_job_sitemaps(job, context)
    await job_store.asave(job)


async def _entry_result(
    job: Any,
    identity: NormalizedUrl,
    item: EngineCrawlResult | None,
    depth: int,
    context: JobAdmissionContext,
    known: set[NormalizedUrl],
) -> int:
    """Record one entry's result into ``job``; returns ``1`` if it failed.

    ``known`` is the batch's threaded occupied set (#381): admission folds its
    ``known_delta`` back in, so the next entry dedupes against the value.
    """
    if item is None:
        # The batch's own shaping envelope names the unanswered URL and carries
        # the admitted value, reused rather than reinvented.
        job.results.append(failed_unified_result(identity).model_dump())
        return 1
    if is_transient_classification(item.classification):
        # The frozen dataclass's own substitute constructor: overridden fields
        # are named and checked, never merged through a plain dict.
        item = replace(item, status="failed", error="retries_exhausted")
    try:
        unified = await abuild_unified_result(item)
    except Exception:
        # Shaping is the batch's last line of defense and not meant to raise.
        unified = failed_unified_result(item)
    job.results.append(unified.model_dump())
    failed = 1 if item.status == "failed" else 0
    try:
        admitted = await admit_job_links(
            job, item.discovered_links, source_depth=depth, context=context, known=known
        )
    except Exception:
        # One entry's link admission must not lose the whole job: drop its links
        # and let the remaining entries resolve.
        url_attr = item.normalized_url.raw if item.normalized_url else "<unknown>"
        logger.warning("link_admission_failed", extra={"url": url_attr})
        admitted = AdmissionOutcome()
    known |= admitted.known_delta  # unioning the delta copies nothing whole (#519)
    job.stats["total_discovered"] = job.stats.get("total_discovered", 0) + admitted.accepted
    return failed


async def _should_stop(job: Any, job_id: str, job_store: JobStore, shutdown_event: asyncio.Event) -> bool:
    """Whether the loop ends this iteration; a shutdown sets PAUSED first."""
    if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        return True
    if shutdown_event.is_set():
        await job_store.aset_status(job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)
        return True
    return False


async def _mark_complete_if_empty(job: Any, job_id: str, job_store: JobStore, batch: Sequence[Any]) -> bool:
    """Mark the job COMPLETED when its frontier yielded nothing; else ``False``."""
    if batch:
        return False
    await job_store.asave(job)
    await job_store.aset_status(job_id, JobStatus.COMPLETED, expected=JobStatus.RUNNING)
    return True


async def _absorb_batch(
    job: Any,
    batch: Sequence[Any],
    resolved: dict[NormalizedUrl, EngineCrawlResult],
    context: JobAdmissionContext,
) -> int:
    """Fold one resolved batch into ``job``; returns the failed count."""
    failed = 0
    known = admitted_page_identities(job, host_policy=context.host_policy)
    for entry in batch:
        identity = entry.url
        job.in_flight = [pending for pending in job.in_flight if pending.url != identity]
        item = resolved.get(identity)
        transient = item is None or is_transient_classification(item.classification)
        if transient and job.transient_retries.get(identity, 0) + 1 < MAX_URL_TRANSIENT_ATTEMPTS:
            job.transient_retries[identity] = job.transient_retries.get(identity, 0) + 1
            requeue_admitted_pages(
                job,
                (FrontierEntry(url=identity, depth=entry.depth),),
                context,
            )
            continue
        job.transient_retries.pop(identity, None)
        job.visited.add(identity)
        failed += await _entry_result(job, identity, item, entry.depth, context, known)
    return failed


async def run_job(
    job_id: str,
    job_store: JobStore,
    settings: Any,
    shutdown_event: asyncio.Event,
    fetch_and_decode: FetchAndDecode | None = None,
    *,
    preclaimed: bool = False,
) -> None:
    """Whole-site engine loop for one job; ``preclaimed`` says the caller's own
    PAUSED->RUNNING flip is the claim, so every other row is claimed by the CAS."""
    job = await _load_and_claim(job_id, job_store, preclaimed=preclaimed)
    if job is None:
        return
    try:
        context = JobAdmissionContext.create(job.policy, job.tier, settings, fetch_and_decode)
        await _seed_sitemaps(job, context, job_store)
        while True:
            job = await job_store.aload(job_id)
            if job is None:
                return
            if await _should_stop(job, job_id, job_store, shutdown_event):
                return
            batch = _pop_frontier(job)
            if await _mark_complete_if_empty(job, job_id, job_store, batch):
                return
            if not job.in_flight:
                job.in_flight = list(batch)
            await job_store.asave(job)
            await pace_delayed_batch(context, batch)
            result = await _execute_crawl(
                job_engine_payload(job, batch), settings=settings, shutdown_event=shutdown_event
            )
            items = result["results"]
            reloaded = await job_store.areload_state(job_id)
            if reloaded is None:
                return
            job = replace(reloaded, results=job.results, visited=job.visited)
            # ``original_url`` wins the identity, redirects fill the gaps; each
            # produced result is admitted under the job's own host policy.
            resolved: dict[NormalizedUrl, EngineCrawlResult] = {}
            for produced in items:
                admitted_original = admit_or_none(produced.original_url, policy=job.host_policy)
                if admitted_original is not None:
                    resolved.setdefault(admitted_original, produced)
            for produced in items:
                for key in (produced.final_url, produced.normalized_url):
                    if key is not None:
                        resolved.setdefault(key, produced)
            failed = await _absorb_batch(job, batch, resolved, context)
            job.stats["total_crawled"] = len(job.visited)
            if failed:
                job.stats["total_failed"] = job.stats.get("total_failed", 0) + failed
            await job_store.asave(job)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("job_runner_failed job_id=%s error=%r", job_id, exc)
        await arecord_terminal_failure(job_store, job_id, str(exc))


async def _transition(
    job_store: JobStore,
    job_id: str,
    to: JobStatus,
    *,
    expected: JobStatus | frozenset[JobStatus],
) -> str:
    """Flip a job's status and say why if the flip did not happen.

    One three-way answer -- applied, missing, conflict -- produced once, not
    per control. Missing is told from conflict by re-reading the row."""
    if await job_store.aset_status(job_id, to, expected=expected):
        return "ok"
    return "missing" if await job_store.aload(job_id) is None else "conflict"


async def pause_job(job_id: str, job_store: JobStore) -> str:
    """Pause: flip the row; the task exits at the next batch boundary."""
    return await _transition(job_store, job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)


async def resume_job(job_id: str, job_store: JobStore, settings: Any, shutdown_event: asyncio.Event) -> str:
    """Resume a paused job by respawning its runner task."""
    # A stale predecessor task still owning the slot must not leave a RUNNING
    # row with no runner (#523 COR-08): take the registry's slot-lock path.
    outcome = await _transition(job_store, job_id, JobStatus.RUNNING, expected=JobStatus.PAUSED)
    if outcome == "ok":
        # The flip above is this process's claim, so the runner is told not to
        # take a second one -- the only way it can run without the CAS failing.
        old = tracked_task(job_id)
        if old is not None:
            try:
                await asyncio.wait_for(asyncio.shield(old), timeout=5.0)
            except TimeoutError:
                # Still wedged past the grace period: cancel and join it so the
                # spawn below actually claims the freed slot.
                old.cancel()
                try:
                    await old
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                raise
        start_job(job_id, job_store, settings, shutdown_event, preclaimed=True)
    return outcome


async def cancel_job(job_id: str, job_store: JobStore) -> str:
    """Cancel: flip the row; the task exits at the next batch boundary (the
    in-flight batch is still recorded — the engine already did the work)."""
    return await _transition(
        job_store,
        job_id,
        JobStatus.CANCELLED,
        expected=frozenset({JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.PENDING}),
    )


__all__ = [
    "BATCH_SIZE",
    "cancel_job",
    "cancel_job_task",
    "install_execute_crawl",
    "is_running",
    "pace_delayed_batch",
    "pause_job",
    "recover_orphaned_running_jobs",
    "run_job",
    "recover_and_start_pending_jobs",
    "reset_job_runner",
    "resume_job",
    "shutdown_job_runner",
    "start_job",
    "start_tracked_task",
]
