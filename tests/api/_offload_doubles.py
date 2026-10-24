"""Loop-guard store doubles and the off-loop call helper."""

import asyncio
import functools

from app.storage.coordination_cookies import SharedCookieStore
from app.storage.job_store import JobStore
from app.storage.job_store_state import JobStatus
from app.storage.result_store import CrawlResultStore
from app.storage.watch_store import WatchStore


class LoopGuardJobStore(JobStore):
    """The real store, with every sync entry point refusing to run on the loop."""

    def create(self, root_url, policy, *, owner=""):
        _refuse_on_loop("JobStore.create")
        return super().create(root_url, policy, owner=owner)

    def create_single_shot(self, job_id, root_url, policy, *, owner=""):
        _refuse_on_loop("JobStore.create_single_shot")
        return super().create_single_shot(job_id, root_url, policy, owner=owner)

    def save(self, job):
        _refuse_on_loop("JobStore.save")
        return super().save(job)

    def set_status(self, job_id, new: JobStatus, *, expected=None):
        _refuse_on_loop("JobStore.set_status")
        return super().set_status(job_id, new, expected=expected)

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

    def payloads(self):
        _refuse_on_loop("CrawlResultStore.payloads")
        return super().payloads()

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

    def all_slots(self):
        _refuse_on_loop("SharedCookieStore.all_slots")
        return super().all_slots()

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
