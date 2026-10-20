"""The diagnostics record a crawl result carries -- the declared key-set.

``EngineCrawlResult.diagnostics`` is an open disclosure bag; the keys consumers
read are declared here, so a read such as ``elapsed_ms`` is a typed field lookup.
"""

from __future__ import annotations

from typing import Any, TypedDict

from app.models.fetch_envelope import RetryDiagnostics
from app.models.json_value import JsonValue
from app.models.robots_diagnostics import RobotsDiagnostics


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
    request_profile: dict[str, Any]
    decompression_failed: bool
    content_metrics: dict[str, Any]
    cooldown: dict[str, Any]
    discovery: dict[str, Any]
    robots: RobotsDiagnostics
    sitemap_lastmod: dict[str, Any]
    url_budget_seconds: int | float
    next_page_url: str
    js_endpoints: list[dict[str, str]]
    cache: str
    conditional: dict[str, str]
    browser_profile: dict[str, Any]
    frames: list[Any]
    forms: list[Any]
    intercepted_xhr: list[Any]
    screenshot: dict[str, Any] | None
    robots_diag: JsonValue
