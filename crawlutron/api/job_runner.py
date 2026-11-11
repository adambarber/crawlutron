"""Background whole-site job runner for ``/v1/jobs``.

Consumes the persisted frontier in persistent batches through the shared
engine; every transition leaves the store resumable."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace

from crawlutron.admission.admission import (
    JobAdmissionContext,
    admit_job_links,
    admitted_page_identities,
    fetch_job_sitemap_candidates,
    record_cap_overcommitted,
    record_cap_truncated,
    requeue_admitted_pages,
)
from crawlutron.admission.admission_links import AdmissionOutcome, remaining_page_capacity
from crawlutron.admission.policy import read_policy
from crawlutron.api.crawl_executor import execute_crawl as _real_execute_crawl
from crawlutron.api.job_lifecycle import (
    cancel_all_tasks,
    cancel_job_task,
    is_running,
    shutdown_job_runner,
    start_tracked_task,
    tracked_task,
)
from crawlutron.api.job_recovery import (
    recover_and_start_pending_jobs,
    recover_orphaned_running_jobs,
)
from crawlutron.api.response_shaping import failed_unified_result
from crawlutron.api.unified_shaping import abuild_unified_result
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.normalized_url import NormalizedUrl, admit_or_none
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.safety.url_redaction import redact_url
from crawlutron.storage.job_store import (
    CrawlJobState,
    FrontierEntry,
    JobStatus,
    JobStore,
    job_max_pages,
    normalize_job_state,
)
from crawlutron.transport.robots import FetchAndDecode
from crawlutron.vocabulary.limits import MAX_URL_TRANSIENT_ATTEMPTS
from crawlutron.vocabulary.taxonomy import is_transient_classification

logger = logging.getLogger(__name__)


BATCH_SIZE = 8


ExecuteCrawl = Callable[..., Awaitable[ProducerEnvelope]]


_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>]+")


def _scrubbed(error: str) -> str:
    """Every URL embedded in the text through the redaction chokepoint."""
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), error)


_execute_crawl: ExecuteCrawl = _real_execute_crawl


def install_execute_crawl(fn: ExecuteCrawl) -> None:
    """Swap the engine seam (tests only)."""
    global _execute_crawl
    _execute_crawl = fn


async def arecord_terminal_failure(job_store: JobStore, job_id: str, error: str) -> bool:
    """Flip a RUNNING job to FAILED with a scrubbed ``error`` in the same CAS
    write; True iff it landed; a past-RUNNING job is left as-is. The scrub routes
    through the #599 chokepoint (str(exc) embeds redirect-target credentials)."""
    job = await job_store.aload(job_id)
    if job is None or job.status is not JobStatus.RUNNING:
        return False
    return await job_store.aset_status(
        job_id, JobStatus.FAILED, expected=JobStatus.RUNNING, error=_scrubbed(error)
    )


def reset_execute_crawl() -> None:
    global _execute_crawl
    _execute_crawl = _real_execute_crawl


def pop_frontier(job: CrawlJobState) -> list[FrontierEntry]:
    normalize_job_state(job)
    # Normalization's drops are already logged by the merge, only the pops here need logging.
    max_pages = job_max_pages(job.policy, job.tier)
    if job.in_flight:
        # Cap arithmetic without the whole-set copy: the union's size is this sum.
        if max_pages > 0:
            fixed_count = len(job.visited) + sum(1 for entry in job.in_flight if entry.url not in job.visited)
            if fixed_count > max_pages:
                record_cap_overcommitted(job)
        return list(job.in_flight[:BATCH_SIZE])
    capacity = remaining_page_capacity(len(job.visited), max_pages)
    if capacity is not None and capacity <= 0:
        if job.frontier:
            record_cap_truncated(job)
        # The whole queue is dropped: log every entry so the persist deletes exactly their rows.
        job.frontier_removed.extend(entry.url for entry in job.frontier)
        del job.frontier[:]
        return []
    count = min(BATCH_SIZE, len(job.frontier))
    if capacity is not None:
        count = min(count, capacity)
    if capacity is not None and count == capacity and count < len(job.frontier):
        record_cap_truncated(job)
        job.frontier_removed.extend(entry.url for entry in job.frontier[count:])
        del job.frontier[count:]
    batch = list(job.frontier[:count])
    # The popped prefix's rows are deleted by the next persist.
    job.frontier_removed.extend(entry.url for entry in batch)
    job.frontier_mark = max(0, job.frontier_mark - count)
    del job.frontier[:count]
    return batch


def job_engine_payload(job: CrawlJobState, batch: Sequence[FrontierEntry]) -> CrawlRequest:
    """Build one non-recursive engine batch from a whole-site job.

    The runner owns the durable frontier; the engine is a single-page fetcher per
    batch. Two owners of one property was the defect, so engine recursion is off.
    """
    # The wire shape carries text in ``urls``; the entry's URL is admitted by construction.
    urls = [entry.url.raw for entry in batch]
    policy = read_policy(job.policy, job.tier)
    # Model_copy is safe because max_depth=0 is a declared value and this line
    # is ours, not a caller's: one engine batch is one page deep by definition.
    policy = policy.model_copy(update={"limits": policy.limits.model_copy(update={"max_depth": 0})})
    return CrawlRequest(urls=urls, policy=policy)


async def _seed_job_sitemaps(job: CrawlJobState, context: JobAdmissionContext) -> None:
    candidates = await fetch_job_sitemap_candidates(job.root_url, context)
    outcome = await admit_job_links(job, candidates, source_depth=-1, context=context)
    job.stats["total_discovered"] = job.stats.get("total_discovered", 0) + outcome.accepted


async def _load_and_claim(job_id: str, job_store: JobStore, *, preclaimed: bool) -> CrawlJobState | None:
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


async def _seed_sitemaps(job: CrawlJobState, context: JobAdmissionContext, job_store: JobStore) -> None:
    """Seed discovery sitemaps when the policy calls for them."""
    if not (context.max_depth > 0 and context.seed_sitemaps):
        return
    await _seed_job_sitemaps(job, context)
    await job_store.asave(job)


async def _entry_result(
    job: CrawlJobState,
    identity: NormalizedUrl,
    item: EngineCrawlResult | None,
    depth: int,
    context: JobAdmissionContext,
    known: set[NormalizedUrl],
) -> int:
    """Record one entry's result into ``job``; returns ``1`` if it failed.

    ``known`` is the batch's threaded occupied set: admission folds its
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
        logger.warning("link_admission_failed", extra={"url": redact_url(url_attr)})
        admitted = AdmissionOutcome()
    known |= admitted.known_delta  # unioning the delta copies nothing whole
    job.stats["total_discovered"] = job.stats.get("total_discovered", 0) + admitted.accepted
    return failed


async def _should_stop(
    job: CrawlJobState, job_id: str, job_store: JobStore, shutdown_event: asyncio.Event
) -> bool:
    """Whether the loop ends this iteration; a shutdown sets PAUSED first."""
    if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        return True
    if shutdown_event.is_set():
        await job_store.aset_status(job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)
        return True
    return False


async def _mark_complete_if_empty(
    job: CrawlJobState, job_id: str, job_store: JobStore, batch: Sequence[FrontierEntry]
) -> bool:
    """Mark the job COMPLETED when its frontier yielded nothing; else ``False``."""
    if batch:
        return False
    await job_store.asave(job)
    await job_store.aset_status(job_id, JobStatus.COMPLETED, expected=JobStatus.RUNNING)
    return True


async def absorb_batch(
    job: CrawlJobState,
    batch: Sequence[FrontierEntry],
    resolved: dict[NormalizedUrl, EngineCrawlResult],
    context: JobAdmissionContext,
    known: set[NormalizedUrl],
) -> int:
    """Fold one resolved batch into ``job`` under the threaded known set."""
    failed = 0
    for entry in batch:
        identity = entry.url
        job.in_flight = [pending for pending in job.in_flight if pending.url != identity]
        # The identity leaves in_flight here; the threaded set must forget it too,
        # or the requeue below would dedupe against its own URL.
        known.discard(identity)
        item = resolved.get(identity)
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
    # The batch's own counters fold here: one absorb, one save, one stats write.
    job.stats["total_crawled"] = len(job.visited)
    if failed:
        job.stats["total_failed"] = job.stats.get("total_failed", 0) + failed
    return failed


def _resolve_redirects(
    items: Sequence[EngineCrawlResult], resolved: dict[NormalizedUrl, EngineCrawlResult]
) -> None:
    """Fill the redirect gaps in ``resolved`` in place.

    A produced result must also answer its final and normalized identities;
    first admission wins, so ``setdefault`` never displaces the original."""
    for produced in items:
        for key in (produced.final_url, produced.normalized_url):
            if key is not None:
                resolved.setdefault(key, produced)


async def run_job(
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
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
        # The occupied set is threaded across the loop: one derivation here.
        known = admitted_page_identities(job, host_policy=context.host_policy)
        while True:
            # State-only reload: a full read re-decodes the page history per batch only to discard it.
            state = await job_store.areload_state(job_id)
            if state is None:
                return
            job = replace(state, results=job.results, visited=job.visited, visited_mark=job.visited_mark)
            if await _should_stop(job, job_id, job_store, shutdown_event):
                return
            batch = pop_frontier(job)
            if await _mark_complete_if_empty(job, job_id, job_store, batch):
                return
            if not job.in_flight:
                job.in_flight = list(batch)
            await job_store.asave(job)
            result = await _execute_crawl(
                job_engine_payload(job, batch), settings=settings, shutdown_event=shutdown_event
            )
            items = result["results"]
            reloaded = await job_store.areload_state(job_id)
            if reloaded is None:
                return
            job = replace(reloaded, results=job.results, visited=job.visited, visited_mark=job.visited_mark)
            # ``original_url`` wins the identity; redirects fill the gaps.
            resolved: dict[NormalizedUrl, EngineCrawlResult] = {}
            for produced in items:
                admitted_original = admit_or_none(produced.original_url, policy=job.host_policy)
                if admitted_original is not None:
                    resolved.setdefault(admitted_original, produced)
            _resolve_redirects(items, resolved)
            await absorb_batch(job, batch, resolved, context, known)
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


def reset_job_runner() -> None:
    """Best-effort synchronous teardown for test fixtures (no await needed).

    The registry part is ``cancel_all_tasks``; this also resets the
    execute-crawl seam this module owns.
    """
    cancel_all_tasks()
    reset_execute_crawl()


def start_job(
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    *,
    preclaimed: bool = False,
) -> asyncio.Task:
    """Spawn (or return) the runner task; preclaimed rows were flipped by caller.

    Lives here so the registry never imports back into this one.
    """
    return start_tracked_task(
        job_id, lambda: run_job(job_id, job_store, settings, shutdown_event, preclaimed=preclaimed)
    )


async def pause_job(job_id: str, job_store: JobStore) -> str:
    """Pause: flip the row; the task exits at the next batch boundary."""
    return await _transition(job_store, job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)


async def resume_job(
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    wedge_grace_seconds: float = 5.0,
) -> str:
    """Resume a paused job by respawning its runner task."""
    # A stale predecessor owning the slot must not leave a RUNNING row with no runner:
    outcome = await _transition(job_store, job_id, JobStatus.RUNNING, expected=JobStatus.PAUSED)
    if outcome == "ok":
        # The flip above is this process's claim, so the runner is told not to
        # take a second one -- the only way it can run without the CAS failing.
        old = tracked_task(job_id)
        if old is not None:
            try:
                await asyncio.wait_for(asyncio.shield(old), timeout=wedge_grace_seconds)
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
