"""Worker dispatch for the crawl strategy pipeline."""

from __future__ import annotations

import asyncio
import inspect
from typing import TypeGuard, overload

from app.models import AsyncCrawlWorker, CrawlJob, CrawlResult, CrawlWorker, SyncCrawlWorker


def _is_sync_worker(worker: CrawlWorker) -> TypeGuard[SyncCrawlWorker]:
    return not (inspect.iscoroutinefunction(worker) or inspect.iscoroutinefunction(type(worker).__call__))


def _is_async_worker(worker: CrawlWorker) -> TypeGuard[AsyncCrawlWorker]:
    return inspect.iscoroutinefunction(worker) or inspect.iscoroutinefunction(type(worker).__call__)


async def _invoke_async_worker(worker: AsyncCrawlWorker, job: CrawlJob) -> CrawlResult:
    return await worker(job)


def _invoke_sync_worker(worker: SyncCrawlWorker, job: CrawlJob) -> CrawlResult:
    return worker(job)


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
