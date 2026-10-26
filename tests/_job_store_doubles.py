import asyncio
import pathlib
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager

from app.models.crawl_policy import CrawlPolicy
from app.storage.job_store import CrawlJobState, FrontierEntry, JobStatus, JobStore
from tests._policy_factories import canonical, job_store


class _StatusBus:
    """Edge-triggered notifications for one ``JobStore``.

    Subscription precedes the first read, so a transition between read and
    wait still wakes the waiter -- no window, no deadline."""

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

    Store writes arrive from a worker thread, so the signal hops via
    ``call_soon_threadsafe`` -- an ``asyncio.Event`` from a foreign thread is unsafe.
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


def _publishing_set_status(self, job_id, new, *, expected, error=None) -> bool:
    """The real CAS, then a bus publish when the write landed."""
    changed = self._real_set_status(job_id, new, expected=expected, error=error)
    if changed:
        self.status_bus.publish(job_id)
    return changed


class InstrumentedJobStore(JobStore):
    """A real ``JobStore`` that also publishes every write it receives to a bus.

    Overriding ``set_status``/``save`` shadows the base for every caller; a
    subclass (not instance poking) because ``status_bus`` is not a ``JobStore`` field.
    """

    def __init__(self, db_path, *, policy, tier, row_to_job=None) -> None:
        super().__init__(db_path, policy=policy, tier=tier, row_to_job=row_to_job)
        self.status_bus = _StatusBus()
        # Bound off the CLASS, not off ``self``: by this point ``self.set_status`` already resolves to the
        # override below, and capturing that would make every publish an infinite recursion.
        self._real_set_status: Callable[..., bool] = JobStore.set_status.__get__(self)
        self._real_save: Callable[[CrawlJobState], None] = JobStore.save.__get__(self)

    set_status = _publishing_set_status

    def save(self, job: CrawlJobState) -> None:
        self._real_save(job)
        self.status_bus.publish(job.job_id)


def make_job_store(tmpdir: str) -> JobStore:
    db_path = pathlib.Path(tmpdir) / "jobs.db"
    return job_store(db_path, of=InstrumentedJobStore)


def loaded_job(store: JobStore, job_id: str) -> CrawlJobState:
    """``store.load(job_id)``, asserted present.

    Call sites read back a row they just created; asserting at the seam yields
    a message naming the job, not an ``AttributeError`` on ``None``."""
    job = store.load(job_id)
    assert job is not None, f"job {job_id!r} has no row in the store after it was written"
    return job


def frontier(urls: list[str], depth: int = 0) -> list[FrontierEntry]:
    """``urls`` as frontier entries at ``depth``.

    A raw string survives only while the element-type sniff runs; reading
    ``entry.url`` off one raises. Built through the real dataclass."""
    return [FrontierEntry(url=canonical(url), depth=depth) for url in urls]


@contextmanager
def single_shot_job(job_id: str = "test-id", root: str = "http://example.com/"):
    """A throwaway store holding one fresh SINGLE_SHOT job; yield ``(store, job)``.

    Store, temp dir and seed are one fixture -- an about-SINGLE_SHOT test should
    not re-derive them, and a second seed copy is a second thing to forget."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_job_store(tmpdir)
        yield store, store.create_single_shot(job_id, root, CrawlPolicy())


@contextmanager
def single_shot_orphan(job_id: str = "test-id", root: str = "http://example.com/"):
    """A throwaway store holding one SINGLE_SHOT job stranded in RUNNING.

    The crash state: claimed for a crawl that can never resume, so recovery
    retires it. (The FRESH state is :func:`single_shot_job`.)"""
    with single_shot_job(job_id, root) as (store, job):
        store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
        yield store, job


def make_crawled_job_store(tmpdir: str, pages: int) -> tuple[JobStore, CrawlJobState]:
    """A store holding one job that has already crawled ``pages`` pages.

    The :func:`make_job_store` companion for subjects with history; both need a
    non-empty accumulator, and building it twice is a clone, not a coincidence."""
    from tests._policy_factories import canonical

    store = make_job_store(tmpdir)
    job = store.create("http://example.test/a", CrawlPolicy())
    # Visited holds admitted values now; results stay the persisted wire dicts the store actually keeps,
    # naming each page by its raw identity.
    job.visited = {canonical(f"http://example.test/p/{i}") for i in range(pages)}
    job.results = [{"url": url.raw, "status": "success"} for url in job.visited]
    store.save(job)
    return store, job
