"""A single URL scheduled for crawling within a crawl request."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.normalized_url import NormalizedUrl


@dataclass
class CrawlTask:
    """Per-job bookkeeping the engine/recursive dispatcher needs.

    Submission index, original URL, admitted page, and the registrable domain for
    scheduling/robots; ``original_url`` keeps the caller's text, not only the admitted form.
    """

    index: int
    original_url: str
    normalized_url: NormalizedUrl
    domain: str
