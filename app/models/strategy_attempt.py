from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from app.models.crawl_status import CrawlStatus
from app.models.crawl_strategy import CrawlStrategy
from app.vocabulary.taxonomy import CrawlClassification


class StrategyAttempt(BaseModel):
    # Optional: an envelope built after a worker raised before any strategy ran
    # records None ("no strategy attempted") rather than fabricating a member.
    strategy: CrawlStrategy | None = Field(
        description="Strategy attempted for this URL, or null when none was."
    )
    status: CrawlStatus = Field(description="Attempt-level status.")
    classification: CrawlClassification = Field(description="Attempt-level classification.")
    http_status: Annotated[
        int | None,
        Field(
            description="HTTP status observed by this strategy, when available.",
        ),
    ] = None
    content_type: Annotated[str | None, Field(description="Observed content type, when available.")] = None
    elapsed_ms: int = Field(description="Attempt elapsed time in milliseconds.")
    error: Annotated[
        str | None,
        Field(
            description="Stable machine-readable error detail for this attempt, if any.",
        ),
    ] = None
