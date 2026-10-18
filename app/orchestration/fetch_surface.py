"""The fetch/worker/model surface the engine, its strategy chain, and the crawl
harness jointly consume -- one owner of the run so none re-spells the model block.
The models stay single-home; this module re-exports them at that boundary.
"""

from __future__ import annotations

from app.models.crawl_result import EngineCrawlResult
from app.models.crawl_worker import CrawlWorker
from app.models.host_policy import HostPolicy
from app.models.http_fetcher import HttpFetcher
from app.models.normalized_url import NormalizedUrl
from app.models.result_record import ProducerEnvelope
from app.runtime.resource_pressure import ResourceGate
from app.transport.cache import HttpCache
from app.transport.content_cache import ContentCache
from app.transport.fetchers import default_http_fetcher, impersonating_fetcher

__all__ = [
    "CrawlWorker",
    "EngineCrawlResult",
    "HostPolicy",
    "HttpCache",
    "HttpFetcher",
    "NormalizedUrl",
    "ProducerEnvelope",
    "ResourceGate",
    "ContentCache",
    "default_http_fetcher",
    "impersonating_fetcher",
]
