from __future__ import annotations

import asyncio
import logging
import socket
import ssl

import httpx

from app.crawl.decompression import DecompressionTooLargeError
from app.models import BrowserPoolExhaustedError, CrawlStrategyAttempt, EngineCrawlResult

logger = logging.getLogger(__name__)


def _classify_error_code(exc: BaseException) -> str:
    """Map an internal exception to a stable, non-leaking error code.

    Raw exception text can leak DNS hostnames, internal filesystem paths, or
    stack detail. Callers must never see ``str(exc)``; these codes are safe to
    surface in user-facing ``error``/``diagnostics`` fields.

    The classifier is strategy-neutral and lives here (not inside a strategy
    module) because both the HTTP and browser strategies classify through it:
    housing it in one strategy would make the other reach across module
    boundaries for a shared taxonomy decision.

    ``browser_pool_exhausted`` is kept distinct from the transport errors: it
    means the URL was never attempted because renderer capacity was
    unavailable, so it is a retry candidate, not a page failure.
    """
    if isinstance(exc, BrowserPoolExhaustedError):
        return "browser_pool_exhausted"
    if isinstance(exc, DecompressionTooLargeError):
        return "payload_too_large"
    if isinstance(exc, httpx.TooManyRedirects):
        return "too_many_redirects"
    if isinstance(
        exc,
        (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
            httpx.ProtocolError,
            socket.gaierror,
            socket.herror,
            ConnectionError,
            ConnectionRefusedError,
            ConnectionResetError,
            TimeoutError,
            asyncio.TimeoutError,
        ),
    ):
        return "connection_error"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_status_error"
    if isinstance(exc, httpx.HTTPError):
        return "http_error"
    if isinstance(exc, ValueError):
        return "value_error"
    return "network_error"


def _engine_result_from_exception(
    *,
    error: BaseException,
    original_url: str,
    normalized_url: str,
    strategy: str,
    elapsed_ms: int | None = None,
    extra_diagnostics: dict | None = None,
    known_classification: str | None = None,
) -> EngineCrawlResult:
    """Convert ANY strategy exception into a classified terminal result.

    This is the single chokepoint for "a strategy raised": the HTTP strategy's
    broad-except sink, the browser strategy's outer guard, the engine's
    budget-timeout path, and the scheduler's worker-exception recovery all
    route through here, so an exception can never become an ad-hoc result
    shape (or vanish entirely) at one of them.

    Raw exception text is logged server-side only and never reaches the wire
    (it can leak DNS names, paths, or stack detail); the caller sees the stable
    code.

    ``known_classification`` is for the one case where the failure mode is not
    derived from the exception type at all -- the per-URL wall-clock budget,
    which raises bare ``TimeoutError`` and whose real classification is
    ``url_budget_exceeded``. It must be a taxonomy member.
    """
    classification = known_classification if known_classification is not None else _classify_error_code(error)
    elapsed = elapsed_ms if elapsed_ms is not None else 0
    logger.warning(
        "strategy_result_error",
        extra={
            "strategy": strategy,
            "error_code": classification,
            "error_detail": str(error),
        },
    )
    # Pool exhaustion is a capacity condition, not a dead page: the URL was
    # never attempted. Flag it so the frontier layer can re-queue rather than
    # record a permanent failure.
    retryable = classification == "browser_pool_exhausted"
    diagnostics = {"retryable": retryable, "error_code": classification}
    # Callers may merge strategy-specific diagnostics that are ALREADY
    # redacted (e.g. the HTTP path's request_profile). The primitive owns
    # classification and raw-text redaction; it must not silently drop
    # diagnostics a caller built under the redaction contract.
    if extra_diagnostics:
        diagnostics.update(extra_diagnostics)
    return _terminal_engine_result(
        original_url=original_url,
        normalized_url=normalized_url,
        strategy=strategy,
        status="failed",
        classification=classification,
        error=classification,
        http_status=None,
        diagnostics=diagnostics,
        attempt_elapsed_ms=elapsed,
    )



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
    attempt_elapsed_ms: int = 0,
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
        elapsed_ms=attempt_elapsed_ms,
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
