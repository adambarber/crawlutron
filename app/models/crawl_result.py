"""The rendered result of one URL, and the attempt trail it was chosen from.

Two records share this file because they share a shape: an attempt reports how
one strategy went, and the result reports which attempt won, so four of the
result's fields restate an attempt's outcome under the same names and types.
Splitting them made the shared vocabulary two files to keep in step. Collapsing
them into one record -- the result holding the winning attempt rather than a
copy of it -- is the follow-up this pairing points at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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


@dataclass(frozen=True)
class CrawlResult(LiteralChecked):
    url: str
    status: ResultStatus
    classification: Classification
    final_url: str | None
    html: str | None
    text: str | None
    attempts: int
    elapsed_ms: int
    domain: str
    metadata: dict[str, Any] = field(default_factory=dict)
    error: CrawlError | None = None
    # The browser mode that actually served this render (BROWSER_HEADLESS or
    # BROWSER_HEADED_WARMED), or None when no browser strategy produced it. The
    # render endpoint surfaces this on the wire as ``selected_strategy`` so the
    # caller can tell a warmed render from a plain headless one; None rather
    # than a hard-coded member keeps "we did not render this" distinguishable
    # from "we rendered it headless".
    selected_strategy: CrawlStrategyName | None = None
