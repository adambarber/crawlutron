"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.async_crawl_runner import (
    async_crawl_semaphore as async_crawl_semaphore,
)
from crawlutron.service.async_crawl_runner import (
    run_async_crawl as run_async_crawl,
)
