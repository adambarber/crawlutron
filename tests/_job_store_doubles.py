import asyncio
import pathlib
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager

from app.admission.policy import PolicyTier
from app.models.crawl_policy import CrawlPolicy
from app.safety.validation import HostPolicy
from app.storage.job_store import CrawlJobState, FrontierEntry, JobStatus, JobStore
from app.storage.job_store import _Expected as _Expected
from tests._policy_factories import canonical, job_store


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
            # The awaiting loop is already closed: the test is over and there is nobody left to wake. Dropping
            # the signal is the only correct move.
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
        # Bound off the CLASS, not off ``self``: by this point ``self.set_status`` already resolves to the
        # override below, and capturing that would make every publish an infinite recursion.
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
    return [FrontierEntry(url=canonical(url), depth=depth) for url in urls]


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
    from tests._policy_factories import canonical

    store = make_job_store(tmpdir)
    job = store.create("http://example.test/a", CrawlPolicy())
    # Visited holds admitted values now; results stay the persisted wire dicts the store actually keeps,
    # naming each page by its raw identity.
    job.visited = {canonical(f"http://example.test/p/{i}") for i in range(pages)}
    job.results = [{"url": url.raw, "status": "success"} for url in job.visited]
    store.save(job)
    return store, job
