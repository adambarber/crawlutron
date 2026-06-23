from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ResultStatus = Literal["success", "blocked", "disallowed", "retry_exhausted", "failed"]
Classification = Literal[
    "rendered",
    "captcha_detected",
    "bot_challenge_detected",
    "access_denied",
    "login_required",
    "paywall_detected",
    "rate_limited",
    "not_found",
    "site_error",
    "gone",
    "binary_no_text",
    "robots_disallowed",
    "navigation_timeout",
    "browser_crash",
    "network_error",
    "tls_error",
    "dns_error",
    "invalid_url",
    "unsupported_scheme",
    "private_network_target",
    "unknown_error",
]

CrawlStrategyName = Literal[
    "HTTP_DECLARED_CRAWLER",
    "HTTP_SITE_COMPAT",
    "BROWSER_HEADLESS",
    "BROWSER_HEADED_WARMED",
    "DOCUMENT_EXTRACT",
    "TRAFILATURA_EXTRACT",
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


@dataclass(frozen=True)
class HttpFetchResult:
    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int


@dataclass(frozen=True)
class ClassificationEvidence:
    classification: str
    signature: str
    source: str
    context: str


@dataclass(frozen=True)
class CrawlStrategyAttempt:
    strategy: CrawlStrategyName
    status: ResultStatus
    classification: Classification
    http_status: int | None
    content_type: str | None
    elapsed_ms: int
    error: str | None = None


@dataclass(frozen=True)
class DiscoveredLink:
    url: str
    normalized_url: str
    text: str
    rel: list[str]
    source_url: str
    host: str
    is_same_host: bool


@dataclass(frozen=True)
class UniversalCrawlResult:
    original_url: str
    normalized_url: str | None
    final_url: str | None
    status: ResultStatus
    classification: Classification
    selected_strategy: CrawlStrategyName | None
    attempted_strategies: list[CrawlStrategyAttempt]
    http_status: int | None
    headers: dict[str, str]
    content_type: str | None
    html: str | None
    text: str | None
    extraction_method: str | None
    quality_score: float
    classification_evidence: list[ClassificationEvidence] = field(default_factory=list)
    discovered_link_count: int = 0
    discovered_links_truncated: bool = False
    discovered_links: list[DiscoveredLink] = field(default_factory=list)
    error: str | None = None
