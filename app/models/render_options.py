"""How a caller asked for a page to be fetched."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RenderOptions:
    timeout_seconds: int = 30
    max_retries: int = 2
    respect_robots: bool = True
    include_html: bool = True
    include_text: bool = True
    browser_profile: dict[str, Any] = field(default_factory=dict)
