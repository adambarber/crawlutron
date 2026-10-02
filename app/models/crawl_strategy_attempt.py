"""One strategy's answer for one URL, as recorded in a result's attempt trail."""

from __future__ import annotations

from dataclasses import dataclass

from app.constants import ResultStatus
from app.models.literal_checked import LiteralChecked
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName


@dataclass(frozen=True)
class CrawlStrategyAttempt(LiteralChecked):
    # Optional, not bare CrawlStrategyName: the worker-exception and
    # budget-timeout envelopes are built after a failure that preceded any
    # strategy attempt, and "no strategy was attempted" is not a strategy. The
    # honest value there is None, and the wire model serializes it as null.
    strategy: CrawlStrategyName | None
    status: ResultStatus
    classification: Classification
    http_status: int | None
    content_type: str | None
    elapsed_ms: int
    error: CrawlError | None = None
