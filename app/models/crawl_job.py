"""One unit of work handed to a crawl worker."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.host_policy import HostPolicy
from app.models.render_options import RenderOptions


@dataclass(frozen=True)
class CrawlJob:
    index: int
    url: str
    domain: str
    options: RenderOptions
    host_policy: HostPolicy
