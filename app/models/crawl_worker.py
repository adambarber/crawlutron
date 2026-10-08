"""The worker call shapes the engine dispatches to.

The union is honest: a worker may be a coroutine function (the browser
strategies and render-path instances) or a plain synchronous function (some
HTTP/test workers). Callers MUST dispatch on the callable before invoking
(see ``_call_worker``) rather than awaiting the result blindly -- awaiting a
non-awaitable was the original double-invocation bug. The sync/async members
are broken out so the dispatch can narrow without a cast.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.models.crawl_job import CrawlJob
from app.models.crawl_result import CrawlResult

# ``CrawlWorker`` is the per-URL render/strategy worker used by the engine and
# service layers; ``SchedulerWorker`` (defined in app.orchestration.scheduler) is the worker the
# domain scheduler drives, which returns the internal engine result.
SyncCrawlWorker = Callable[[CrawlJob], CrawlResult]
AsyncCrawlWorker = Callable[[CrawlJob], Awaitable[CrawlResult]]
CrawlWorker = SyncCrawlWorker | AsyncCrawlWorker
