"""One unit of work handed to a crawl worker."""

from __future__ import annotations

from dataclasses import dataclass

from crawlutron.models.crawl_task import scheduler_domain_key
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.render_options import RenderOptions


@dataclass(frozen=True)
class CrawlJob:
    """One admitted URL and everything the worker needs to fetch it.

    ``domain`` is derived, not stored: a caller-supplied field could disagree with
    the URL, silently mis-keying the scheduler mechanisms that key off the pair.
    """

    index: int
    url: NormalizedUrl
    options: RenderOptions
    host_policy: HostPolicy

    @property
    def domain(self) -> str:
        """The canonical scheduler key; derived so no caller can mis-key it."""
        return scheduler_domain_key(self.url)
