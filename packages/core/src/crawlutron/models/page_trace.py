"""One page's trace: what was attempted, and what decided its classification."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from crawlutron.models.classification_evidence import ClassificationEvidence
from crawlutron.models.crawl_status import CrawlStatus
from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron.models.strategy_attempt import StrategyAttempt
from crawlutron.vocabulary.taxonomy import CrawlClassification


class PageTrace(BaseModel):
    """The diagnostic record of one page, as the crawl already wrote it.

    A projection of the same record the response is (fields named and typed as stored,
    spans reusing the crawl's own attempts/evidence), with only the page body omitted.
    """

    normalized_url: str = Field(description="Canonical URL of the page this trace describes.")
    status: CrawlStatus = Field(description="Page-level status.")
    classification: CrawlClassification = Field(description="Page-level classification.")
    selected_strategy: Annotated[
        CrawlStrategy | None,
        Field(description="Strategy whose result was kept, or null when none produced one."),
    ] = None
    attempted_strategies: list[StrategyAttempt] = Field(
        description="Every strategy attempted for this URL, in order."
    )
    classification_evidence: list[ClassificationEvidence] = Field(
        description="Signatures that decided the classification."
    )
