"""One page's trace: what was attempted, and what decided its classification."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from app.constants import CrawlClassification
from app.models.classification_evidence import ClassificationEvidence
from app.models.crawl_status import CrawlStatus
from app.models.crawl_strategy import CrawlStrategy
from app.models.strategy_attempt import StrategyAttempt


class PageTrace(BaseModel):
    """The diagnostic record of one page, as the crawl already wrote it.

    The fields are named as the stored response names them and typed as the
    stored record holds them -- the URL is text here, because the stored payload
    is JSON and re-admitting it would need a policy the result cache does not
    own. So a trace is a projection of the same record the response is, which is
    what makes it incapable of disagreeing with it: one record, read two ways.

    Nothing here is new instrumentation. The spans are the crawl's own
    ``attempted_strategies`` and the evidence its own
    ``classification_evidence``; what a trace leaves out is the page body, which
    is why it is cheap to keep asking for.
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
