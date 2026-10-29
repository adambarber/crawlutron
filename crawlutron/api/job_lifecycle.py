"""Job runner task lifecycle: the task registry and its spawn/cancel/drain seams."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)


_tasks: dict[str, asyncio.Task] = {}


def is_running(job_id: str) -> bool:
    task = _tasks.get(job_id)
    return task is not None and not task.done()


def tracked_task(job_id: str) -> asyncio.Task | None:
    """The live task registered for ``job_id``, or ``None`` when none/finished.

    Lets a supervisor await the exact task owning a job instead of polling."""
    task = _tasks.get(job_id)
    return task if task is not None and not task.done() else None


async def shutdown_job_runner() -> None:
    """Cancel all running job tasks and await their exit, then drain webhooks."""
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
    # A cancellation can land after a job's terminal write but before its webhook
    # POST; drain the shielded deliveries so that callback is never dropped at exit.
    from crawlutron.api.webhooks import drain_webhook_deliveries

    await drain_webhook_deliveries()


def cancel_all_tasks() -> None:
    """Cancel every registered task and clear the registry, without awaiting.

    The registry-owned teardown primitive; ``crawlutron.api.job_runner`` layers its
    execute-crawl reset on top of this; that reset moved there.
    """
    for task in list(_tasks.values()):
        task.cancel()
    _tasks.clear()


def start_tracked_task(
    job_id: str,
    factory: Callable[[], Coroutine[Any, Any, Any]],
) -> asyncio.Task:
    """Spawn ``factory()`` as the task owning ``job_id``, or return live one.

    One registry means one drain and one cancel path; the factory runs only
    once a slot is free."""
    existing = _tasks.get(job_id)
    if existing is not None and not existing.done():
        return existing
    holder: list[asyncio.Task] = []

    def release(t: asyncio.Task) -> None:
        # Only this task's completion may reclaim the slot: a stale
        # callback from a predecessor must not evict the live replacement.
        if holder and _tasks.get(job_id) is t:
            _tasks.pop(job_id, None)

    task = asyncio.create_task(_track_then_run(holder, factory))
    holder.append(task)
    _tasks[job_id] = task
    task.add_done_callback(release)
    return task


async def _track_then_run(
    holder: list[asyncio.Task],
    factory: Callable[[], Coroutine[Any, Any, Any]],
) -> object:
    """Yield once so the spawn site registers the task before any user code.

    Registration precedes the first ``await`` of ``factory``, so an early
    failure can never land in a task the registry never saw."""
    await asyncio.sleep(0)
    return await factory()


def cancel_job_task(job_id: str) -> None:
    """Cancel a running job task (delete path); state row untouched."""
    task = _tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
