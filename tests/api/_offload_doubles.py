"""Loop-guard store doubles and the off-loop call helper."""

import asyncio
import functools

from app.storage.job_store import JobStore
from app.storage.result_store import CrawlResultStore
from app.storage.watch_store import WatchStore


class LoopGuardJobStore(JobStore):
    """The real store, with every sync entry point refusing to run on the loop."""

    def create(self, root_url, policy):
        _refuse_on_loop("JobStore.create")
        return super().create(root_url, policy)

    def create_single_shot(self, job_id, root_url, policy):
        _refuse_on_loop("JobStore.create_single_shot")
        return super().create_single_shot(job_id, root_url, policy)

    def save(self, job):
        _refuse_on_loop("JobStore.save")
        return super().save(job)

    def set_status(self, job_id, new_status, *, expected=None):
        _refuse_on_loop("JobStore.set_status")
        return super().set_status(job_id, new_status, expected=expected)

    def load(self, job_id):
        _refuse_on_loop("JobStore.load")
        return super().load(job_id)

    def reload_state(self, job_id):
        _refuse_on_loop("JobStore.reload_state")
        return super().reload_state(job_id)

    def delete(self, job_id):
        _refuse_on_loop("JobStore.delete")
        return super().delete(job_id)

    def list_jobs(self):
        _refuse_on_loop("JobStore.list_jobs")
        return super().list_jobs()

    def status_row(self, job_id):
        _refuse_on_loop("JobStore.status_row")
        return super().status_row(job_id)

    def exists(self, job_id):
        _refuse_on_loop("JobStore.exists")
        return super().exists(job_id)

    def close(self):
        _refuse_on_loop("JobStore.close")
        return super().close()


class LoopGuardResultStore(CrawlResultStore):
    """The real result store, with its sync entry points refusing the loop."""

    def put(self, request_id, payload):
        _refuse_on_loop("CrawlResultStore.put")
        return super().put(request_id, payload)

    def get(self, request_id):
        _refuse_on_loop("CrawlResultStore.get")
        return super().get(request_id)

    def payloads(self):
        _refuse_on_loop("CrawlResultStore.payloads")
        return super().payloads()

    def close(self):
        _refuse_on_loop("CrawlResultStore.close")
        return super().close()


class LoopGuardWatchStore(WatchStore):
    """The real watch store, with every sync entry point refusing the loop.

    The sweep is the harder half of this surface: it runs in the background for
    the life of the process, so a sync store call there would park the loop
    that every in-flight request is being answered on, and nothing in the
    request path would say so.
    """

    def create(self, url, /, **kwargs):
        _refuse_on_loop("WatchStore.create")
        return super().create(url, **kwargs)

    def get(self, watch_id):
        _refuse_on_loop("WatchStore.get")
        return super().get(watch_id)

    def list_watches(self):
        _refuse_on_loop("WatchStore.list_watches")
        return super().list_watches()

    def delete(self, watch_id):
        _refuse_on_loop("WatchStore.delete")
        return super().delete(watch_id)

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


async def _off_loop(func, *args, **kwargs):
    """Call a store method the way production must: off the loop."""
    return await asyncio.to_thread(functools.partial(func, *args, **kwargs))


def _refuse_on_loop(name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AssertionError(f"sync store method {name} ran on the event loop; the async twin owns that call")
