"""One unit of work handed to a crawl worker."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.models.render_options import RenderOptions


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
        """The registrable site every scheduler component keys on."""
        return self.url.site
