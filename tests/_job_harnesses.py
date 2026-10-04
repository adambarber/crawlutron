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
import pathlib
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Collection
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.crawl.job_store import TERMINAL_STATUSES, CrawlJobState, FrontierEntry, JobStatus, JobStore
from app.crawl.job_store import _Expected as _Expected
from app.crawl.policy import PolicyLike, PolicyTier
from app.crawl.result_record import ProducerEnvelope
from app.models.crawl_policy import CrawlPolicy
from app.models.engine_crawl_result import EngineCrawlResult
from app.models.taxonomy import Classification
from app.validation import HostPolicy
from app.models.normalized_url import NormalizedUrl
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, job_store
from tests._result_builders import _admit, _canned_result

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

#: The statuses a job settles in, as the store spells them. Derived from the
#: store's own set rather than restated here: "has this job finished?" is a
#: question every status reader asks, and a second hand-written answer is how
#: this one drifted in the first place. It used to omit CANCELLED, which cost a
#: cancelled job the thirty-second hang guard and then an assertion it had
#: already satisfied.
_SETTLED_STATUSES = frozenset(status.value for status in TERMINAL_STATUSES)

#: The subset of those a wait may *join the runner task* for. Strictly smaller
#: than settled, and the difference is load-bearing: joining a cancelled job
#: waits for the very thing the test is about to interrupt. This answers "may I
#: join?" and nothing else -- the previous name claimed to answer "is it done?"
#: while excluding CANCELLED, which is how one set came to serve two questions.
_JOINABLE_STATUSES = _SETTLED_STATUSES - {JobStatus.CANCELLED.value}


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


class InstrumentedJobStore(JobStore):
    """A real ``JobStore`` that also publishes every write it receives to a bus.

    ``set_status`` and ``save`` are OVERRIDDEN, which shadows the base method for
    every caller (``job_store.save(...)``, ``asyncio.to_thread(job_store.save,
    ...)``, ``self.save`` from ``asave``) without touching the store class
    production uses.

    This is a subclass rather than attributes poked onto an instance because
    ``status_bus`` is not a ``JobStore`` field: production has no such concept,
    and ``tests/api/test_jobs_execution.py`` asserts exactly that -- the marker
    is present on an instrumented store and absent on a bare one. Poking the
    attribute and the two methods onto the instance made that a promise the type
    of the object denied, and rebound a method on an object typed as having a
    read-only one. The bus is a test concern, so the type carrying it is a test
    type, and it is still a real ``JobStore`` everywhere production asks for one.
    """

    def __init__(self, db_path: str | pathlib.Path, *, policy: HostPolicy, tier: PolicyTier) -> None:
        super().__init__(db_path, policy=policy, tier=tier)
        self.status_bus = _StatusBus()
        # Bound off the CLASS, not off ``self``: by this point ``self.set_status``
        # already resolves to the override below, and capturing that would make
        # every publish an infinite recursion.
        self._real_set_status: Callable[..., bool] = JobStore.set_status.__get__(self)
        self._real_save: Callable[[CrawlJobState], None] = JobStore.save.__get__(self)

    def set_status(self, job_id: str, new: JobStatus, *, expected: _Expected) -> bool:
        changed = self._real_set_status(job_id, new, expected=expected)
        if changed:
            self.status_bus.publish(job_id)
        return changed

    def save(self, job: CrawlJobState) -> None:
        self._real_save(job)
        self.status_bus.publish(job.job_id)


def make_job_store(tmpdir: str) -> JobStore:
    db_path = pathlib.Path(tmpdir) / "jobs.db"
    return job_store(db_path, of=InstrumentedJobStore)


def loaded_job(store: JobStore, job_id: str) -> CrawlJobState:
    """``store.load(job_id)``, asserted present.

    ``load`` returns ``None`` for a job that does not exist, and the call sites
    that reach for it are all reading back a row they just created or saved.
    Asserting here rather than at each site says what actually went wrong when
    the store loses one -- a real message naming the job -- instead of an
    ``AttributeError`` on ``None`` several frames from the cause, and it is one
    check rather than the same check copied.
    """
    job = store.load(job_id)
    assert job is not None, f"job {job_id!r} has no row in the store after it was written"
    return job


def frontier(urls: list[str], depth: int = 0) -> list[FrontierEntry]:
    """``urls`` as frontier entries, at ``depth``.

    ``CrawlJobState.frontier`` is ``list[FrontierEntry]``, and a raw string in
    that list survives only because ``_frontier_identity`` sniffs the element
    type. The moment anything reads ``entry.url`` off it, a string stops being
    a URL and becomes an ``AttributeError``. Built through the real dataclass so
    the fixture says what the field holds.
    """
    return [FrontierEntry(url=_admit(url), depth=depth) for url in urls]


@contextmanager
def single_shot_job(job_id: str = "test-id", root: str = "http://example.com/"):
    """A throwaway store holding one fresh SINGLE_SHOT job; yield ``(store, job)``.

    The store, the temporary directory and the seed job are one fixture: a test
    about SINGLE_SHOT semantics should not have to re-derive them, and a second
    copy of the seed line is a second thing to forget to update.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_job_store(tmpdir)
        yield store, store.create_single_shot(job_id, root, CrawlPolicy())


@contextmanager
def single_shot_orphan(job_id: str = "test-id", root: str = "http://example.com/"):
    """A throwaway store holding one SINGLE_SHOT job stranded in RUNNING.

    The state a process crash leaves behind: the row was claimed for a crawl
    that the service can never resume, because a single-shot job has no
    frontier to continue from. Recovery has to retire it rather than rewind it.

    Separate from :func:`single_shot_job` because that one seeds the *fresh*
    state, and a test about recovery has to state the stranded one -- spelling
    the claim-and-crash out again at each call site is how the two states came
    to be confused.
    """
    with single_shot_job(job_id, root) as (store, job):
        store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
        yield store, job


def make_crawled_job_store(tmpdir: str, pages: int) -> tuple[JobStore, CrawlJobState]:
    """A store holding one job that has already crawled ``pages`` pages.

    The companion to :func:`make_job_store` for the tests whose subject is a job
    with history behind it -- what a status read costs, and what a batch persist
    costs. Both need a non-empty accumulator, and building one the same way
    twice is a clone rather than a coincidence.
    """
    from tests._result_builders import _admit

    store = make_job_store(tmpdir)
    job = store.create("http://example.test/a", CrawlPolicy())
    # Visited holds admitted values now; results stay the persisted wire
    # dicts the store actually keeps, naming each page by its raw identity.
    job.visited = {_admit(f"http://example.test/p/{i}") for i in range(pages)}
    job.results = [{"url": url.raw, "status": "success"} for url in job.visited]
    store.save(job)
    return store, job


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
    # The payload is the engine's wire shape, so its URLs are text; the real
    # seam admits them before any result is built, and the double must cross
    # the same boundary or it is recording a shape production never sends.
    urls = payload_dict.get("urls") or [payload_dict.get("url")]
    return [_admit(url) for url in urls]


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
        # Keys are URL identities: caller-spelled text is admitted here (so a
        # key like "http://example.test" matches its canonical identity), and
        # results-mode keys are the funnel's projected originals, which are
        # canonical by construction. Lookup keys arrive as values.
        if scripts is not None:
            self._scripts = {_admit(url).raw: list(seq) for url, seq in scripts.items()}
        else:
            self._scripts = {r.original_url: [r] for r in results or []}
        # A real ``ResultStatus``, not a classification: the result record
        # validates its own taxonomy, and "rendered" is one -- so the default
        # used to be a status the engine never emits, invisible while the
        # double produced an unvalidated dict.
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


async def _create_api_job(client, root_url: str, execute, policy: PolicyLike | None = None):
    from app.api import job_runner

    job_runner.install_execute_crawl(execute)
    response = await post_api_job(client, root_url, policy)
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def _seed_job(
    job_store,
    root_url: str,
    policy: PolicyLike | None = None,
    *,
    frontier: list[str] | None = None,
    in_flight: list[str] | None = None,
    running: bool = False,
    on_seeded: Callable[[Any, Any], None] | None = None,
):
    job = job_store.create(root_url, policy if policy is not None else {})
    if frontier is not None:
        job.frontier = [FrontierEntry(url=_admit(url), depth=0) for url in frontier]
    if in_flight is not None:
        job.in_flight = [FrontierEntry(url=_admit(url), depth=0) for url in in_flight]
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
    status = await _wait_status(job_store, job_id, _SETTLED_STATUSES)
    finished = job_store.load(job_id)
    assert finished is not None, f"job {job_id} reported {status} with no row"
    assert finished.status.value == status, (
        f"job {job_id} settled {status} but the row it handed back reads "
        f"{finished.status.value}: the terminal CAS landed before the batch did"
    )
    return finished


async def run_api_job(client, job_store, root_url: str, execute, policy: PolicyLike | None = None):
    """Create a job through the API and drive it to terminal; return the row."""
    return await run_to_terminal(job_store, await _create_api_job(client, root_url, execute, policy))


async def run_scripted_api_job(
    client,
    job_store,
    root_url: str,
    results: list[EngineCrawlResult],
    policy: PolicyLike | None = None,
):
    """``run_api_job`` for the common case: a scripted engine over canned results.

    Building the engine is the decision, and it was re-spelled as a
    ``ScriptedExecute(results=...)`` argument at every job test that only cares
    about the job. Routing it through here means a change to how a scripted
    engine is constructed cannot land in one test and miss the other eleven.
    """
    return await run_api_job(client, job_store, root_url, ScriptedExecute(results=results), policy)


def drive_job(job_id, job_store, settings=None, shutdown_event=None):
    """Run ``_run_job`` for one already-persisted job; return the task.

    ``_run_job``'s call shape -- id, store, settings, shutdown event -- is what
    every runner-lifecycle test ends on, and ``CrawlerSettings.from_mapping({})``
    was spelled out at each site. A settings default that changes, or a fourth
    argument, is then a four-edit change, and one of the four is this harness:
    the one that silently stops exercising the same call the tests do.

    Returned as a task rather than awaited, because the tests that need to cancel
    or wait on a *running* job have to hold the task to do it.
    """
    from app.api import job_runner
    from app.settings import CrawlerSettings

    return asyncio.ensure_future(
        job_runner._run_job(
            job_id,
            job_store,
            CrawlerSettings.from_mapping({}) if settings is None else settings,
            asyncio.Event() if shutdown_event is None else shutdown_event,
        )
    )


async def run_seeded_job(job, job_store, execute):
    """Drive an already-persisted job in-line to terminal; return the row."""
    from app.api import job_runner

    job_runner.install_execute_crawl(execute)
    await drive_job(job.job_id, job_store)
    return await run_to_terminal(job_store, job.job_id)


async def run_seeded_scripted_job(
    job_store,
    root_url: str,
    policy: PolicyLike,
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


def recording_execute(execute, captured: list[ProducerEnvelope]):
    """Wrap a scripted engine, recording every payload dict it was handed.

    The engine call is the only place the runner's assembled batch is visible
    before results come back, so the request profile and the admitted URLs are
    read off the payloads rather than off the request the test sent.
    """

    async def _execute(payload_dict, *, settings, shutdown_event):
        captured.append(payload_dict)
        return await execute(payload_dict, settings=settings, shutdown_event=shutdown_event)

    return _execute


def job_limits(max_depth: int = 2, max_pages: int = 0, **extra) -> PolicyLike:
    """A whole-site job policy carrying nothing but its limits.

    The runner tests all state the same shape -- a policy that is a ``limits``
    block and nothing else -- and restating it inline is how two of them ended
    up capping pages at different depths while reading identically. Unset limits
    are filled in by :func:`_execution_policy` on the way to the API.
    """
    limits: dict[str, Any] = {"max_depth": max_depth, "max_pages": max_pages}
    limits.update(extra)
    return {"limits": limits}


def assert_settled(
    job,
    *,
    visited: Collection[str],
    discovered: int | None = None,
    crawled: int | None = None,
    in_flight: Collection[str] = (),
) -> None:
    """A job that ran to terminal: stopped, nothing queued, these pages kept.

    The settled-job assertion is the same claim in every whole-site job test --
    the runner drained the frontier, committed nothing in flight, and ended on
    exactly the pages that survived admission. Spelled out per test, the one
    line that matters (the frontier drained) was the one most likely to be
    forgotten, and a job that stopped with links still queued passed.

    ``visited`` is compared as a set because the pages are what the test is
    about, not the order the runner happened to commit them in; the stats a
    test does not claim are not asserted here.

    ``Collection``, not ``Container``: the comparison below builds a set from
    these, and ``Container`` promises only ``in``. A test passing a list was
    right; the annotation was the only thing saying otherwise.
    """
    assert job.status == JobStatus.COMPLETED, f"job ended {job.status}"
    assert not job.frontier, f"frontier left queued: {job.frontier}"
    admitted_in_flight = {_admit(url) for url in in_flight}
    admitted_visited = {_admit(url) for url in visited}
    assert {entry.url for entry in job.in_flight} == admitted_in_flight, (
        f"in-flight left uncommitted: {job.in_flight}"
    )
    assert set(job.visited) == admitted_visited, (
        f"visited {set(job.visited) ^ admitted_visited} differs from what the job kept"
    )
    if discovered is not None:
        assert job.stats["total_discovered"] == discovered, f"discovered {job.stats['total_discovered']}"
    if crawled is not None:
        assert job.stats["total_crawled"] == crawled, f"crawled {job.stats['total_crawled']}"


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
    "run_scripted_api_job",
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


async def post_api_job(client, root_url: str, policy: PolicyLike | None = None):
    return await client.post("/v1/jobs", json={"root_url": root_url, "policy": _execution_policy(policy)})


async def post_render_with_pool(client, pool, url: str):
    from app.crawl.strategies.browser_pool import set_browser_pool

    old = set_browser_pool(pool)
    try:
        return await client.post("/v1/render", json={"url": url})
    finally:
        set_browser_pool(old)


def write_row_policy(job_store, job_id: str, policy_json: str) -> None:
    """Put a policy on a job's row as raw text, bypassing the validating write path.

    A row the API would refuse -- a JSON array where a mapping belongs, a limits
    block that is a string -- can only be produced by writing the table directly,
    which is what the ``load``-path sanitizer exists to survive. The UPDATE and
    its commit live here so a test cannot write the row and forget to commit it,
    which reads as a passing sanitizer test on a row that never changed.
    """
    with raw_job_row(job_store) as connection:
        connection.execute(
            "UPDATE crawl_jobs SET policy = ? WHERE job_id = ?",
            (policy_json, job_id),
        )
