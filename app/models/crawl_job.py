"""One unit of work handed to a crawl worker."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.models.render_options import RenderOptions


@dataclass(frozen=True)
class CrawlJob:
    """One admitted URL and everything the worker needs to fetch it.

    ``domain`` is derived, not stored. It used to be a caller-supplied field, and
    a job could carry a ``domain`` that had nothing to do with its own URL --
    which is invisible until a scheduler keys six mechanisms off the pair and
    they disagree about which page the throttle belongs to.
    """

    index: int
    url: NormalizedUrl
    options: RenderOptions
    host_policy: HostPolicy

    @property
    def domain(self) -> str:
        """The registrable site every scheduler component keys on."""
        return self.url.site
