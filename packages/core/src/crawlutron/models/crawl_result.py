"""The result records a crawl renders or reports -- the render ``CrawlResult`` and

A shared family: both hold a ``CrawlStrategyAttempt`` and the classification and
error vocabularies, so they are declared together here and each stays findable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from safe_url import NormalizedUrl

from crawlutron.models.classification_evidence import ClassificationEvidenceRecord
from crawlutron.models.crawl_diagnostics import CrawlDiagnostics
from crawlutron.models.crawl_strategy_attempt import CrawlStrategyAttempt
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.models.json_value import JsonValue
from crawlutron.models.literal_checked import LiteralChecked
from crawlutron.models.page_metadata import PageMetadata
from crawlutron.models.taxonomy import (
    Classification,
    CrawlError,
    CrawlStrategyName,
)
from crawlutron.vocabulary.taxonomy import ResultStatus


@dataclass(frozen=True)
class CrawlResult(LiteralChecked):
    url: NormalizedUrl
    attempt: CrawlStrategyAttempt
    final_url: NormalizedUrl | None
    html: str | None
    text: str | None
    attempts: int
    domain: str
    # Two producers write this record (the engine's diagnostics passthrough, the
    # browser worker's metadata disclosure), so its key set is open JSON by design.
    metadata: dict[str, JsonValue] = field(default_factory=dict)
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
    attempted_strategies: tuple[CrawlStrategyAttempt, ...]
    http_status: int | None
    headers: Mapping[str, str]
    content_type: str | None
    html: str | None
    text: str | None
    extraction_method: str | None
    quality_score: float
    classification_evidence: tuple[ClassificationEvidenceRecord, ...] = ()
    diagnostics: CrawlDiagnostics = field(default_factory=CrawlDiagnostics)
    discovered_link_count: int = 0
    discovered_links_truncated: bool = False
    discovered_links: tuple[DiscoveredLink, ...] = ()
    error: CrawlError | None = None
    markdown: str | None = None
    next_page_url: NormalizedUrl | None = None
    # Origin validators ("etag"/"last_modified") for conditional revalidation;
    # empty when the origin supplied none.
    validators: Mapping[str, str] = field(default_factory=dict)
    # Page metadata from the engine's discovery tree; None when no tree existed.
    # Persisted in the spill, never on the wire (shaping maps it).
    llm_metadata: PageMetadata | None = None

    def __post_init__(self) -> None:
        """Coerce the collection fields to their immutable forms at the one
        construction edge, so ``replace`` and direct construction cannot sneak
        a mutable list or dict past the frozen record (#603)."""
        object.__setattr__(self, "attempted_strategies", tuple(self.attempted_strategies))
        object.__setattr__(self, "classification_evidence", tuple(self.classification_evidence))
        object.__setattr__(self, "discovered_links", tuple(self.discovered_links))
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))
        object.__setattr__(self, "validators", MappingProxyType(dict(self.validators)))
        super().__post_init__()

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
