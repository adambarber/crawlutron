"""Job runner task lifecycle: the task registry and its spawn/cancel/drain seams."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from app.storage.job_store import JobStore

logger = logging.getLogger(__name__)


_tasks: dict[str, asyncio.Task] = {}


def is_running(job_id: str) -> bool:
    task = _tasks.get(job_id)
    return task is not None and not task.done()


async def shutdown_job_runner() -> None:
    """Cancel all running job tasks and await their exit."""
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


def reset_job_runner() -> None:
    """Best-effort synchronous teardown for test fixtures (no await needed)."""
    from app.api.job_runner import reset_execute_crawl

    for task in list(_tasks.values()):
        task.cancel()
    _tasks.clear()
    reset_execute_crawl()


def start_tracked_task(
    job_id: str,
    factory: Callable[[], Coroutine[Any, Any, Any]],
) -> asyncio.Task:
    """Spawn ``factory()`` as the task that owns ``job_id``, or return the live one.

    One registry means one shutdown drain and one cancel path. The factory is
    called only once a slot is free, so a job that is already running does not
    leave a coroutine constructed and never awaited.

    Taking a factory rather than a coroutine is what lets the async crawl come
    through here too. Handed to a Starlette background task instead, it was
    awaited inside the request cycle -- so the 202 did not return until the
    crawl finished, and the drain in ``app/main.py`` could not see it either.
    """
    existing = _tasks.get(job_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(factory())
    _tasks[job_id] = task
    task.add_done_callback(lambda t: _tasks.pop(job_id, None))
    return task


def start_job(
    job_id: str,
    job_store: JobStore,
    settings: Any,
    shutdown_event: asyncio.Event,
    *,
    preclaimed: bool = False,
) -> asyncio.Task:
    """Spawn (or return) the runner task; preclaimed rows were flipped by the caller.

    The runner lives in :mod:`app.api.job_runner`; importing it here, at call
    time, breaks the recovery→runner cycle while the leaf registry stays free
    of orchestrator dependencies at import time.
    """
    from app.api.job_runner import _run_job

    return start_tracked_task(
        job_id, lambda: _run_job(job_id, job_store, settings, shutdown_event, preclaimed=preclaimed)
    )


def cancel_job_task(job_id: str) -> None:
    """Cancel a running job task (delete path); state row untouched."""
    task = _tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
