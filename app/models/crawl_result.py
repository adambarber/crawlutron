"""The result records a crawl renders or reports -- the render ``CrawlResult`` and

A shared family: both hold a ``CrawlStrategyAttempt`` and the classification and
error vocabularies, so they are declared together here and each stays findable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.crawl_diagnostics import CrawlDiagnostics
from app.models.crawl_strategy_attempt import CrawlStrategyAttempt
from app.models.discovered_link import DiscoveredLink
from app.models.literal_checked import LiteralChecked
from app.models.normalized_url import NormalizedUrl
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from app.vocabulary.taxonomy import ResultStatus


@dataclass(frozen=True)
class CrawlResult(LiteralChecked):
    url: NormalizedUrl
    attempt: CrawlStrategyAttempt
    final_url: NormalizedUrl | None
    html: str | None
    text: str | None
    attempts: int
    domain: str
    metadata: dict[str, Any] = field(default_factory=dict)
    # The caller's submitted text, as the engine record names it; None until a
    # request funnel stamps it, and consumers fall back to the canonical text.
    original_url: str | None = None

    @property
    def status(self) -> ResultStatus:
        return self.attempt.status

    @property
    def classification(self) -> Classification:
        return self.attempt.classification

    @property
    def elapsed_ms(self) -> int:
        """How long the winning attempt took -- the same reach-through the engine
        record uses, so the scheduler can ask either shape for the number without
        knowing which it holds.
        """
        return self.attempt.elapsed_ms

    @property
    def error(self) -> CrawlError | None:
        return self.attempt.error

    @property
    def selected_strategy(self) -> CrawlStrategyName | None:
        """The browser mode that actually served this render -- headless or
        headed-warmed, or None when no browser produced it, keeping "we did not
        render this" distinguishable from "we rendered it headless".
        """
        return self.attempt.strategy


@dataclass(frozen=True)
class EngineCrawlResult(LiteralChecked):
    original_url: str
    normalized_url: NormalizedUrl | None
    final_url: NormalizedUrl | None
    status: ResultStatus
    classification: Classification
    selected_strategy: CrawlStrategyName | None
    attempted_strategies: list[CrawlStrategyAttempt]
    http_status: int | None
    headers: dict[str, str]
    content_type: str | None
    html: str | None
    text: str | None
    extraction_method: str | None
    quality_score: float
    classification_evidence: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: CrawlDiagnostics = field(default_factory=CrawlDiagnostics)
    discovered_link_count: int = 0
    discovered_links_truncated: bool = False
    discovered_links: list[DiscoveredLink] = field(default_factory=list)
    error: CrawlError | None = None
    markdown: str | None = None
    next_page_url: NormalizedUrl | None = None
    # Origin validators ("etag"/"last_modified") for conditional revalidation;
    # empty when the origin supplied none.
    validators: dict[str, str] = field(default_factory=dict)
    # Page metadata from the engine's discovery tree; None when no tree existed.
    # Persisted in the spill, never on the wire (shaping maps it).
    llm_metadata: dict[str, Any] | None = None

    @property
    def elapsed_ms(self) -> int:
        """How long this result's work took, or 0 when nothing observed it -- one
        attribute, since the generic scheduler reads it off both shapes. Diagnostics
        is consulted first (what the strategy writes), the attempt the fallback; ms.
        """
        runtime = self.diagnostics.get("runtime")
        if runtime is not None:
            observed = runtime.get("elapsed_ms")
            if isinstance(observed, int | float):
                return int(observed)
        if self.attempted_strategies:
            return int(self.attempted_strategies[0].elapsed_ms)
        return 0
