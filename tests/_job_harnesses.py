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
import sqlite3
import tempfile
import threading
from collections.abc import Callable
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
        scripts: dict[str, list[dict]] | None = None,
        *,
        results: list[dict] | None = None,
        hang: asyncio.Event | None = None,
    ):
        if (scripts is None) == (results is None):
            raise AssertionError("ScriptedExecute takes exactly one of scripts= or results=")
        self._scripts = dict(scripts) if scripts is not None else {r["original_url"]: [r] for r in results or []}
        self._default = "rendered" if scripts is not None else "failed"
        self._hang = hang
        self.attempts: dict[str, int] = {}

    def _entry(self, url: str) -> dict:
        sequence = self._scripts.get(url)
        if sequence is None:
            return _canned_result(url, status=self._default)
        return sequence.pop(0) if len(sequence) > 1 else sequence[0]

    async def __call__(self, payload_dict, *, settings, shutdown_event):
        urls = await _execution_urls(payload_dict, self._hang)
        for url in urls:
            self.attempts[url] = self.attempts.get(url, 0) + 1
        return _execution_payload([self._entry(url) for url in urls], len(urls))


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


async def _wait_for(job_store, job_id: str, satisfied, *, join_task: bool):
    """Block until ``satisfied(job)`` holds; return that job.

    Subscribe first, then read, then wait -- in that order, and the read comes
    before the wait because the state can already be what the caller asked for
    (a caller that drove ``_run_job`` in-line reaches this only once the job is
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


async def _wait_status(job_store, job_id: str, statuses: set[str]) -> str:
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
        join_task=bool(statuses) and statuses <= _TERMINAL_STATUSES,
    )
    return job.status.value


async def _wait_until(job_store, job_id: str, predicate):
    """Block until ``predicate(job)`` holds; return that job.

    The predicate ranges over the whole row (frontier length, stat counters), so
    ``save`` is the edge source rather than the status CAS alone.
    """
    return await _wait_for(job_store, job_id, predicate, join_task=False)


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
    on_seeded: Callable[[Any, Any], None] | None = None,
):
    job = job_store.create(root_url, policy if policy is not None else {})
    if frontier is not None:
        job.frontier = list(frontier)
    if in_flight is not None:
        job.in_flight = list(in_flight)
    job_store.save(job)
    if running:
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    if on_seeded is not None:
        on_seeded(job, job_store)
    return job


async def run_to_terminal(job_store, job_id: str) -> Any:
    """Block until ``job_id`` settles; return the finished row.

    This is the one place that knows what "done" means, so the wait and the load
    are stated once. The row is re-read after the wait rather than taken from the
    wait itself: a status CAS and the body that records the batch are separate
    writes, and handing back a row that disagrees with the state the wait
    confirmed would be a lie the caller cannot detect.
    """
    status = await _wait_status(job_store, job_id, _TERMINAL_STATUSES)
    finished = job_store.load(job_id)
    assert finished is not None, f"job {job_id} reported {status} with no row"
    assert finished.status.value == status, (
        f"job {job_id} settled {status} but the row it handed back reads "
        f"{finished.status.value}: the terminal CAS landed before the batch did"
    )
    return finished


async def run_api_job(client, job_store, root_url: str, execute, policy: dict | None = None):
    """Create a job through the API and drive it to terminal; return the row."""
    return await run_to_terminal(job_store, await _create_api_job(client, root_url, execute, policy))


async def run_seeded_job(job, job_store, execute):
    """Drive an already-persisted job in-line to terminal; return the row."""
    from app.api import job_runner
    from app.settings import CrawlerSettings

    job_runner.install_execute_crawl(execute)
    await job_runner._run_job(
        job.job_id,
        job_store,
        CrawlerSettings.from_mapping({}),
        asyncio.Event(),
    )
    return await run_to_terminal(job_store, job.job_id)


async def run_seeded_scripted_job(
    job_store,
    root_url: str,
    policy: dict,
    execute,
    on_seeded: Callable[[Any, Any], None] | None = None,
    **seed: Any,
):
    """Seed a row the way a crash left it, then drive it to terminal.

    Recovery tests vary three things -- what the engine answers, what the row
    already held, and how it ends -- and the seed/run/load trio spelling them
    out was one of the widest repeated lines in the tree. The engine stays a
    caller-owned name so the attempt count remains reachable after the run.

    ``on_seeded`` gets ``(job, job_store)`` after the row is persisted and
    before the first engine call, for a row shape the write path refuses to
    produce -- a corrupt policy column, a legacy envelope. It belongs here
    rather than at the call site because a test that has to mutate the row
    between the seed and the run otherwise has to hold the two halves of this
    funnel itself, and then the seeding contract is theirs to keep in sync.
    """
    return await run_seeded_job(
        _seed_job(job_store, root_url, policy, on_seeded=on_seeded, **seed), job_store, execute
    )


def recording_execute(execute, captured: list[dict]):
    """Wrap a scripted engine, recording every payload dict it was handed.

    The engine call is the only place the runner's assembled batch is visible
    before results come back, so the request profile and the admitted URLs are
    read off the payloads rather than off the request the test sent.
    """

    async def _execute(payload_dict, *, settings, shutdown_event):
        captured.append(payload_dict)
        return await execute(payload_dict, settings=settings, shutdown_event=shutdown_event)

    return _execute


def job_limits(max_depth: int = 2, max_pages: int = 0, **extra) -> dict:
    """A whole-site job policy carrying nothing but its limits.

    The runner tests all state the same shape -- a policy that is a ``limits``
    block and nothing else -- and restating it inline is how two of them ended
    up capping pages at different depths while reading identically. Unset limits
    are filled in by :func:`_execution_policy` on the way to the API.
    """
    limits: dict[str, Any] = {"max_depth": max_depth, "max_pages": max_pages}
    limits.update(extra)
    return {"limits": limits}


@contextmanager
def raw_job_row(job_store):
    """Direct SQL against a job's own row, for shapes the write path refuses.

    A row that only a crash or a legacy migration could leave behind cannot be
    written through the validated API, so these tests go to the table. The
    transaction lives here rather than at each call site because a raw write
    that forgets to commit is a test that passes without the row ever changing.
    """
    connection = sqlite3.connect(job_store.db_path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


async def submit_blocked_job(client, job_store, root_url: str, results, policy=None):
    """Submit a job whose engine blocks; return ``(job_id, release)``.

    A control-plane test has to flip a control bit while the runner is in
    flight, and without a block the job may already be terminal by the time the
    test looks. ``release`` is the event to set at the moment the test means the
    in-flight batch to be answered.
    """
    hang = asyncio.Event()
    job_id = await _create_api_job(client, root_url, ScriptedExecute(results=results, hang=hang), policy)
    await _wait_status(job_store, job_id, {"running"})
    return job_id, hang


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
    "job_limits",
    "make_job_store",
    "post_api_job",
    "post_render_with_pool",
    "raw_job_row",
    "recording_execute",
    "run_api_job",
    "run_job_control_test",
    "run_seeded_job",
    "run_seeded_scripted_job",
    "run_to_terminal",
    "submit_blocked_job",
]


async def post_api_job(client, root_url: str, policy: dict | None = None):
    return await client.post("/v1/jobs", json={"root_url": root_url, "policy": _execution_policy(policy)})


async def post_render_with_pool(client, pool, url: str):
    from app.crawl.strategies.browser_pool import set_browser_pool

    old = set_browser_pool(pool)
    try:
        return await client.post("/v1/render", json={"url": url})
    finally:
        set_browser_pool(old)
