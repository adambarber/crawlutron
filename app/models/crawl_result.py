"""The result records, and the strategy attempt they record.

A render result (:class:`CrawlResult`) reports what one strategy did to one
URL, so it HOLDS that strategy's attempt rather than restating it. The five
fields the wire still exposes as attributes -- ``status``, ``classification``,
``elapsed_ms``, ``error`` and ``selected_strategy`` -- are properties here,
reaching into the one attempt that holds the answer, so the record and the
attempt cannot disagree about the same outcome.

:class:`EngineCrawlResult` is the record that crosses every boundary in the
system: strategies build it, the engine wraps it in a job envelope, the spill
persists it as JSON, and response shaping reads it back out. It sits beside the
attempt because both records are one family over one vocabulary, and the shared
read that split produced was the duplication. Both directions of its
serialization are DERIVED from this declaration (``app.models.serialization``),
so a field renamed or added moves both at once.

The ``(status, classification)`` agreement check lives on
:class:`LiteralChecked`, which every record carrying both axes already
inherits. A construction chokepoint that had to remember to call a checker let
an attempt built anywhere else validate its two axes separately and never check
that they agreed -- and the record the wire reports is exactly the pair the
attempt carries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.discovered_link import DiscoveredLink
from app.models.literal_checked import LiteralChecked
from app.models.normalized_url import NormalizedUrl
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from app.vocabulary.taxonomy import ResultStatus


@dataclass(frozen=True)
class CrawlStrategyAttempt(LiteralChecked):
    # None, not a bare CrawlStrategyName: envelopes built after a failure that
    # preceded any attempt carry "no strategy attempted", serialized as null.
    strategy: CrawlStrategyName | None
    status: ResultStatus
    classification: Classification
    http_status: int | None
    content_type: str | None
    elapsed_ms: int
    error: CrawlError | None = None


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
        """How long the winning attempt took.

        The same reach-through :class:`EngineCrawlResult` uses for its own
        elapsed time, so the scheduler can ask either record for the number
        without knowing which shape it holds.
        """
        return self.attempt.elapsed_ms

    @property
    def error(self) -> CrawlError | None:
        return self.attempt.error

    @property
    def selected_strategy(self) -> CrawlStrategyName | None:
        """The browser mode that actually served this render.

        ``BROWSER_HEADLESS`` or ``BROWSER_HEADED_WARMED``, or None when no
        browser strategy produced it. The render endpoint surfaces this on
        the wire so the caller can tell a warmed render from a plain headless
        one; None rather than a hard-coded member keeps "we did not render
        this" distinguishable from "we rendered it headless".
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
    diagnostics: dict[str, Any] = field(default_factory=dict)
    discovered_link_count: int = 0
    discovered_links_truncated: bool = False
    discovered_links: list[DiscoveredLink] = field(default_factory=list)
    error: CrawlError | None = None
    markdown: str | None = None
    next_page_url: NormalizedUrl | None = None
    # Origin validators ("etag"/"last_modified") for conditional revalidation;
    # empty when the origin supplied none.
    validators: dict[str, str] = field(default_factory=dict)
    # Page metadata from the engine's discovery tree; None when no tree
    # existed. Persisted in the spill, never on the wire (shaping maps it).
    llm_metadata: dict[str, Any] | None = None

    @property
    def elapsed_ms(self) -> int:
        """How long this result's work took, or 0 when nothing observed it.

        The scheduler is generic over two result shapes and needs this number
        from both, so it is one attribute with one meaning rather than a
        ``getattr`` for a member only one of the two happens to carry. The
        diagnostics block is consulted first because that is what the HTTP and
        browser strategies write, and the attempt is the fallback for a result
        built without diagnostics (the worker-exception and budget-timeout
        envelopes). Both are milliseconds, so no caller has to know which one
        it got.
        """
        runtime = self.diagnostics.get("runtime")
        if isinstance(runtime, dict):
            observed = runtime.get("elapsed_ms")
            if isinstance(observed, int | float):
                return int(observed)
        if self.attempted_strategies:
            return int(self.attempted_strategies[0].elapsed_ms)
        return 0
