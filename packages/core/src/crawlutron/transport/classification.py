"""What a failure MEANS: the exception-to-classification mapping, declared once
because engine and render both classify through it. The table is ordered and the
order is semantic -- a subclass row must precede its catch-all.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl

import httpx
from curl_cffi.requests.exceptions import CertificateVerifyError
from curl_cffi.requests.exceptions import SSLError as CffiSSLError
from safe_url.url_redaction import redact_urls_in_text

from crawlutron.models.browser_pool_exhausted import BrowserPoolExhaustedError
from crawlutron.models.taxonomy import Classification, CrawlStrategyName
from crawlutron.transport.decompression import DecompressionError, DecompressionTooLargeError
from crawlutron.transport.pinned_dns import ResolverBudgetExceeded
from crawlutron.transport.scheduler_exceptions import CircuitBreakerOpen
from crawlutron.vocabulary.taxonomy import is_transient_classification

_logger = logging.getLogger(__name__)

__all__ = ["CLASSIFICATION_BY_EXCEPTION", "classify_and_log", "classify_error_code"]


CLASSIFICATION_BY_EXCEPTION: tuple[tuple[tuple[type[BaseException], ...], Classification], ...] = (
    ((BrowserPoolExhaustedError,), Classification.browser_pool_exhausted),
    # Capacity, not a page failure: the URL was never attempted, which is why it
    # stays distinct from the transport rows below and stays retryable.
    ((CircuitBreakerOpen,), Classification.circuit_breaker_open),
    ((DecompressionTooLargeError,), Classification.payload_too_large),
    # The declared encoding did not decode; its ValueError base is a zlib
    # detail, so without this row a transient fault reads as terminal.
    ((DecompressionError,), Classification.http_error),
    ((httpx.TooManyRedirects,), Classification.too_many_redirects),
    # Name resolution never got off the host; without this row it read as
    # "connection_error" and the declared dns_error member had no producer.
    ((socket.gaierror, socket.herror), Classification.dns_error),
    # The bounded resolver's own verdict: the resolve outlasted the
    # request's budget -- a resolution failure, not a navigation or connection one.
    ((ResolverBudgetExceeded,), Classification.dns_error),
    (
        (
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.WriteError,
            httpx.CloseError,
            httpx.RemoteProtocolError,
            httpx.ProtocolError,
            ConnectionError,
            ConnectionRefusedError,
            ConnectionResetError,
            TimeoutError,
            asyncio.TimeoutError,
        ),
        Classification.connection_error,
    ),
    ((httpx.HTTPStatusError,), Classification.http_status_error),
    ((httpx.HTTPError,), Classification.http_error),
    # curl_cffi's TLS failures are classified tls_error too -- they share
    # the row below; it must precede the OSError row (SSLError's base IS OSError).
    ((ssl.SSLError, CffiSSLError, CertificateVerifyError), Classification.tls_error),
    ((ValueError,), Classification.value_error),
    # OSError is the base of every network fault (a degraded resolver raises bare
    # timeout/OSError); the table's closing catch-all keeps every verdict terminal.
    ((OSError,), Classification.connection_error),
)


def classify_error_code(exc: BaseException) -> Classification:
    """Map an internal exception to a stable, non-leaking error code -- raw
    exception text can leak DNS paths, so these codes are safe to surface;
    strategy-neutral, every row a taxonomy member.
    """
    for types, classification in CLASSIFICATION_BY_EXCEPTION:
        if isinstance(exc, types):
            return classification
    return Classification.network_error


def classify_and_log(
    error: BaseException,
    *,
    event: str,
    strategy: CrawlStrategyName | None,
    known_classification: Classification | None = None,
) -> tuple[Classification, bool]:
    """Classify one exception, log it once, and return ``(classification, retryable)``
    -- the shared head of both failure primitives, so their log payloads cannot
    differ; ``retryable`` is the taxonomy's verdict, never caller-supplied.
    """
    classification = known_classification if known_classification is not None else classify_error_code(error)
    _logger.warning(
        event,
        extra={
            "strategy": strategy,
            "error_code": classification,
            "error_detail": redact_urls_in_text(str(error)),
        },
    )
    return classification, is_transient_classification(classification)
