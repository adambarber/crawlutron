"""The attempt a result record names -- strategy, status, classification, timing.

None, not a bare ``CrawlStrategyName``: envelopes built after a failure that
preceded any attempt carry "no strategy attempted", serialized as null.
"""

from __future__ import annotations

from dataclasses import dataclass

from crawlutron.models.literal_checked import LiteralChecked
from crawlutron.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from crawlutron.vocabulary.taxonomy import ResultStatus


@dataclass(frozen=True)
class CrawlStrategyAttempt(LiteralChecked):
    strategy: CrawlStrategyName | None
    status: ResultStatus
    classification: Classification
    http_status: int | None
    content_type: str | None
    elapsed_ms: int
    error: CrawlError | None = None
