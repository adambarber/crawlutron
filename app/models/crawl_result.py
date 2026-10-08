"""The rendered result of one URL, and the attempt it was rendered by.

A render result reports what one strategy did to one URL, so it HOLDS that
strategy's attempt rather than restating it. Five fields used to be copied out
of it -- ``status``, ``classification``, ``elapsed_ms``, ``error`` and
``selected_strategy`` -- which meant the result and the attempt could disagree
about the same outcome: two records, each with its own copy of the answer, and
nothing that made them agree. They are properties here, reaching into the one
attempt that holds the answer, and the field names are unchanged so every reader
sees the same attributes it saw before.

The ``(status, classification)`` agreement check moved with them, onto
:class:`LiteralChecked`, which every record carrying both axes already
inherits. It was a function the construction chokepoint had to remember to call,
so an attempt built anywhere else validated its two axes separately and never
checked that they agreed -- and the record the wire reports is exactly the pair
the attempt carries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.literal_checked import LiteralChecked
from app.models.normalized_url import NormalizedUrl
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from app.vocabulary.taxonomy import ResultStatus


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
    # The caller's submitted text, as the engine record carries it under the
    # same name. None until the render pipeline's receive funnel stamps it;
    # results built outside a request funnel keep None and their consumers
    # fall back to the page's canonical text.
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
        browser strategy produced it. The render endpoint surfaces this on the
        wire so the caller can tell a warmed render from a plain headless one;
        None rather than a hard-coded member keeps "we did not render this"
        distinguishable from "we rendered it headless".
        """
        return self.attempt.strategy
