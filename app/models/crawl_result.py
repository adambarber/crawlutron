"""The rendered result of one URL, as the render path reports it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.constants import ResultStatus
from app.models.literal_checked import LiteralChecked
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName


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
