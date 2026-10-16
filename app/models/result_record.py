"""The batch envelope a crawl producer hands back: the three scalar keys every
producer agrees on, plus the result record it holds -- an envelope is not a
result's serialized form, so its keys are named here rather than silently dropped.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NotRequired, TypedDict

from app.models.crawl_result import CrawlResult, EngineCrawlResult

AnyResult = EngineCrawlResult | CrawlResult
"""Either result record, for the code that genuinely handles both.

``/v1/crawl`` shapes engine results and ``/v1/render`` shapes browser results;
only the adapter differs. Naming the union keeps that one distinction visible
where it is real instead of collapsing both records back into ``dict[str, Any]``
-- which is the erasure the result dataclasses exist to prevent.
"""


class ProducerEnvelope(TypedDict):
    """What the crawl pipeline hands back: engine results plus the batch's scalars.
    ``webhook_url`` is optional -- the pipeline stamps it, the render path has
    none -- so a literal check holds both producers to this declaration.
    """

    request_id: str
    submitted_count: int
    policy_schema_version: str
    results: Sequence[EngineCrawlResult]
    webhook_url: NotRequired[str]


class RenderEnvelope(TypedDict):
    """What the render worker hands back: browser results, same three scalars. A
    separate record (not one union field) because the two endpoints adapt
    different records -- collapsing them would force an unchecked assertion.
    """

    request_id: str
    submitted_count: int
    policy_schema_version: str
    results: Sequence[CrawlResult]


__all__ = [
    "AnyResult",
    "ProducerEnvelope",
    "RenderEnvelope",
]
