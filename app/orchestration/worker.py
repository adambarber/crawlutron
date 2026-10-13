"""Worker dispatch for the crawl strategy pipeline."""

from __future__ import annotations

import asyncio
import inspect
from typing import TypeGuard, overload

from app.models.crawl_job import CrawlJob
from app.models.crawl_result import CrawlResult
from app.models.crawl_worker import AsyncCrawlWorker, CrawlWorker, SyncCrawlWorker


def _is_sync_worker(worker: CrawlWorker) -> TypeGuard[SyncCrawlWorker]:
    return not (inspect.iscoroutinefunction(worker) or inspect.iscoroutinefunction(type(worker).__call__))


def _is_async_worker(worker: CrawlWorker) -> TypeGuard[AsyncCrawlWorker]:
    return inspect.iscoroutinefunction(worker) or inspect.iscoroutinefunction(type(worker).__call__)


async def _invoke_async_worker(worker: AsyncCrawlWorker, job: CrawlJob) -> CrawlResult:
    return await worker(job)


def _invoke_sync_worker(worker: SyncCrawlWorker, job: CrawlJob) -> CrawlResult:
    result = worker(job)
    if inspect.isawaitable(result):
        # it used to surface ~100 lines away as a strategy error -- a browser
        # page's .html on a never-awaited coroutine, as a network_error.
        raise TypeError(
            f"sync worker {worker!r} returned an awaitable; a worker is either a "
            "coroutine function or a plain function returning a CrawlResult"
        )
    return result


@overload
async def call_worker(worker: AsyncCrawlWorker, job: CrawlJob) -> CrawlResult: ...


@overload
async def call_worker(worker: SyncCrawlWorker, job: CrawlJob) -> CrawlResult: ...


async def call_worker(worker: CrawlWorker, job: CrawlJob) -> CrawlResult:
    if _is_async_worker(worker):
        return await _invoke_async_worker(worker, job)
    if _is_sync_worker(worker):
        return await asyncio.to_thread(_invoke_sync_worker, worker, job)
    raise TypeError(f"worker is neither async nor sync: {type(worker)!r}")


__all__ = ["call_worker"]
