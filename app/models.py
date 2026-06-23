from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ResultStatus = Literal["success", "blocked", "disallowed", "retry_exhausted", "failed"]
Classification = Literal[
    "rendered",
    "captcha_detected",
    "access_denied",
    "login_required",
    "paywall_detected",
    "rate_limited",
    "robots_disallowed",
    "navigation_timeout",
    "browser_crash",
    "network_error",
    "tls_error",
    "dns_error",
    "invalid_url",
    "unsupported_scheme",
    "unknown_error",
]


@dataclass(frozen=True)
class RenderOptions:
    timeout_seconds: int = 30
    wait_until: str = "network_idle_or_timeout"
    max_retries: int = 2
    respect_robots: bool = True
    include_html: bool = True
    include_text: bool = True


@dataclass(frozen=True)
class CrawlJob:
    index: int
    url: str
    domain: str
    options: RenderOptions


@dataclass(frozen=True)
class CrawlResult:
    url: str
    status: ResultStatus
    classification: Classification
    final_url: str | None
    html: str | None
    text: str | None
    attempts: int
    elapsed_ms: int
    domain: str
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
