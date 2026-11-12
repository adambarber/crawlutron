"""The worker call shapes the engine dispatches to.

A worker may be a coroutine or plain sync function; callers MUST dispatch on the
callable before invoking (awaiting a non-awaitable was the double-invocation bug).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from crawlutron.models.crawl_job import CrawlJob
from crawlutron.models.crawl_result import CrawlResult

# CrawlWorker is the per-URL worker the engine drives; SchedulerWorker in
# crawlutron.transport.scheduler is the one the domain scheduler drives.
SyncCrawlWorker = Callable[[CrawlJob], CrawlResult]
AsyncCrawlWorker = Callable[[CrawlJob], Awaitable[CrawlResult]]
CrawlWorker = SyncCrawlWorker | AsyncCrawlWorker
