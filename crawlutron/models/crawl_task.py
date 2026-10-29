"""A single URL scheduled for crawling within a crawl request."""

from __future__ import annotations

from dataclasses import dataclass

from crawlutron.models.normalized_url import NormalizedUrl


def scheduler_domain_key(url: NormalizedUrl) -> str:
    """The one canonical scheduler key: the registrable site.

    Host-keyed writers split one site's state in two; every writer and reader
    of scheduler state goes through this function."""
    return url.site


@dataclass
class CrawlTask:
    """Per-job bookkeeping the engine/recursive dispatcher needs.

    Submission index, original URL, and admitted page; ``original_url`` keeps
    the caller's text, not only the admitted form.
    """

    index: int
    original_url: str
    normalized_url: NormalizedUrl

    @property
    def domain(self) -> str:
        """The canonical scheduler key; derived so no caller can mis-key it."""
        return scheduler_domain_key(self.normalized_url)
