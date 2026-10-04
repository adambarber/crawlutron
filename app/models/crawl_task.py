"""A single URL scheduled for crawling within a crawl request."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.normalized_url import NormalizedUrl


@dataclass
class CrawlTask:
    """Carries the per-job bookkeeping the engine/recursive dispatcher needs.

    Submission index for result ordering, the original URL, the admitted
    page, and the registrable domain used for per-domain scheduling and
    robots scoping. ``original_url`` stays the caller's text: the task must
    remember what was asked for, not only what was admitted.
    """

    index: int
    original_url: str
    normalized_url: NormalizedUrl
    domain: str
