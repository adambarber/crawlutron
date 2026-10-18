"""The render/worker/result model surface one owner.
The render-fetch model run is imported by the render endpoint and the browser
transport that builds it -- one owner so no two consumers re-spell the block.
"""

from __future__ import annotations

from app.models.crawl_result import CrawlResult, CrawlStrategyAttempt, EngineCrawlResult
from app.models.crawl_worker import CrawlWorker
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.models.render_options import RenderOptions
from app.models.result_record import RenderEnvelope
from app.models.taxonomy import Classification, CrawlError

__all__ = [
    "Classification",
    "CrawlError",
    "CrawlResult",
    "CrawlStrategyAttempt",
    "CrawlWorker",
    "EngineCrawlResult",
    "HostPolicy",
    "NormalizedUrl",
    "RenderEnvelope",
    "RenderOptions",
]
