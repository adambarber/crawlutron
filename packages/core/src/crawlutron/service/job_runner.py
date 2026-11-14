"""Background whole-site job runner for ``/v1/jobs``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace

from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl, admit_or_none
from safe_url.url_redaction import redact_url

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
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.service.crawl_executor import execute_crawl as _real_execute_crawl
from crawlutron.service.job_lifecycle import (
    cancel_all_tasks,
    cancel_job_task,
    is_running,
    shutdown_job_runner,
    start_tracked_task,
    tracked_task,
)
from crawlutron.service.job_recovery import (
    recover_and_start_pending_jobs,
    recover_orphaned_running_jobs,
)
from crawlutron.service.job_runner_lease import new_claim_token
from crawlutron.service.response_shaping import failed_unified_result
from crawlutron.service.unified_shaping import abuild_unified_result
from crawlutron.storage.job_store import (
    DEFAULT_JOB_CLAIM_SECONDS,
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
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), error)


_execute_crawl: ExecuteCrawl = _real_execute_crawl


def install_execute_crawl(fn: ExecuteCrawl) -> None:
    """Swap the engine seam (tests only)."""
    global _execute_crawl
    _execute_crawl = fn


async def arecord_terminal_failure(
    job_store: JobStore, job_id: str, error: str, *, claimed_by: str | None = None
) -> bool:
    """Flip a RUNNING job to FAILED; ownership-guarded when ``claimed_by`` is set."""
    scrubbed = _scrubbed(error)
    if claimed_by is not None:
        return await job_store.aset_status(
            job_id,
            JobStatus.FAILED,
            expected=JobStatus.RUNNING,
            error=scrubbed,
            claimed_by=claimed_by,
            require_owner=True,
        )
    return await job_store.aset_status(job_id, JobStatus.FAILED, expected=JobStatus.RUNNING, error=scrubbed)


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
    job.frontier_removed.extend(entry.url for entry in batch)
    job.frontier_mark = max(0, job.frontier_mark - count)
    del job.frontier[:count]
    return batch


def job_engine_payload(job: CrawlJobState, batch: Sequence[FrontierEntry]) -> CrawlRequest:
    """Build one non-recursive engine batch from a whole-site job."""
    urls = [entry.url.raw for entry in batch]
    policy = read_policy(job.policy, job.tier)
    policy = policy.model_copy(update={"limits": policy.limits.model_copy(update={"max_depth": 0})})
    return CrawlRequest(urls=urls, policy=policy)


async def _seed_job_sitemaps(job: CrawlJobState, context: JobAdmissionContext) -> None:
    candidates = await fetch_job_sitemap_candidates(job.root_url, context)
    outcome = await admit_job_links(job, candidates, source_depth=-1, context=context)
    job.stats["total_discovered"] = job.stats.get("total_discovered", 0) + outcome.accepted


async def _load_and_claim(
    job_id: str, job_store: JobStore, *, preclaimed: bool, claim_token: str, claim_seconds: float
) -> CrawlJobState | None:
    """Load and claim the job, or ``None`` when it cannot be owned."""
    job = await job_store.aload(job_id)
    if job is None:
        return None
    now = time.time()
    if preclaimed:
        if not await job_store.arenew_job_claim(job_id, claim_token, now=now):
            return None
    elif not await job_store.aset_status(
        job_id,
        JobStatus.RUNNING,
        expected=JobStatus.PENDING,
        claimed_by=claim_token,
        claim_now=now,
        claim_seconds=claim_seconds,
    ):
        return None
    return replace(job, status=JobStatus.RUNNING, claimed_by=claim_token, claimed_at=now)


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
        logger.warning("link_admission_failed", extra={"url": redact_url(url_attr)})
        admitted = AdmissionOutcome()
    known |= admitted.known_delta  # unioning the delta copies nothing whole
    job.stats["total_discovered"] = job.stats.get("total_discovered", 0) + admitted.accepted
    return failed


async def _should_stop(
    job: CrawlJobState, job_id: str, job_store: JobStore, shutdown_event: asyncio.Event, *, claim_token: str
) -> bool:
    if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        return True
    if shutdown_event.is_set():
        await job_store.asave(job)
        await job_store.aset_status(
            job_id,
            JobStatus.PAUSED,
            expected=JobStatus.RUNNING,
            claimed_by=claim_token,
            require_owner=True,
            clear_claim=True,
        )
        return True
    return False


async def _mark_complete_if_empty(
    job: CrawlJobState, job_id: str, job_store: JobStore, batch: Sequence[FrontierEntry], *, claim_token: str
) -> bool:
    """Mark COMPLETED when the frontier yielded nothing; else ``False``."""
    if batch:
        return False
    await job_store.asave(job)
    await job_store.aset_status(
        job_id,
        JobStatus.COMPLETED,
        expected=JobStatus.RUNNING,
        claimed_by=claim_token,
        require_owner=True,
        clear_claim=True,
    )
    return True


async def absorb_batch(
    job: CrawlJobState,
    batch: Sequence[FrontierEntry],
    resolved: dict[NormalizedUrl, EngineCrawlResult],
    context: JobAdmissionContext,
    known: set[NormalizedUrl],
) -> int:
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
    job.stats["total_crawled"] = len(job.visited)
    if failed:
        job.stats["total_failed"] = job.stats.get("total_failed", 0) + failed
    return failed


def _resolve_redirects(
    items: Sequence[EngineCrawlResult], resolved: dict[NormalizedUrl, EngineCrawlResult]
) -> None:
    for produced in items:
        for key in (produced.final_url, produced.normalized_url):
            if key is not None:
                resolved.setdefault(key, produced)


def _resolved_items(
    items: Sequence[EngineCrawlResult], host_policy
) -> dict[NormalizedUrl, EngineCrawlResult]:
    resolved: dict[NormalizedUrl, EngineCrawlResult] = {}
    for produced in items:
        admitted = admit_or_none(produced.original_url, policy=host_policy)
        if admitted is not None:
            resolved.setdefault(admitted, produced)
    _resolve_redirects(items, resolved)
    return resolved


async def _run_claimed_loop(
    job,
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    context,
    token: str,
) -> None:
    known = admitted_page_identities(job, host_policy=context.host_policy)
    while True:
        if not await job_store.arenew_job_claim(job_id, token, now=time.time()):
            return
        state = await job_store.areload_state(job_id)
        if state is None:
            return
        job = replace(state, results=job.results, visited=job.visited, visited_mark=job.visited_mark)
        if await _should_stop(job, job_id, job_store, shutdown_event, claim_token=token):
            return
        batch = pop_frontier(job)
        if await _mark_complete_if_empty(job, job_id, job_store, batch, claim_token=token):
            return
        if not job.in_flight:
            job.in_flight = list(batch)
        if not await job_store.arenew_job_claim(job_id, token, now=time.time()):
            return
        await job_store.asave(job)
        result = await _execute_crawl(
            job_engine_payload(job, batch), settings=settings, shutdown_event=shutdown_event
        )
        still_owned = await job_store.arenew_job_claim(job_id, token, now=time.time())
        items = result["results"]
        reloaded = await job_store.areload_state(job_id)
        if reloaded is None or reloaded.claimed_by != token:
            return
        job = replace(reloaded, results=job.results, visited=job.visited, visited_mark=job.visited_mark)
        resolved = _resolved_items(items, job.host_policy)
        if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
            if job.in_flight:
                await absorb_batch(job, batch, resolved, context, known)
                await job_store.asave(job)
            return
        if not still_owned or job.status is not JobStatus.RUNNING:
            return
        await absorb_batch(job, batch, resolved, context, known)
        if not await job_store.arenew_job_claim(job_id, token, now=time.time()):
            return
        await job_store.asave(job)


async def run_job(
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    fetch_and_decode: FetchAndDecode | None = None,
    *,
    preclaimed: bool = False,
    claim_token: str | None = None,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
) -> None:
    """Whole-site engine loop for one job, stamped/renewed under a DB lease."""
    token = claim_token or new_claim_token(job_id)
    job = await _load_and_claim(
        job_id, job_store, preclaimed=preclaimed, claim_token=token, claim_seconds=claim_seconds
    )
    if job is None:
        return
    try:
        context = JobAdmissionContext.create(job.policy, job.tier, settings, fetch_and_decode)
        _ = HostPolicy
        if context.max_depth > 0 and context.seed_sitemaps:
            await _seed_job_sitemaps(job, context)
            await job_store.asave(job)
        await _run_claimed_loop(job, job_id, job_store, settings, shutdown_event, context, token)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("job_runner_failed job_id=%s error=%r", job_id, exc)
        await arecord_terminal_failure(job_store, job_id, str(exc), claimed_by=token)


async def _transition(
    job_store: JobStore, job_id: str, to: JobStatus, *, expected: JobStatus | frozenset[JobStatus]
) -> str:
    """Flip status: applied, missing, or conflict."""
    if await job_store.aset_status(job_id, to, expected=expected):
        return "ok"
    return "missing" if await job_store.aload(job_id) is None else "conflict"


def reset_job_runner() -> None:
    cancel_all_tasks()
    reset_execute_crawl()


def start_job(
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    *,
    preclaimed: bool = False,
    claim_token: str | None = None,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
) -> asyncio.Task:
    """Spawn (or return) the runner task; preclaimed rows were flipped by caller.

    Lives here so the registry never imports back into this one.
    """
    token = claim_token or new_claim_token(job_id)
    return start_tracked_task(
        job_id,
        lambda: run_job(
            job_id,
            job_store,
            settings,
            shutdown_event,
            preclaimed=preclaimed,
            claim_token=token,
            claim_seconds=claim_seconds,
        ),
    )


async def pause_job(job_id: str, job_store: JobStore) -> str:
    return await _transition(job_store, job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)


async def resume_job(
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    wedge_grace_seconds: float = 5.0,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
) -> str:
    """Resume a paused job by respawning its runner task."""
    token = new_claim_token(job_id)
    outcome = "conflict"
    if await job_store.aset_status(
        job_id,
        JobStatus.RUNNING,
        expected=JobStatus.PAUSED,
        claimed_by=token,
        claim_now=time.time(),
        claim_seconds=claim_seconds,
    ):
        outcome = "ok"
    elif await job_store.aload(job_id) is None:
        outcome = "missing"
    if outcome == "ok":
        old = tracked_task(job_id)
        if old is not None:
            try:
                await asyncio.wait_for(asyncio.shield(old), timeout=wedge_grace_seconds)
            except TimeoutError:
                old.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await old
        start_job(
            job_id,
            job_store,
            settings,
            shutdown_event,
            preclaimed=True,
            claim_token=token,
            claim_seconds=claim_seconds,
        )
    return outcome


async def cancel_job(job_id: str, job_store: JobStore) -> str:
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
