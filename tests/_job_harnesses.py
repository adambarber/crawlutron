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
never writes ``status`` (H3) -- control state moves exclusively through
:meth:`JobStore.set_status` -- so that single compare-and-set is the COMPLETE
set of edges a job-state wait has to observe. Subscribing to it turns the poll
into an edge-triggered wait: the wait returns the instant the status is
committed, however long ago that was relative to now, so a starved-but-progressing
suite is no longer raced. ``save`` is published on too, because
:func:`_wait_until` takes arbitrary predicates over the whole row.
"""

import asyncio
import pathlib
import tempfile
import threading
from contextlib import contextmanager
from typing import Any

from _policy_factories import UNRESTRICTED_HOST_POLICY
from _result_builders import _canned_result

from app.crawl.job_store import CrawlJobState, JobStatus, JobStore

#: Hang guard for the store waits. Deliberately NOT ``HANG_GUARD_SECONDS``: that
#: one guards waits for a task the SAME loop has already scheduled, and such a
#: task either runs on the next turn or never. This guard is for a task in
#: another thread -- the runner's ``asyncio.to_thread`` worker on a box with
#: four agent sessions, which was delayed long enough to blow the 5.0s deadline
#: this replaced (62.9s of user CPU inside a 271.5s run). A progressing job
#: writes nothing for seconds at a time under that load, so 5.0s here is not a
#: hang guard, it is the same starvation trap wearing a different name. The
#: terminal path does not rely on it at all -- it joins the runner task, which
#: has no clock in it -- so this bound only ever fires on a store that is not
#: being written to by anything.
STORE_HANG_GUARD_SECONDS = 30.0

#: Statuses that mean the runner is finished. Only for these may a wait join the
#: runner task: a mid-run wait (``{"running"}``, ``{"paused"}``) is asking about
#: a job that is deliberately NOT finished, and joining it would wait for the
#: very thing the test is about to interrupt.
_TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED.value, JobStatus.FAILED.value})


class _StatusBus:
    """Edge-triggered notifications for one ``JobStore``.

    Subscription is edge-triggered and the subscription happens BEFORE the
    first read, so a transition that lands between the read and the wait still
    wakes the waiter (its callback is already queued) and none can be missed.
    That is what lets :func:`_wait_status` drop its deadline without a window.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._listeners: list[_StoreListener] = []

    def publish(self, job_id: str) -> None:
        with self._lock:
            targets = list(self._listeners)
        for listener in targets:
            if listener.job_id == job_id:
                listener.notify()

    def subscribe(self, job_id: str) -> "_StoreListener":
        listener = _StoreListener(self, job_id, asyncio.get_running_loop())
        with self._lock:
            self._listeners.append(listener)
        return listener

    def discard(self, listener: "_StoreListener") -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)


class _StoreListener:
    """One subscriber's wake-up: an event plus the loop that has to be woken.

    Job-store writes reach the store from a worker thread -- the runner and the
    control plane both go through ``asyncio.to_thread`` -- so the signal hops to
    the awaiting loop with ``call_soon_threadsafe``. Setting an ``asyncio.Event``
    from a foreign thread is not safe.
    """

    __slots__ = ("_bus", "event", "job_id", "loop")

    def __init__(self, bus: _StatusBus, job_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self._bus = bus
        self.job_id = job_id
        self.loop = loop
        self.event = asyncio.Event()

    def notify(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.event.set)
        except RuntimeError:
            # The awaiting loop is already closed: the test is over and there is
            # nobody left to wake. Dropping the signal is the only correct move.
            pass

    def close(self) -> None:
        self._bus.discard(self)


def _instrument_status_bus(store: JobStore) -> JobStore:
    """Publish every control-plane and data write this store receives.

    Both methods are rebound on the INSTANCE, which shadows the class method for
    every caller (``job_store.save(...)``, ``asyncio.to_thread(job_store.save,
    ...)``, ``self.save`` from ``asave``) without touching the store class that
    production uses.
    """
    bus = _StatusBus()
    real_set_status = store.set_status
    real_save = store.save

    def set_status(job_id, new, *, expected):
        changed = real_set_status(job_id, new, expected=expected)
        if changed:
            bus.publish(job_id)
        return changed

    def save(job):
        real_save(job)
        bus.publish(job.job_id)

    store.status_bus = bus
    store.set_status = set_status
    store.save = save
    return store


def make_job_store(tmpdir: str) -> JobStore:
    db_path = pathlib.Path(tmpdir) / "jobs.db"
    return _instrument_status_bus(JobStore(db_path, policy=UNRESTRICTED_HOST_POLICY))


@contextmanager
def single_shot_job(job_id: str = "test-id", root: str = "http://example.com/"):
    """A throwaway store holding one fresh SINGLE_SHOT job; yield ``(store, job)``.

    The store, the temporary directory and the seed job are one fixture: a test
    about SINGLE_SHOT semantics should not have to re-derive them, and a second
    copy of the seed line is a second thing to forget to update.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_job_store(tmpdir)
        yield store, store.create_single_shot(job_id, root, {"test": "policy"})


def make_crawled_job_store(tmpdir: str, pages: int) -> tuple[JobStore, CrawlJobState]:
    """A store holding one job that has already crawled ``pages`` pages.

    The companion to :func:`make_job_store` for the tests whose subject is a job
    with history behind it -- what a status read costs, and what a batch persist
    costs. Both need a non-empty accumulator, and building one the same way
    twice is a clone rather than a coincidence.
    """
    store = make_job_store(tmpdir)
    job = store.create("http://example.test/a", {})
    job.visited = [f"http://example.test/p/{i}" for i in range(pages)]
    job.results = [{"url": url, "status": "success"} for url in job.visited]
    store.save(job)
    return store, job


async def run_job_control_test(
    app,
    job_store: JobStore,
    job_id: str,
    action: str,
    expected_status: int = 422,
) -> None:
    from httpx import ASGITransport, AsyncClient

    async with ASGITransport(app=app) as transport:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/v1/jobs/{job_id}/control", json={"action": action})
            assert resp.status_code == expected_status


async def _execution_urls(payload_dict, hang: asyncio.Event | None):
    if hang is not None and not hang.is_set():
        await hang.wait()
    return payload_dict.get("urls") or [payload_dict.get("url")]


def _execution_payload(results: list[dict], submitted_count: int) -> dict:
    return {
        "results": results,
        "submitted_count": submitted_count,
        "policy_schema_version": "test",
    }


async def run_canned_graph(
    client,
    job_store,
    root: str,
    scripts: dict[str, list[dict]],
    policy: dict | None = None,
) -> tuple[dict[str, int], Any]:
    """Run a canned page graph to completion; return ``(attempts, job)``.

    The admission-contract tests all state the same thing in different shapes:
    a scripted graph, the per-URL attempt count, and the finished job. The
    attempt count is what separates "fetched once" from "fetched twice and still
    produced a result", so it is part of what this hands back rather than
    something each test wires up for itself.
    """
    attempts: dict[str, int] = {}
    job = await _run_job_to_completed(
        client,
        job_store,
        root,
        _fake_execute_scripted(scripts, attempts=attempts),
        policy,
    )
    return attempts, job


def _fake_execute_scripted(
    scripts: dict[str, list[dict]],
    *,
    attempts: dict[str, int] | None = None,
    hang: asyncio.Event | None = None,
):
    async def _execute(payload_dict, *, settings, shutdown_event):
        urls = await _execution_urls(payload_dict, hang)
        results = []
        for url in urls:
            sequence = scripts.get(url)
            if sequence is None:
                entry = _canned_result(url)
            elif len(sequence) > 1:
                entry = sequence.pop(0)
            else:
                entry = sequence[0]
            if attempts is not None:
                attempts[url] = attempts.get(url, 0) + 1
            results.append(entry)
        return _execution_payload(results, len(urls))

    return _execute


def _fake_execute(results: list[dict], *, hang: asyncio.Event | None = None):
    async def _execute(payload_dict, *, settings, shutdown_event):
        urls = await _execution_urls(payload_dict, hang)
        by_url = {result["original_url"]: result for result in results}
        selected = [by_url.get(url, _canned_result(url, status="failed")) for url in urls]
        return _execution_payload(selected, len(urls))

    return _execute


def _make_app(job_store):
    from fastapi import FastAPI

    from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
    from app.api.jobs_router import router as jobs_router
    from app.settings import CrawlerSettings

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
    ``_run_job`` returns when it has finished, and it does not return at a time
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
    from app.api import job_runner

    task = job_runner._tasks.get(job_id)
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


async def _wait_status(job_store, job_id: str, statuses: set[str]) -> str:
    """Block until ``job_id`` holds one of ``statuses``; return the status name.

    Subscribe first, then read, then wait -- in that order, and the read comes
    before the wait because the state can already be what the caller asked for
    (``_run_persisted_job`` drives ``_run_job`` to completion in-line before it
    ever calls this). Clearing the event after the read is what makes the wait
    lossless: a write that lands after the clear has its ``call_soon_threadsafe``
    callback queued, so the very next suspension runs it and the wait returns; a
    write that landed before the clear is visible to a read that follows it.

    When the caller is waiting for a terminal status there is a live runner task
    to join, and that is the preferred path: it carries no deadline at all.
    """
    bus = getattr(job_store, "status_bus", None)
    if bus is None:
        raise AssertionError(
            "this job store has no status bus: build it with make_job_store() so "
            "job-state waits are edge-triggered instead of polling a deadline"
        )
    terminal = bool(statuses) and statuses <= _TERMINAL_STATUSES
    listener = bus.subscribe(job_id)
    try:
        while True:
            job = job_store.load(job_id)
            if job is not None and job.status.value in statuses:
                return job.status.value
            if terminal and await _join_runner_task(job_id):
                continue  # the row may have moved; re-read before waiting again
            await _next_store_edge(job_store, job_id, listener)
    finally:
        listener.close()


async def _wait_until(job_store, job_id: str, predicate):
    """Block until ``predicate(job)`` holds; return that job.

    Same edge-driven shape as :func:`_wait_status`, but the predicate ranges
    over the whole row (frontier length, stat counters), so ``save`` is the edge
    source rather than the status CAS alone. There is no runner task to join
    here -- the predicate is about an intermediate state -- so the store-level
    hang guard is the only bound.
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
            if job is not None and predicate(job):
                return job
            await _next_store_edge(job_store, job_id, listener)
    finally:
        listener.close()


def _execution_policy(policy: dict | None) -> dict:
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


async def _create_api_job(client, root_url: str, execute, policy: dict | None = None):
    from app.api import job_runner

    job_runner.install_execute_crawl(execute)
    response = await post_api_job(client, root_url, policy)
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def _seed_job(
    job_store,
    root_url: str,
    policy: dict | None = None,
    *,
    frontier: list[str] | None = None,
    in_flight: list[str] | None = None,
    running: bool = False,
):
    job = job_store.create(root_url, policy if policy is not None else {})
    if frontier is not None:
        job.frontier = list(frontier)
    if in_flight is not None:
        job.in_flight = list(in_flight)
    job_store.save(job)
    if running:
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    return job


async def _run_persisted_job(job, job_store, execute):
    from app.api import job_runner
    from app.settings import CrawlerSettings

    job_runner.install_execute_crawl(execute)
    await job_runner._run_job(
        job.job_id,
        job_store,
        CrawlerSettings.from_mapping({}),
        asyncio.Event(),
    )
    status = await _wait_status(job_store, job.job_id, {"completed", "failed"})
    assert status in {"completed", "failed"}
    finished = job_store.load(job.job_id)
    assert finished is not None
    return finished


async def _run_scripted_persisted_job(job, job_store, result_map):
    attempts: dict[str, int] = {}
    finished = await _run_persisted_job(
        job,
        job_store,
        _fake_execute_scripted(result_map, attempts=attempts),
    )
    return finished, attempts


async def _run_job_to_completed(client, job_store, root_url: str, execute, policy: dict | None = None):
    job_id = await _create_api_job(client, root_url, execute, policy)
    status = await _wait_status(job_store, job_id, {"completed", "failed"})
    assert status == "completed"
    return job_store.load(job_id)


async def _run_scripted_job(
    client, job_store, root_url: str, sequence: list[dict]
) -> tuple[Any, dict[str, int]]:
    attempts: dict[str, int] = {}
    job = await _run_job_to_completed(
        client,
        job_store,
        root_url,
        _fake_execute_scripted({root_url: sequence}, attempts=attempts),
    )
    return job, attempts


__all__ = [
    "_create_api_job",
    "_execution_payload",
    "_execution_policy",
    "_execution_urls",
    "_fake_execute",
    "_fake_execute_scripted",
    "_make_app",
    "post_api_job",
    "post_render_with_pool",
    "run_seeded_scripted_job",
    "_run_job_to_completed",
    "_run_persisted_job",
    "_run_scripted_job",
    "_run_scripted_persisted_job",
    "_seed_job",
    "_wait_status",
    "_wait_until",
    "make_job_store",
    "run_job_control_test",
]


async def post_api_job(client, root_url: str, policy: dict | None = None):
    return await client.post("/v1/jobs", json={"root_url": root_url, "policy": _execution_policy(policy)})


async def run_seeded_scripted_job(
    job_store,
    root_url: str,
    policy: dict,
    result_map: dict[str, list[dict]],
    **seed: Any,
):
    job = _seed_job(job_store, root_url, policy, **seed)
    return await _run_scripted_persisted_job(job, job_store, result_map)


async def post_render_with_pool(client, pool, url: str):
    from app.crawl.strategies.browser_pool import set_browser_pool

    old = set_browser_pool(pool)
    try:
        return await client.post("/v1/render", json={"url": url})
    finally:
        set_browser_pool(old)
