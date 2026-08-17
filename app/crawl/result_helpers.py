"""Shared result construction helpers for crawl strategies.

Centralizes repetitive EngineCrawlResult and CrawlStrategyAttempt construction
patterns to eliminate code duplication (jscpd).
"""

from app.models import CrawlStrategyAttempt, EngineCrawlResult


def _make_engine_result(
    *,
    original_url: str,
    normalized_url: str,
    final_url: str | None,
    status: str,
    classification: str,
    selected_strategy: str | None,
    attempt: CrawlStrategyAttempt,
    http_status: int | None,
    headers: dict[str, str],
    content_type: str | None,
    html: str | None,
    text: str | None,
    markdown: str | None,
    extraction_method: str | None,
    quality_score: float,
    classification_evidence: list,
    diagnostics: dict,
    discovered_link_count: int,
    discovered_links_truncated: bool,
    discovered_links: list,
    error: str | None,
) -> EngineCrawlResult:
    """Construct an EngineCrawlResult from a pre-built attempt."""
    return EngineCrawlResult(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=final_url,
        status=status,
        classification=classification,
        selected_strategy=selected_strategy,
        attempted_strategies=[attempt],
        http_status=http_status,
        headers=headers,
        content_type=content_type,
        html=html,
        text=text,
        markdown=markdown,
        extraction_method=extraction_method,
        quality_score=quality_score,
        classification_evidence=classification_evidence,
        diagnostics=diagnostics,
        discovered_link_count=discovered_link_count,
        discovered_links_truncated=discovered_links_truncated,
        discovered_links=discovered_links,
        error=error,
    )


def _failed_engine_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: str,
    classification: str,
    error: str,
    diagnostics: dict | None = None,
    http_status: int | None = None,
    final_url: str | None = None,
) -> EngineCrawlResult:
    """Construct a failed/blocked EngineCrawlResult with a single attempt."""
    attempt = CrawlStrategyAttempt(
        strategy=strategy,  # type: ignore[arg-type]
        status="failed",
        classification=classification,
        http_status=http_status,
        content_type=None,
        elapsed_ms=0,
        error=error,
    )
    return _make_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=final_url,
        status="failed",
        classification=classification,
        selected_strategy=None,
        attempt=attempt,
        http_status=http_status,
        headers={},
        content_type=None,
        html=None,
        text=None,
        markdown=None,
        extraction_method=None,
        quality_score=0.0,
        classification_evidence=[],
        diagnostics=diagnostics or {},
        discovered_link_count=0,
        discovered_links_truncated=False,
        discovered_links=[],
        error=error,
    )


def _blocked_engine_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: str,
    classification: str,
    http_status: int | None = None,
    final_url: str | None = None,
) -> EngineCrawlResult:
    """Construct a blocked EngineCrawlResult with a single attempt (status=blocked)."""
    attempt = CrawlStrategyAttempt(
        strategy=strategy,  # type: ignore[arg-type]
        status="blocked",
        classification=classification,
        http_status=http_status,
        content_type=None,
        elapsed_ms=0,
        error=classification,
    )
    return _make_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=final_url,
        status="blocked",
        classification=classification,
        selected_strategy=None,
        attempt=attempt,
        http_status=http_status,
        headers={},
        content_type=None,
        html=None,
        text=None,
        markdown=None,
        extraction_method=None,
        quality_score=0.0,
        classification_evidence=[],
        diagnostics={},
        discovered_link_count=0,
        discovered_links_truncated=False,
        discovered_links=[],
        error=classification,
    )


def _skipped_engine_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: str,
    classification: str,
    error: str,
    diagnostics: dict | None = None,
) -> EngineCrawlResult:
    """Construct a skipped EngineCrawlResult (e.g., robots_disallowed)."""
    attempt = CrawlStrategyAttempt(
        strategy=strategy,  # type: ignore[arg-type]
        status="skipped",
        classification=classification,
        http_status=None,
        content_type=None,
        elapsed_ms=0,
        error=error,
    )
    return _make_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=None,
        status="skipped",
        classification=classification,
        selected_strategy=None,
        attempt=attempt,
        http_status=None,
        headers={},
        content_type=None,
        html=None,
        text=None,
        markdown=None,
        extraction_method=None,
        quality_score=0.0,
        classification_evidence=[],
        diagnostics=diagnostics or {},
        discovered_link_count=0,
        discovered_links_truncated=False,
        discovered_links=[],
        error=error,
    )


def _binary_no_text_result(
    *,
    original_url: str,
    normalized_url: str,
    final_url: str | None,
    strategy: str,
    http_status: int | None,
    headers: dict[str, str],
    content_type: str | None,
    elapsed_ms: int,
) -> EngineCrawlResult:
    """Construct a binary/no-text EngineCrawlResult."""
    attempt = CrawlStrategyAttempt(
        strategy=strategy,  # type: ignore[arg-type]
        status="failed",
        classification="binary_no_text",
        http_status=http_status,
        content_type=content_type,
        elapsed_ms=elapsed_ms,
        error="binary_no_text",
    )
    return _make_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=final_url,
        status="failed",
        classification="binary_no_text",
        selected_strategy=None,
        attempt=attempt,
        http_status=http_status,
        headers=headers,
        content_type=content_type,
        html=None,
        text=None,
        markdown=None,
        extraction_method="binary_no_text",
        quality_score=0.0,
        classification_evidence=[],
        diagnostics={},
        discovered_link_count=0,
        discovered_links_truncated=False,
        discovered_links=[],
        error="binary_no_text",
    )
