"""The engine's own result record: every strategy's output, in one shape.

This is the record that crosses every boundary in the system: strategies build
it, the engine wraps it in a job envelope, the spill persists it as JSON, and
response shaping reads it back out. It is declared once, here, as a dataclass,
and both directions of serialization are DERIVED from this declaration --
``to_dict`` walks its fields and ``from_wire`` rebuilds from the same field
annotations -- so a field renamed or added moves both at once and there is no
second copy of the key set for a detector to hold in step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.constants import ResultStatus
from app.models.crawl_strategy_attempt import CrawlStrategyAttempt
from app.models.discovered_link import DiscoveredLink
from app.models.literal_checked import LiteralChecked
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName


@dataclass(frozen=True)
class EngineCrawlResult(LiteralChecked):
    original_url: str
    normalized_url: str | None
    final_url: str | None
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
    diagnostics: dict[str, Any] = field(default_factory=dict)
    discovered_link_count: int = 0
    discovered_links_truncated: bool = False
    discovered_links: list[DiscoveredLink] = field(default_factory=list)
    error: CrawlError | None = None
    markdown: str | None = None
    next_page_url: str | None = None
    # Origin validators for the delivered representation ("etag"/"last_modified").
    # Empty when the origin supplied none; surfaced so callers can revalidate
    # conditionally on later requests (freshness monitoring).
    validators: dict[str, str] = field(default_factory=dict)
    # Page metadata (title/og/twitter/JSON-LD) computed once from the engine's
    # shared discovery tree. None = never computed here (no tree existed);
    # shaping then reparses once. A dict is plain JSON data -- persisted in the
    # spill, read by response shaping, and NEVER part of the wire response
    # (build_unified_result maps it into the existing PageMetadata fields).
    llm_metadata: dict[str, Any] | None = None

    @property
    def elapsed_ms(self) -> int:
        """How long this result's work took, or 0 when nothing observed it.

        The scheduler is generic over two result shapes and needs this number
        from both, so it is one attribute with one meaning rather than a
        ``getattr`` for a member only one of the two happens to carry. The
        diagnostics block is consulted first because that is what the HTTP and
        browser strategies have always written, and the attempt is the fallback
        for a result built without diagnostics (the worker-exception and
        budget-timeout envelopes). Both are milliseconds, so no caller has to
        know which one it got.
        """
        runtime = self.diagnostics.get("runtime")
        if isinstance(runtime, dict):
            observed = runtime.get("elapsed_ms")
            if isinstance(observed, int | float):
                return int(observed)
        if self.attempted_strategies:
            return int(self.attempted_strategies[0].elapsed_ms)
        return 0
