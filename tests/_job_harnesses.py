"""Job-runner test harness: stores, fake engine seams, and the job-state waits.

The waits at the bottom are the reason this module exists in its current form.
They used to be a poll loop racing a 5.0s wall-clock deadline:

    while loop.time() < deadline:
        if <the status I asked for>:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(...)

That deadline was a starvation escape hatch wearing a deadline's clothes. It
surfaced as one red run of
``tests/api/test_jobs_execution.py::TestTransientFailureRequeue::test_pause_non_
running_conflicts`` on a box running four agent sessions, where the process took
62.9s of user CPU inside a 271.5s run (23% of one core) -- a job that was
progressing perfectly well simply ran out of seconds. The same test passed 3/3
in isolation (0.4s), its whole file passed 53/53, and three other full runs of
the commit were green, which is the signature of a scheduling failure and not a
defect. Raising the number would have hidden that without fixing it.

The fix is the instrument, not the number. ``JobStore.save()`` deliberately
never writes ``status`` -- control state moves exclusively through
:meth:`JobStore.set_status` -- so that single compare-and-set is the COMPLETE
set of edges a job-state wait has to observe. Subscribing to it turns the poll
into an edge-triggered wait: the wait returns the instant the status is
committed, however long ago that was relative to now, so a starved-but-progressing
suite is no longer raced. ``save`` is published on too, because
:func:`_wait_until` takes arbitrary predicates over the whole row.
"""

import asyncio
from collections.abc import Collection

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.admission.policy import PolicyLike
from app.models.crawl_result import EngineCrawlResult
from app.models.normalized_url import NormalizedUrl
from app.models.result_record import ProducerEnvelope
from app.models.taxonomy import Classification
from app.storage.job_store import TERMINAL_STATUSES, JobStatus, JobStore
from tests._job_drivers import (
    _create_api_job,
    _seed_job,
    assert_settled,
    drive_job,
    job_limits,
    post_api_job,
    post_render_with_pool,
    raw_job_row,
    recording_execute,
    run_api_job,
    run_scripted_api_job,
    run_seeded_job,
    run_seeded_scripted_job,
    run_to_terminal,
    submit_blocked_job,
    write_row_policy,
)
from tests._job_store_doubles import (
    _StoreListener,
    frontier,
    loaded_job,
    make_crawled_job_store,
    make_job_store,
    single_shot_job,
    single_shot_orphan,
)
from tests._policy_factories import canonical
from tests._result_builders import _canned_result

#: Hang guard for the store waits. Deliberately NOT ``HANG_GUARD_SECONDS``: that one guards waits for a task
#: the SAME loop has already scheduled, and such a task either runs on the next turn or never.
STORE_HANG_GUARD_SECONDS = 30.0


#: The statuses a job settles in, as the store spells them.
_SETTLED_STATUSES = frozenset(status.value for status in TERMINAL_STATUSES)


_JOINABLE_STATUSES = _SETTLED_STATUSES - {JobStatus.CANCELLED.value}


async def run_job_control_test(
    app: FastAPI,
    job_store: JobStore,
    job_id: str,
    action: str,
    expected_status: int = 422,
) -> None:

    async with ASGITransport(app=app) as transport:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": action})
            assert resp.status_code == expected_status


async def _execution_urls(payload_dict, hang: asyncio.Event | None):
    if hang is not None and not hang.is_set():
        await hang.wait()
    urls = payload_dict.get("urls") or [payload_dict.get("url")]
    return [canonical(url) for url in urls]


def _execution_payload(results: list[EngineCrawlResult], submitted_count: int) -> ProducerEnvelope:
    return {
        "request_id": "test",
        "results": results,
        "submitted_count": submitted_count,
        "policy_schema_version": "test",
    }


class ScriptedExecute:
    """The engine seam, faked, owning the per-URL attempt count.

    Four runner wrappers used to hand back ``(job, attempts)`` because the
    counter was declared by the test and threaded down through each of them. But
    the count is what separates "fetched once" from "fetched twice and still
    produced a result", so it belongs to the thing that does the fetching, not to
    the caller that asks -- and once it lived here, the four wrappers turned out
    to be the same wrapper.

    Construct from exactly one of:

    ``scripts``  per-URL response sequences. A URL with more than one entry is
                 served from the front and popped, which is how transient-failure
                 requeue is driven. An unscripted URL renders successfully.
    ``results``  a flat list of responses, keyed by ``original_url``. An unscripted
                 URL fails, because a fixed answer set means the runner asked for
                 something the fixture never described.
    """

    def __init__(
        self,
        scripts: dict[str, list[EngineCrawlResult]] | None = None,
        *,
        results: list[EngineCrawlResult] | None = None,
        hang: asyncio.Event | None = None,
    ):
        if (scripts is None) == (results is None):
            raise AssertionError("ScriptedExecute takes exactly one of scripts= or results=")
        # Keys are URL identities: caller-spelled text is admitted here (so a key like "http://example.test"
        if scripts is not None:
            self._scripts = {canonical(url).raw: list(seq) for url, seq in scripts.items()}
        else:
            self._scripts = {r.original_url: [r] for r in results or []}
        self._default: Classification = "rendered" if scripts is not None else "site_error"
        self._hang = hang
        self.attempts: dict[str, int] = {}

    def _entry(self, url: NormalizedUrl) -> EngineCrawlResult:
        sequence = self._scripts.get(url.raw)
        if sequence is None:
            return _canned_result(url, classification=self._default)
        return sequence.pop(0) if len(sequence) > 1 else sequence[0]

    async def __call__(self, payload_dict, *, settings, shutdown_event):
        urls = await _execution_urls(payload_dict, self._hang)
        for url in urls:
            self.attempts[url.raw] = self.attempts.get(url.raw, 0) + 1
        return _execution_payload([self._entry(url) for url in urls], len(urls))


def _make_app(job_store):
    from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
    from app.api.jobs_router import router as jobs_router
    from app.config.settings import CrawlerSettings

    app = FastAPI()
    app.include_router(jobs_router)
    shutdown = asyncio.Event()
    app.dependency_overrides[get_job_store] = lambda: job_store
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
    app.dependency_overrides[get_shutdown_event] = lambda: shutdown
    return app


async def _join_runner_task(job_id: str) -> bool:
    """Await the runner task for ``job_id`` if one is registered and live.

    For a terminal wait this is the exact event the caller is waiting on:
    ``run_job`` returns when it has finished, and it does not return at a time
    of our choosing. A starved box *delays* that task; it does not end it early,
    so a task join is immune to the whole class of failure this replaced --
    there is no deadline in this path to race.

    ``asyncio.wait`` rather than ``await``: it resolves when the task finishes
    however it finishes (returned, raised, cancelled) without re-raising any of
    those into the waiter, and it still propagates the waiter's own
    cancellation. The row is the record of what happened; the task's exception
    is not the caller's to handle.

    Returns True when a task was joined, so the caller knows the state may have
    moved without an edge having been observed.
    """
    from app.api import job_lifecycle

    task = job_lifecycle._tasks.get(job_id)
    if task is None or task.done():
        return False
    await asyncio.wait({task})
    return True


async def _next_store_edge(store: JobStore, job_id: str, listener: _StoreListener) -> None:
    """Block until the next write to ``job_id``, or fail naming what never came.

    The bound here is a HANG GUARD, not an instrument, and the difference is
    load-bearing: the wait is edge-triggered, so a slow job takes longer to
    reach its next write instead of failing. The guard only fires when NOTHING
    is written for this long.

    Sized off measurement rather than off the old 5.0s: across a full
    ``pytest -q`` sweep the slowest of the 68 edge-driven waits in the suite
    took 1.6ms. The old number was not a hang guard, though -- a synthetic 6.5s
    stall in the runner's worker thread (longer than 5.0s) reproduced the exact
    original failure against a 5.0s guard, which is why the terminal path joins
    the task instead and this bound is a store-level wedge detector at 30s.
    """
    listener.event.clear()
    try:
        await asyncio.wait_for(listener.event.wait(), STORE_HANG_GUARD_SECONDS)
    except TimeoutError as exc:
        raise AssertionError(
            f"no write reached job {job_id} for {STORE_HANG_GUARD_SECONDS}s. The wait is "
            "edge-triggered, so nothing elapsed: the store was not written to at all, "
            f"which is a wedge rather than slow progress. Last state: {store.load(job_id)}"
        ) from exc


async def _wait_for(job_store, job_id: str, satisfied, *, join_task: bool):
    """Block until ``satisfied(job)`` holds; return that job.

    Subscribe first, then read, then wait -- in that order, and the read comes
    before the wait because the state can already be what the caller asked for
    (a caller that drove ``run_job`` in-line reaches this only once the job is
    finished). Clearing the event after the read is what makes the wait
    lossless: a write that lands after the clear has its ``call_soon_threadsafe``
    callback queued, so the very next suspension runs it and the wait returns; a
    write that landed before the clear is visible to a read that follows it.
    """
    bus = getattr(job_store, "status_bus", None)
    if bus is None:
        raise AssertionError(
            "this job store has no status bus: build it with make_job_store() so "
            "job-state waits are edge-triggered instead of polling a deadline"
        )
    listener = bus.subscribe(job_id)
    try:
        while True:
            job = job_store.load(job_id)
            if job is not None and satisfied(job):
                return job
            if join_task and await _join_runner_task(job_id):
                continue  # the row may have moved; re-read before waiting again
            await _next_store_edge(job_store, job_id, listener)
    finally:
        listener.close()


async def _wait_status(job_store, job_id: str, statuses: Collection[str]) -> str:
    """Block until ``job_id`` holds one of ``statuses``; return the status name.

    When the caller is waiting for a terminal status there is a live runner task
    to join, and that is the preferred path: it carries no deadline at all. A
    predicate over an intermediate state has no such task, so
    :func:`_wait_until` falls back to the store-level hang guard alone.
    """
    job = await _wait_for(
        job_store,
        job_id,
        lambda row: row.status.value in statuses,
        join_task=bool(statuses) and frozenset(statuses) <= _JOINABLE_STATUSES,
    )
    return job.status.value


async def _wait_until(job_store, job_id: str, predicate):
    """Block until ``predicate(job)`` holds; return that job.

    The predicate ranges over the whole row (frontier length, stat counters), so
    ``save`` is the edge source rather than the status CAS alone.
    """
    return await _wait_for(job_store, job_id, predicate, join_task=False)


def _execution_policy(policy: PolicyLike | None) -> PolicyLike:
    effective = dict(policy or {})
    limits = dict(effective.get("limits") or {})
    limits.setdefault("max_depth", 1)
    limits.setdefault("max_pages", 0)
    limits.setdefault("respect_robots", False)
    effective["limits"] = limits
    discovery = dict(effective.get("discovery") or {})
    discovery.setdefault("sitemaps", False)
    effective["discovery"] = discovery
    return effective


__all__ = [
    "ScriptedExecute",
    "_create_api_job",
    "_execution_payload",
    "_execution_policy",
    "_execution_urls",
    "_make_app",
    "_seed_job",
    "_wait_status",
    "_wait_until",
    "assert_settled",
    "drive_job",
    "frontier",
    "job_limits",
    "loaded_job",
    "make_crawled_job_store",
    "make_job_store",
    "post_api_job",
    "post_render_with_pool",
    "raw_job_row",
    "recording_execute",
    "run_api_job",
    "run_job_control_test",
    "run_scripted_api_job",
    "run_seeded_job",
    "run_seeded_scripted_job",
    "run_to_terminal",
    "single_shot_job",
    "single_shot_orphan",
    "submit_blocked_job",
    "write_row_policy",
]
