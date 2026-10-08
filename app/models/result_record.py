"""The batch envelope a crawl producer hands back, as a declared record.

An envelope is not a result's serialized form. A result's key set is spelled
once, by its dataclass (``app.models.EngineCrawlResult`` / ``CrawlResult``), and
``app.models.to_dict`` / ``app.models.from_wire`` are the two directions derived
from it. An envelope is the batch around those results: three scalar keys every
producer agrees on, plus whichever result record that producer holds.

It used to also carry a ``TypedDict`` per result shape -- a second spelling of
each dataclass's field list, which needed a detector to hold the two in step and
which is now gone. What is here instead is the one thing a TypedDict is genuinely
good for: a fixed key set that is read in more than one place and whose absence
is a 500. ``/v1/crawl`` and ``/v1/render`` both read these keys, and
``UnifiedCrawlResponse`` drops an unpassed field rather than raising, so a
missing one is silent unless something here names it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NotRequired, TypedDict

from app.models.crawl_result import CrawlResult
from app.models.engine_crawl_result import EngineCrawlResult

AnyResult = EngineCrawlResult | CrawlResult
"""Either result record, for the code that genuinely handles both.

``/v1/crawl`` shapes engine results and ``/v1/render`` shapes browser results;
only the adapter differs. Naming the union keeps that one distinction visible
where it is real instead of collapsing both records back into ``dict[str, Any]``
-- which is the erasure the result dataclasses exist to prevent.
"""


class ProducerEnvelope(TypedDict):
    """What the crawl pipeline hands back: engine results plus the batch's scalars.

    ``webhook_url`` is the one key the two producers disagree about --
    ``app/orchestration/request_pipeline.py`` stamps it when a webhook is configured and
    ``app/api/render.py`` has none -- so it is optional here and the literal is
    checked against this declaration by
    ``test_producer_envelope_declares_exactly_the_keys_its_producers_stamp``.
    """

    request_id: str
    submitted_count: int
    policy_schema_version: str
    results: Sequence[EngineCrawlResult]
    webhook_url: NotRequired[str]


class RenderEnvelope(TypedDict):
    """What the render worker hands back: browser results, same three scalars.

    A separate record from :class:`ProducerEnvelope` rather than one record whose
    ``results`` is a union, because the difference is not incidental -- the two
    endpoints adapt different records, and collapsing them is what forces the
    adapter to be told its element type by an unchecked assertion instead of
    read off the envelope the producer actually built.
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
