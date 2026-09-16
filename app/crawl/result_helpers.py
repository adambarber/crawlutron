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
    attempt: CrawlStrategyAttempt,
    http_status: int | None,
    error: str | None,
    selected_strategy: str | None = None,
    headers: dict[str, str] | None = None,
    content_type: str | None = None,
    html: str | None = None,
    text: str | None = None,
    markdown: str | None = None,
    extraction_method: str | None = None,
    quality_score: float = 0.0,
    classification_evidence: list | None = None,
    diagnostics: dict | None = None,
    discovered_link_count: int = 0,
    discovered_links_truncated: bool = False,
    discovered_links: list | None = None,
    next_page_url: str | None = None,
    validators: dict[str, str] | None = None,
) -> EngineCrawlResult:
    """Construct an EngineCrawlResult from a pre-built attempt.

    Many parameters have defaults for the common "empty result" case.
    """
    if headers is None:
        headers = {}
    if classification_evidence is None:
        classification_evidence = []
    if diagnostics is None:
        diagnostics = {}
    if discovered_links is None:
        discovered_links = []
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
        next_page_url=next_page_url,
        validators=validators or {},
    )


def _terminal_engine_result(
    *,
    original_url: str,
    normalized_url: str,
    strategy: str,
    status: str,
    classification: str,
    error: str | None = None,
    diagnostics: dict | None = None,
    http_status: int | None = None,
    final_url: str | None = None,
) -> EngineCrawlResult:
    """Construct a terminal (no payload) EngineCrawlResult with one attempt.

    The three terminal outcomes -- failed, blocked, skipped -- differ only in
    the status string and how the error defaults: a blocked result reports the
    classification itself as the reason and a skipped result never carries a
    final URL or status. The thin wrappers below pin those per-status defaults
    so no call site has to spell them out.
    """
    if error is None:
        error = classification
    attempt = CrawlStrategyAttempt(
        strategy=strategy,  # type: ignore[arg-type]
        status=status,
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
        status=status,
        classification=classification,
        attempt=attempt,
        http_status=http_status,
        error=error,
        diagnostics=diagnostics or {},
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
    """Construct a failed EngineCrawlResult with a single attempt."""
    return _terminal_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=strategy,
        status="failed",
        classification=classification,
        error=error,
        diagnostics=diagnostics,
        http_status=http_status,
        final_url=final_url,
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
    return _terminal_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=strategy,
        status="blocked",
        classification=classification,
        http_status=http_status,
        final_url=final_url,
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
    return _terminal_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=strategy,
        status="skipped",
        classification=classification,
        error=error,
        diagnostics=diagnostics,
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
        attempt=attempt,
        http_status=http_status,
        error="binary_no_text",
        headers=headers,
        content_type=content_type,
        extraction_method="binary_no_text",
        diagnostics={},
    )
