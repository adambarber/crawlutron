import asyncio
import functools
import pathlib
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.storage.coordination_cookies import SharedCookieStore
from crawlutron.storage.job_store import CrawlJobState, FrontierEntry, JobStatus, JobStore, OrderedUrlSet
from crawlutron.storage.result_store import CrawlResultStore
from crawlutron.storage.watch_store import WatchStore
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
    # The store base binds the conn's lifecycle: the store's GC closes it.
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


def seed_frontier(job: CrawlJobState, entries: list[FrontierEntry]) -> None:
    """Wholesale frontier replacement with the removal log kept honest (#547).

    The store persists the queue delta-only: a replacement logs every old row
    for deletion and re-lands the whole new list in order."""
    job.frontier_removed.extend(entry.url for entry in job.frontier)
    job.frontier_mark = 0
    job.frontier = entries


@contextmanager
def single_shot_job(job_id: str = "test-id", root: str = "http://example.com/"):
    """A throwaway store holding one fresh SINGLE_SHOT job; yield ``(store, job)``.

    Store, temp dir and seed are one fixture -- an about-SINGLE_SHOT test should
    not re-derive them, and a second seed copy is a second thing to forget."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = make_job_store(tmpdir)
        try:
            yield store, store.create_single_shot(job_id, root, CrawlPolicy())
        finally:
            store.close()


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
    job.visited = OrderedUrlSet(canonical(f"http://example.test/p/{i}") for i in range(pages))
    job.results = [{"url": url.raw, "status": "success"} for url in job.visited]
    store.save(job)
    return store, job


class LoopGuardJobStore(JobStore):
    """The real store, with every sync entry point refusing to run on the loop."""

    def create(self, root_url, policy, *, owner=""):
        _refuse_on_loop("JobStore.create")
        return super().create(root_url, policy, owner=owner)

    def create_single_shot(self, job_id, root_url, policy, *, owner="", frontier=None):
        _refuse_on_loop("JobStore.create_single_shot")
        return super().create_single_shot(job_id, root_url, policy, owner=owner, frontier=frontier)

    def save(self, job):
        _refuse_on_loop("JobStore.save")
        return super().save(job)

    def set_status(self, job_id, new: JobStatus, *, expected=None, error=None):
        _refuse_on_loop("JobStore.set_status")
        return super().set_status(job_id, new, expected=expected, error=error)

    def load(self, job_id):
        _refuse_on_loop("JobStore.load")
        return super().load(job_id)

    def load_for_caller(self, job_id, caller_id):
        _refuse_on_loop("JobStore.load_for_caller")
        return super().load_for_caller(job_id, caller_id)

    def reload_state(self, job_id):
        _refuse_on_loop("JobStore.reload_state")
        return super().reload_state(job_id)

    def delete(self, job_id):
        _refuse_on_loop("JobStore.delete")
        return super().delete(job_id)

    def delete_for_caller(self, job_id, caller_id):
        _refuse_on_loop("JobStore.delete_for_caller")
        return super().delete_for_caller(job_id, caller_id)

    def list_jobs(self, *, limit=None, offset=0):
        _refuse_on_loop("JobStore.list_jobs")
        return super().list_jobs(limit=limit, offset=offset)

    def list_for_caller(self, caller_id, *, limit=None, offset=0):
        _refuse_on_loop("JobStore.list_for_caller")
        return super().list_for_caller(caller_id, limit=limit, offset=offset)

    def status_row(self, job_id):
        _refuse_on_loop("JobStore.status_row")
        return super().status_row(job_id)

    def status_for_caller(self, job_id, caller_id):
        _refuse_on_loop("JobStore.status_for_caller")
        return super().status_for_caller(job_id, caller_id)

    def exists(self, job_id):
        _refuse_on_loop("JobStore.exists")
        return super().exists(job_id)

    def exists_for_caller(self, job_id, caller_id):
        _refuse_on_loop("JobStore.exists_for_caller")
        return super().exists_for_caller(job_id, caller_id)

    def active_count_for_caller(self, caller_id):
        _refuse_on_loop("JobStore.active_count_for_caller")
        return super().active_count_for_caller(caller_id)

    def count_jobs(self):
        _refuse_on_loop("JobStore.count_jobs")
        return super().count_jobs()

    def count_for_caller(self, caller_id):
        _refuse_on_loop("JobStore.count_for_caller")
        return super().count_for_caller(caller_id)

    def close(self):
        _refuse_on_loop("JobStore.close")
        return super().close()


class LoopGuardResultStore(CrawlResultStore):
    """The real result store, with its sync entry points refusing the loop."""

    def put(self, request_id, payload, *, owner=""):
        _refuse_on_loop("CrawlResultStore.put")
        return super().put(request_id, payload, owner=owner)

    def get(self, request_id):
        _refuse_on_loop("CrawlResultStore.get")
        return super().get(request_id)

    def get_for_caller(self, request_id, caller_id):
        _refuse_on_loop("CrawlResultStore.get_for_caller")
        return super().get_for_caller(request_id, caller_id)

    def payloads_page(self, *, limit, cursor=None, owner=None):
        _refuse_on_loop("CrawlResultStore.payloads_page")
        return super().payloads_page(limit=limit, cursor=cursor, owner=owner)

    def retained_count(self, *, owner=None):
        _refuse_on_loop("CrawlResultStore.retained_count")
        return super().retained_count(owner=owner)

    def close(self):
        _refuse_on_loop("CrawlResultStore.close")
        return super().close()


class LoopGuardWatchStore(WatchStore):
    """The real watch store, every sync entry point refusing the loop.

    The sweep is the hard half: it runs for the process's life, so a sync store
    call there would park the loop answering every in-flight request.
    """

    def create(self, url, /, **kwargs):
        _refuse_on_loop("WatchStore.create")
        return super().create(url, **kwargs)

    def get(self, watch_id):
        _refuse_on_loop("WatchStore.get")
        return super().get(watch_id)

    def get_for_caller(self, watch_id, caller_id):
        _refuse_on_loop("WatchStore.get_for_caller")
        return super().get_for_caller(watch_id, caller_id)

    def list_watches(self):
        _refuse_on_loop("WatchStore.list_watches")
        return super().list_watches()

    def list_for_caller(self, caller_id):
        _refuse_on_loop("WatchStore.list_for_caller")
        return super().list_for_caller(caller_id)

    def count_for_owner(self, owner):
        _refuse_on_loop("WatchStore.count_for_owner")
        return super().count_for_owner(owner)

    def delete(self, watch_id):
        _refuse_on_loop("WatchStore.delete")
        return super().delete(watch_id)

    def delete_for_caller(self, watch_id, caller_id):
        _refuse_on_loop("WatchStore.delete_for_caller")
        return super().delete_for_caller(watch_id, caller_id)

    def deactivate(self, watch_id, **kwargs):
        _refuse_on_loop("WatchStore.deactivate")
        return super().deactivate(watch_id, **kwargs)

    def due(self, now):
        _refuse_on_loop("WatchStore.due")
        return super().due(now)

    def record_check(self, watch_id, **kwargs):
        _refuse_on_loop("WatchStore.record_check")
        return super().record_check(watch_id, **kwargs)

    def claim_due(self, now, token, **kwargs):
        _refuse_on_loop("WatchStore.claim_due")
        return super().claim_due(now, token, **kwargs)

    def renew_claim(self, watch_id, token, **kwargs):
        _refuse_on_loop("WatchStore.renew_claim")
        return super().renew_claim(watch_id, token, **kwargs)

    # Inherited from ``CoordinationStore`` rather than written here, and still covered: the base's ``close``
    # takes the same file-wide lock as a write.
    def close(self):
        _refuse_on_loop("WatchStore.close")
        return super().close()


class LoopGuardSharedCookieStore(SharedCookieStore):
    """The real cookie store, every sync entry point refusing the loop."""

    def load(self, namespace: str, host: str) -> dict[str, str]:
        _refuse_on_loop("SharedCookieStore.load")
        return super().load(namespace, host)

    def merge(self, namespace: str, host: str, cookies) -> None:
        _refuse_on_loop("SharedCookieStore.merge")
        return super().merge(namespace, host, cookies)

    def touch(self, namespace: str, host: str) -> None:
        _refuse_on_loop("SharedCookieStore.touch")
        return super().touch(namespace, host)

    def slots_for_namespace(self, namespace: str) -> dict[str, dict[str, str]]:
        _refuse_on_loop("SharedCookieStore.slots_for_namespace")
        return super().slots_for_namespace(namespace)

    def clear(self, namespace: str = "") -> None:
        _refuse_on_loop("SharedCookieStore.clear")
        return super().clear(namespace)


async def _off_loop(func, *args, **kwargs):
    """Call a store method the way production must: off the loop."""
    return await asyncio.to_thread(functools.partial(func, *args, **kwargs))


def _refuse_on_loop(name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AssertionError(f"sync store method {name} ran on the event loop; the async twin owns that call")
