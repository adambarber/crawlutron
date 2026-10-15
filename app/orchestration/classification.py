"""What a failure MEANS: the exception-to-classification mapping.

An exception says how a fetch broke; a classification says what the service
reports happened. This mapping is declared once because both result families
(engine and render) classify through it, and a second table anywhere would be
a second answer to the same question.

The table is ordered and the order carries semantics: a subclass row must
precede its catch-all or the catch-all claims it.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl

import httpx

from app.models.browser_pool_exhausted import BrowserPoolExhaustedError
from app.models.taxonomy import Classification, CrawlStrategyName
from app.orchestration.scheduler_exceptions import CircuitBreakerOpen
from app.transport.decompression import DecompressionError, DecompressionTooLargeError
from app.vocabulary.taxonomy import is_transient_classification

_logger = logging.getLogger(__name__)

__all__ = ["CLASSIFICATION_BY_EXCEPTION", "classify_and_log", "classify_error_code"]


CLASSIFICATION_BY_EXCEPTION: tuple[tuple[tuple[type[BaseException], ...], Classification], ...] = (
    ((BrowserPoolExhaustedError,), "browser_pool_exhausted"),
    # Capacity, not a page failure: the URL was never attempted, which is why it
    # stays distinct from the transport rows below and stays retryable.
    ((CircuitBreakerOpen,), "circuit_breaker_open"),
    ((DecompressionTooLargeError,), "payload_too_large"),
    # The declared encoding did not decode; its ValueError base is a zlib
    # detail, so without this row a transient fault reads as terminal.
    ((DecompressionError,), "http_error"),
    ((httpx.TooManyRedirects,), "too_many_redirects"),
    # Name resolution never got off the host; without this row it read as
    # "connection_error" and the declared dns_error member had no producer.
    ((socket.gaierror, socket.herror), "dns_error"),
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
        "connection_error",
    ),
    ((ssl.SSLError,), "tls_error"),
    ((httpx.HTTPStatusError,), "http_status_error"),
    ((httpx.HTTPError,), "http_error"),
    ((ValueError,), "value_error"),
)


def classify_error_code(exc: BaseException) -> Classification:
    """Map an internal exception to a stable, non-leaking error code.

    Raw exception text can leak DNS hostnames, internal filesystem paths, or
    stack detail. Callers must never see ``str(exc)``; these codes are safe to
    surface in user-facing ``error``/``diagnostics`` fields.

    The classifier is strategy-neutral and lives here (not inside a strategy
    module) because both the HTTP and browser strategies classify through it:
    housing it in one strategy would make the other reach across module
    boundaries for a shared taxonomy decision.

    The mapping itself is the ordered table
    :data:`CLASSIFICATION_BY_EXCEPTION`; the unrecognized case falls through to
    ``network_error``.

    Every row returns a :data:`Classification` taxonomy member; the return type
    states that so the callers that feed this straight into an
    ``EngineCrawlResult`` get a statically-enforced classification instead of an
    unproven string.
    """
    for types, classification in CLASSIFICATION_BY_EXCEPTION:
        if isinstance(exc, types):
            return classification
    return "network_error"


def classify_and_log(
    error: BaseException,
    *,
    event: str,
    strategy: CrawlStrategyName | None,
    known_classification: Classification | None = None,
) -> tuple[Classification, bool]:
    """Classify one exception, log it once, and return ``(classification, retryable)``.

    The shared head of both failure primitives, because they are the same
    operation at two layers -- ``engine_result_from_exception`` builds an
    ``EngineCrawlResult``, ``crawl_result_from_exception`` a ``CrawlResult`` --
    and were each spelling out the log payload themselves. Split, the two
    ``extra`` dicts could drift: an operator grepping the failure stream would
    find ``error_detail`` on the engine path and not on the render path, and
    conclude one of them stops logging. The event name is the only thing that
    distinguishes them, and it is a parameter precisely because it is the only
    thing that should.

    ``retryable`` is the taxonomy's verdict, not a comparison against a label
    spelled out at the call site: a capacity skip is not a dead page, so the
    URL was never attempted and the frontier can re-queue instead of recording a
    permanent failure, and the render layer has to answer 503 for a transport
    blip too or a retrying client is handed a cached permanent failure. Reading
    it from ``is_transient_classification`` is what stops the two from
    disagreeing, which is also why a caller cannot pass one in.

    ``known_classification`` is the one case where the failure mode is not
    derived from the exception type at all (the per-URL wall-clock budget raises
    bare ``TimeoutError`` but classifies as ``url_budget_exceeded``).
    """
    classification = known_classification if known_classification is not None else classify_error_code(error)
    _logger.warning(
        event,
        extra={
            "strategy": strategy,
            "error_code": classification,
            "error_detail": str(error),
        },
    )
    return classification, is_transient_classification(classification)
