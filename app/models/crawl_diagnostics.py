"""The diagnostics record a crawl result carries -- the declared key-set."""

from typing import TypedDict

from app.models.browser_profile_record import BrowserProfileRecord
from app.models.content_metrics import ContentMetrics
from app.models.cooldown_diagnostics import CooldownDiagnostics
from app.models.discovery_diagnostics import DiscoveryDiagnostics
from app.models.fetch_envelope import RetryDiagnostics
from app.models.intercepted_xhr_record import InterceptedXhrRecord
from app.models.json_value import JsonValue
from app.models.request_profile_diagnostics import RequestProfileDiagnostics
from app.models.robots_diagnostics import RobotsDiagnostics
from app.models.screenshot_diagnostics import ScreenshotDiagnostics


class RuntimeDiagnostics(TypedDict, total=False):
    """The ``diagnostics["runtime"]`` block: what the strategy observed."""

    elapsed_ms: int | float
    final_url: str
    error: str


class CrawlDiagnostics(TypedDict, total=False):
    """The result's diagnostics bag: every top-level key a consumer reads.

    Producers may add keys; each added key a consumer reads must be declared
    here, so the bag's read surface is typed end to end (memory #4666).
    """

    retryable: bool
    error_code: str
    circuit_breaker: bool
    runtime: RuntimeDiagnostics
    retry: RetryDiagnostics
    request_profile: RequestProfileDiagnostics
    decompression_failed: bool
    content_metrics: ContentMetrics
    cooldown: CooldownDiagnostics
    discovery: DiscoveryDiagnostics
    robots: RobotsDiagnostics
    sitemap_lastmod: dict[str, str]
    url_budget_seconds: int | float
    next_page_url: str
    js_endpoints: list[dict[str, str]]
    cache: str
    conditional: dict[str, str]
    browser_profile: BrowserProfileRecord
    frames: list[JsonValue]
    forms: list[JsonValue]
    intercepted_xhr: list[InterceptedXhrRecord]
    screenshot: list[ScreenshotDiagnostics]
    robots_diag: JsonValue
