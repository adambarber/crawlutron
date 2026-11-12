"""How a caller asked for a page to be fetched."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from crawlutron.models.browser_profile_record import BrowserProfileRecord


@dataclass(frozen=True)
class RenderOptions:
    timeout_seconds: int = 30
    max_retries: int = 2
    respect_robots: bool = True
    include_html: bool = True
    include_text: bool = True
    browser_profile: BrowserProfileRecord = field(default_factory=lambda: cast(BrowserProfileRecord, {}))
