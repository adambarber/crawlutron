"""Background whole-site job runner for ``/v1/jobs``.

Consumes the persisted frontier of a whole-site job in persistent batches:
every transition (pause, resume, cancel, crash) leaves the store in a
state that can be resumed later. Batches call the shared
:func:`app.api.crawl_executor.execute_crawl` (seam: ``install_execute_crawl``
for tests), so jobs run through the exact same engine as ``/v1/crawl``.

Design notes
------------
* One :class:`asyncio.Task` per running job, tracked in a module registry.
* ``pause`` flips the store row to PAUSED; the task sees it between batches
  and exits without touching the row (frontier/visited retained).
* ``cancel`` flips to CANCELLED; the task also drops the in-memory task
  handle. Visited/frontier state is retained for auditability.
* ``delete`` on a running job cancels its task before removing the row.
* Crash recovery: :func:`recover_orphaned_running_jobs` rewinds RUNNING
  rows to PENDING at startup; lifespan then auto-starts PENDING jobs.

The registry accessors are async-safe: control transitions happen in the
request handlers (store writes are lock-guarded in JobStore), while the
runner task only transitions between batches.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any
from urllib.parse import urlparse

from app.api.crawl_executor import execute_crawl as _real_execute_crawl
from app.api.response_shaping import _failed_unified_result, abuild_unified_result
from app.constants import (
    MAX_URL_TRANSIENT_ATTEMPTS,
    is_transient_classification,
    status_for_classification,
)
from app.crawl.admission import (
    JobAdmissionContext,
    admit_job_links,
    canonical_page_identity,
    fetch_job_sitemap_candidates,
    job_max_pages,
    record_cap_overcommitted,
    record_cap_truncated,
    remaining_page_capacity,
    requeue_admitted_pages,
)
from app.crawl.job_store import FrontierEntry, JobKind, JobStatus, JobStore, normalize_job_state
from app.crawl.policy import read_policy
from app.models import Classification, CrawlResult, CrawlTask

logger = logging.getLogger(__name__)

# Frontier batch size per engine call. Bounds per-call result memory the
# same way the webhook runner bounds whole-crawl memory.
BATCH_SIZE = 8

ExecuteCrawl = Callable[..., Awaitable[dict[str, Any]]]
_execute_crawl: ExecuteCrawl = _real_execute_crawl


def install_execute_crawl(fn: ExecuteCrawl) -> None:
    """Swap the engine seam (tests only)."""
    global _execute_crawl
    _execute_crawl = fn


async def arecord_terminal_failure(job_store: JobStore, job_id: str, error: str) -> None:
    """Stamp ``error`` on a running job and flip it to FAILED.

    A job that has already left RUNNING -- cancelled, paused, finished -- is
    left exactly as it is. Recording the failure anyway is not a cosmetic
    inconsistency: a cancel that lands while a batch is in flight would find
    its own error message overwritten by the batch's, so the job reports
    *why it failed* when the operator asked for it to stop.

    Written twice, and the two copies had already disagreed about this: the
    whole-site runner re-checked the status before writing and the async
    webhook runner did not, so the same cancel was honoured on the sync path
    and silently overwritten on the async one. The status guard belongs to the
    rule rather than to whichever caller remembered it, so it is here.
    """
    job = await job_store.aload(job_id)
    if job is None or job.status is not JobStatus.RUNNING:
        return
    job.error = error
    await job_store.asave(job)
    await job_store.aset_status(job_id, JobStatus.FAILED, expected=JobStatus.RUNNING)


def reset_execute_crawl() -> None:
    global _execute_crawl
    _execute_crawl = _real_execute_crawl


_tasks: dict[str, asyncio.Task] = {}


def is_running(job_id: str) -> bool:
    task = _tasks.get(job_id)
    return task is not None and not task.done()


async def shutdown_job_runner() -> None:
    """Cancel all running job tasks and await their exit."""
    for task in list(_tasks.values()):
        task.cancel()
    for job_id, task in list(_tasks.items()):
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("job_runner_shutdown_task_error", extra={"job_id": job_id})
    _tasks.clear()


def reset_job_runner() -> None:
    """Best-effort synchronous teardown for test fixtures (no await needed)."""
    for task in list(_tasks.values()):
        task.cancel()
    _tasks.clear()
    reset_execute_crawl()


def _is_resumable_kind(kind: str | None) -> bool:
    """Whether a job of this kind can be resumed from its persisted frontier.

    One definition, read by both recovery seams, because the two used to
    disagree: the rewind accepted every kind while the startup scan resumed
    only whole-site jobs. A single-shot job killed mid-flight was therefore
    marked recoverable and then never recovered -- PENDING indefinitely, and a
    ``202 "pending"`` to every client that polled for it, from an endpoint
    that is otherwise authoritative.

    A seam that cannot resume must not rewind. It has to retire the row.
    """
    return kind in (None, JobKind.WHOLE_SITE.value)


async def recover_orphaned_running_jobs(job_store: JobStore) -> int:
    """Retire crash orphans: rewind the resumable, fail the rest.

    Async: the lifespan calls it with a live loop, and it reads every job row.
    """
    recovered = 0
    for summary in await job_store.alist_jobs():
        if summary.get("status") == JobStatus.RUNNING.value:
            # A job that cannot be resumed is given a terminal status here,
            # while this is still the only place that knows the service has
            # given up on it. Rewinding it instead would hand the next startup
            # a row it has already decided to skip.
            retired = JobStatus.PENDING if _is_resumable_kind(summary.get("kind")) else JobStatus.FAILED
            # Control-plane CAS: retires the row without rewriting its data;
            # save() no longer carries status anyway.
            if await job_store.aset_status(summary["job_id"], retired, expected=JobStatus.RUNNING):
                recovered += 1
    return recovered


async def recover_and_start_pending_jobs(
    job_store: JobStore,
    settings: Any,
    shutdown_event: asyncio.Event,
) -> int:
    """Lifespan startup: retire crash orphans, then run every PENDING job.

    Whole-site jobs resume from their persisted frontier; returns the number
    of runner tasks started. Jobs of any other kind reach the scan already
    terminal -- `recover_orphaned_running_jobs` fails the orphans it cannot
    resume -- so the kind check here is a guard against a row seeded
    straight into PENDING, not the orphan path.
    """
    await recover_orphaned_running_jobs(job_store)
    started = 0
    for summary in await job_store.alist_jobs():
        if summary.get("status") != JobStatus.PENDING.value:
            continue
        if not _is_resumable_kind(summary.get("kind")):
            continue
        job = await job_store.aload(summary["job_id"])
        if job is None:
            continue
        start_job(job.job_id, job_store, settings, shutdown_event)
        started += 1
    return started


def _pop_frontier(job: Any) -> list[FrontierEntry]:
    normalize_job_state(job)
    max_pages = job_max_pages(job.policy, job.tier)
    if job.in_flight:
        fixed = {
            canonical_page_identity(url, host_policy=job.host_policy) or url
            for url in set(job.visited) | {entry.url for entry in job.in_flight}
        }
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


def _job_engine_payload(job: Any, batch: Sequence[FrontierEntry]) -> dict[str, Any]:
    """Build one non-recursive engine batch from a whole-site job.

    The runner owns the durable frontier; the engine is a single-page fetcher per
    batch. Two owners of one property was the defect, so engine recursion is off.
    """
    urls = [entry.url for entry in batch]
    policy = read_policy(job.policy, job.tier)
    # Model_copy is safe here specifically because max_depth=0 is a value the
    # model declares and this line is ours, not a caller's: a single engine
    # batch is one page deep by definition.
    policy = policy.model_copy(update={"limits": policy.limits.model_copy(update={"max_depth": 0})})
    return {
        "urls": urls,
        "policy": policy,
        "url": urls[0],
        "headers": {},
        "request_profile": {},
    }


async def _pace_delayed_batch(
    context: JobAdmissionContext,
    batch: Sequence[FrontierEntry],
) -> None:
    tasks: list[CrawlTask] = []
    for index, entry in enumerate(batch):
        host = (urlparse(entry.url).hostname or "").lower()
        identity = canonical_page_identity(entry.url, host_policy=context.host_policy)
        if host in context.delayed_hosts and identity is not None:
            tasks.append(
                CrawlTask(
                    index=index,
                    original_url=entry.url,
                    normalized_url=identity,
                    domain=host,
                )
            )
    if not tasks:
        return

    def pace_result(job: CrawlTask) -> CrawlResult:
        # The status is derived from the label rather than spelled beside it.
        # A paced task never carries a payload, so this is the one result whose
        # (classification, status) pair could sit here forever as two literals
        # -- correct today, and silently wrong the day the taxonomy moves a
        # default, with nothing in the suite able to see it. Asking the table is
        # the same one-line change that keeps it true.
        classification: Classification = "rendered"
        return CrawlResult(
            url=job.original_url,
            status=status_for_classification(classification),
            classification=classification,
            final_url=job.original_url,
            html=None,
            text=None,
            attempts=0,
            elapsed_ms=0,
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


async def _run_job(
    job_id: str,
    job_store: JobStore,
    settings: Any,
    shutdown_event: asyncio.Event,
) -> None:
    """Whole-site engine loop for one job. Runs until terminal/paused/cancel."""
    job = await job_store.aload(job_id)
    if job is None:
        return
    claimed = await job_store.aset_status(job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    if not claimed and job.status is not JobStatus.RUNNING:
        return
    await job_store.asave(job)

    try:
        context = JobAdmissionContext.create(job.policy, job.tier, settings)
        if context.max_depth > 0 and context.seed_sitemaps:
            await _seed_job_sitemaps(job, context)
            await job_store.asave(job)

        while True:
            job = await job_store.aload(job_id)
            if job is None:
                return
            if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
                return
            if shutdown_event.is_set():
                await job_store.aset_status(job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)
                return

            batch = _pop_frontier(job)
            if not batch:
                await job_store.asave(job)
                await job_store.aset_status(job_id, JobStatus.COMPLETED, expected=JobStatus.RUNNING)
                return
            if not job.in_flight:
                job.in_flight = list(batch)
            await job_store.asave(job)
            await _pace_delayed_batch(context, batch)

            result = await _execute_crawl(
                _job_engine_payload(job, batch),
                settings=settings,
                shutdown_event=shutdown_event,
            )
            items = result.get("results", [])
            job = await job_store.aload(job_id)
            if job is None:
                return

            # Index results by every URL they are known by, in TWO passes with
            # different priority. A result's own `original_url` is what the
            # caller submitted and is therefore authoritative for that entry;
            # redirect aliases (final_url/normalized_url) only fill identities
            # no result claimed for itself. A single first-writer-wins pass made
            # a redirect steal another entry's slot: with batch [a, b] where `a`
            # redirects to `b/`, the redirect result claimed b's identity first
            # and b's real result was never recorded.
            resolved: dict[str, dict[str, Any]] = {}
            for item in items:
                original = item.get("original_url")
                if isinstance(original, str) and original:
                    resolved.setdefault(
                        canonical_page_identity(original, host_policy=context.host_policy) or original,
                        item,
                    )
            for item in items:
                for key in (item.get("final_url"), item.get("normalized_url")):
                    if not isinstance(key, str) or not key:
                        continue
                    identity = canonical_page_identity(key, host_policy=context.host_policy) or key
                    resolved.setdefault(identity, item)

            failed = 0
            for entry in batch:
                url = entry.url
                identity = canonical_page_identity(url, host_policy=context.host_policy) or url
                job.in_flight = [
                    pending
                    for pending in job.in_flight
                    if (canonical_page_identity(pending.url, host_policy=context.host_policy) or pending.url)
                    != identity
                ]
                item = resolved.get(identity)
                if item is None:
                    outcome = "transient"
                elif item.get("status") != "failed":
                    outcome = "success"
                elif is_transient_classification(item.get("classification")):
                    outcome = "transient"
                else:
                    outcome = "terminal_failed"

                if (
                    outcome == "transient"
                    and job.transient_retries.get(identity, 0) + 1 < MAX_URL_TRANSIENT_ATTEMPTS
                ):
                    job.transient_retries[identity] = job.transient_retries.get(identity, 0) + 1
                    requeue_admitted_pages(
                        job,
                        (FrontierEntry(url=identity, depth=entry.depth),),
                        context,
                    )
                    continue

                job.transient_retries.pop(identity, None)
                job.visited.add(identity)
                if item is None:
                    # The engine's batch came back without answering for this URL.
                    # It is already in `visited`, so counting it and moving on left
                    # the job's results unable to account for it -- total_crawled
                    # said 2, one result existed, and nothing named the gap. The
                    # batch's own shaping path already builds this envelope
                    # (its docstring says it mirrors this runner's semantics,
                    # which is the coupling that let the two drift), so the
                    # runner uses it rather than inventing a second shape.
                    job.results.append(
                        _failed_unified_result({"normalized_url": identity, "original_url": url}).model_dump()
                    )
                    failed += 1
                    continue
                if outcome == "transient":
                    item = {**item, "status": "failed", "error": "retries_exhausted"}
                try:
                    unified = await abuild_unified_result(item)
                except Exception:
                    # Shaping is the batch's last line of defense and is not
                    # meant to raise; if it does, this item is still this URL's
                    # result and the job must still name it. Shaping degrades a
                    # malformed item to this same terminal failure, so the two
                    # paths now agree instead of one producing nothing.
                    unified = _failed_unified_result(item)
                job.results.append(unified.model_dump())
                if item.get("status") == "failed":
                    failed += 1
                admitted = await admit_job_links(
                    job,
                    item.get("discovered_links") or [],
                    source_depth=entry.depth,
                    context=context,
                )
                job.stats["total_discovered"] = job.stats.get("total_discovered", 0) + admitted.accepted
            job.stats["total_crawled"] = len(job.visited)
            if failed:
                job.stats["total_failed"] = job.stats.get("total_failed", 0) + failed
            await job_store.asave(job)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("job_runner_failed job_id=%s error=%r", job_id, exc)
        await arecord_terminal_failure(job_store, job_id, str(exc))


def start_tracked_task(
    job_id: str,
    factory: Callable[[], Coroutine[Any, Any, Any]],
) -> asyncio.Task:
    """Spawn ``factory()`` as the task that owns ``job_id``, or return the live one.

    One registry means one shutdown drain and one cancel path. The factory is
    called only once a slot is free, so a job that is already running does not
    leave a coroutine constructed and never awaited.

    Taking a factory rather than a coroutine is what lets the async crawl come
    through here too. Handed to a Starlette background task instead, it was
    awaited inside the request cycle -- so the 202 did not return until the
    crawl finished, and the drain in ``app/main.py`` could not see it either.
    """
    existing = _tasks.get(job_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(factory())
    _tasks[job_id] = task
    task.add_done_callback(lambda t: _tasks.pop(job_id, None))
    return task


def start_job(
    job_id: str,
    job_store: JobStore,
    settings: Any,
    shutdown_event: asyncio.Event,
) -> asyncio.Task:
    """Spawn (or return) the runner task for a job."""
    return start_tracked_task(job_id, lambda: _run_job(job_id, job_store, settings, shutdown_event))


def cancel_job_task(job_id: str) -> None:
    """Cancel a running job task (delete path); state row untouched."""
    task = _tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()


async def _transition(
    job_store: JobStore,
    job_id: str,
    to: JobStatus,
    *,
    expected: JobStatus | frozenset[JobStatus],
) -> str:
    """Flip a job's status and say why if the flip did not happen.

    Every status-changing control needs the same three-way answer -- applied,
    no such job, or a job in a state it cannot leave -- and needs it to mean
    the same thing everywhere. It was spelled per control instead, and the
    copies had already diverged: ``resume_job`` read it one way (triage first,
    then act) and ``pause_job``/``cancel_job`` another (act, then triage),
    which is the shape of a bug waiting for the next state transition added to
    only one of them. Here the answer is produced once, so a fourth control
    cannot get it wrong.

    ``missing`` is distinguished from ``conflict`` by re-reading the row
    rather than by the store's refusal, because the two refusals look the same
    to a caller otherwise: a typo'd id and a job already cancelled are both
    ``aset_status`` returning False.
    """
    if await job_store.aset_status(job_id, to, expected=expected):
        return "ok"
    return "missing" if await job_store.aload(job_id) is None else "conflict"


async def pause_job(job_id: str, job_store: JobStore) -> str:
    """Pause: flip the row; the task exits at the next batch boundary."""
    return await _transition(job_store, job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)


async def resume_job(job_id: str, job_store: JobStore, settings: Any, shutdown_event: asyncio.Event) -> str:
    """Resume a paused job by respawning its runner task."""
    outcome = await _transition(job_store, job_id, JobStatus.RUNNING, expected=JobStatus.PAUSED)
    if outcome == "ok":
        start_job(job_id, job_store, settings, shutdown_event)
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
    "reset_job_runner",
    "resume_job",
    "shutdown_job_runner",
    "start_job",
]
