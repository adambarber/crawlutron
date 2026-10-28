"""Result-construction vocabulary shared by the engine and transport layers.

The terminal/engine result builders construct the wire models below transport, so
both layers import one owner instead of ``app.transport`` reaching into orchestration."""

from __future__ import annotations

from app.models.classification_evidence import ClassificationEvidenceRecord
from app.models.crawl_diagnostics import CrawlDiagnostics
from app.models.crawl_result import CrawlResult, EngineCrawlResult

# The terminal funnels build the attempt and the wire result from these records.
from app.models.crawl_strategy_attempt import CrawlStrategyAttempt
from app.models.discovered_link import DiscoveredLink
from app.models.json_value import JsonValue
from app.models.normalized_url import NormalizedUrl
from app.models.page_metadata import PageMetadata
from app.models.taxonomy import Classification, CrawlError, CrawlStrategyName
from app.vocabulary.taxonomy import ResultStatus

__all__ = [
    "build_engine_result",
    "binary_no_text_result",
    "terminal_attempt",
    "terminal_crawl_result",
]


def build_engine_result(
    *,
    original_url: str,
    normalized_url: NormalizedUrl,
    final_url: NormalizedUrl | None,
    attempt: CrawlStrategyAttempt,
    http_status: int | None,
    selected_strategy: CrawlStrategyName | None = None,
    headers: dict[str, str] | None = None,
    content_type: str | None = None,
    html: str | None = None,
    text: str | None = None,
    markdown: str | None = None,
    extraction_method: str | None = None,
    quality_score: float = 0.0,
    classification_evidence: list[ClassificationEvidenceRecord] | None = None,
    diagnostics: CrawlDiagnostics | None = None,
    discovered_link_count: int = 0,
    discovered_links_truncated: bool = False,
    discovered_links: list[DiscoveredLink] | None = None,
    next_page_url: NormalizedUrl | None = None,
    validators: dict[str, str] | None = None,
    llm_metadata: PageMetadata | None = None,
) -> EngineCrawlResult:
    """Construct an EngineCrawlResult from a pre-built attempt; status and
    classification are read off ``attempt``, never re-stated by the caller."""
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
        status=attempt.status,
        classification=attempt.classification,
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
        error=attempt.error,
        next_page_url=next_page_url,
        validators=validators or {},
        llm_metadata=llm_metadata,
    )


def terminal_attempt(
    *,
    strategy: CrawlStrategyName | None,
    status: ResultStatus,
    classification: Classification,
    elapsed_ms: int,
    error: CrawlError | None,
) -> CrawlStrategyAttempt:
    """The attempt a terminal result reports -- no HTTP status, body, or content
    type. Public because results built in both layers need the same attempt.
    """
    return CrawlStrategyAttempt(
        strategy=strategy,
        status=status,
        classification=classification,
        http_status=None,
        content_type=None,
        elapsed_ms=elapsed_ms,
        error=error,
    )


def terminal_crawl_result(
    *,
    url: NormalizedUrl,
    status: ResultStatus,
    classification: Classification,
    attempts: int,
    elapsed_ms: int,
    domain: str,
    error: CrawlError | None,
    metadata: dict[str, JsonValue] | None = None,
    selected_strategy: CrawlStrategyName | None = None,
) -> CrawlResult:
    """Construct a terminal ``CrawlResult`` (no payload) with a stable error code.

    ``error`` must be a taxonomy member, never raw exception text; ``status`` the
    taxonomy verdict.
    """
    return CrawlResult(
        url=url,
        attempt=terminal_attempt(
            strategy=selected_strategy,
            status=status,
            classification=classification,
            elapsed_ms=elapsed_ms,
            error=error,
        ),
        final_url=None,
        html=None,
        text=None,
        attempts=attempts,
        domain=domain,
        metadata=metadata or {},
    )


def binary_no_text_result(
    *,
    original_url: str,
    normalized_url: NormalizedUrl,
    final_url: NormalizedUrl | None,
    strategy: CrawlStrategyName | None,
    http_status: int | None,
    headers: dict[str, str],
    content_type: str | None,
    elapsed_ms: int,
) -> EngineCrawlResult:
    """Construct a binary/no-text EngineCrawlResult."""
    attempt = CrawlStrategyAttempt(
        strategy=strategy,
        status="failed",
        classification="binary_no_text",
        http_status=http_status,
        content_type=content_type,
        elapsed_ms=elapsed_ms,
        error="binary_no_text",
    )
    return build_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        final_url=final_url,
        attempt=attempt,
        http_status=http_status,
        headers=headers,
        content_type=content_type,
        extraction_method="binary_no_text",
        diagnostics=CrawlDiagnostics(),
    )
