"""One unit of work handed to a crawl worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.models.render_options import RenderOptions

if TYPE_CHECKING:
    from app.validation import HostPolicy


@dataclass(frozen=True)
class CrawlJob:
    index: int
    url: str
    domain: str
    options: RenderOptions
    host_policy: HostPolicy
