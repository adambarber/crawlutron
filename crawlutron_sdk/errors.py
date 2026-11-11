"""Typed error hierarchy for the crawlutron service facade.

Every service refusal carries a wire envelope ``{code, message, details}`` and an
HTTP status. The hierarchy mirrors the status families the service emits so
callers can catch by family or by exact status.
"""

from __future__ import annotations


class CrawlutronError(Exception):
    """Base error: the service envelope code/message/details plus HTTP status."""

    status_code: int | None = None
    code: str = "crawlutron_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, object] | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message
        self.code = code if code is not None else self.code
        self.details = details or {}
        self.status_code = status_code if status_code is not None else self.status_code
        super().__init__(message)


class CrawlutronClientError(CrawlutronError):
    """4xx caller error."""


class CrawlutronUnauthorizedError(CrawlutronClientError):
    status_code = 401
    code = "unauthorized"


class CrawlutronForbiddenError(CrawlutronClientError):
    status_code = 403
    code = "forbidden"


class CrawlutronNotFoundError(CrawlutronClientError):
    status_code = 404
    code = "not_found"


class CrawlutronBadRequestError(CrawlutronClientError):
    status_code = 400
    code = "bad_request"


class CrawlutronConflictError(CrawlutronClientError):
    status_code = 409
    code = "invalid_state"


class CrawlutronClientClosedRequestError(CrawlutronClientError):
    """499: the service saw the caller hang up. Envelope-construction only.

    A connected SDK caller sees httpx TransportError; the service raises 499
    only after detecting the disconnect, so this class never crosses live HTTP."""

    status_code = 499
    code = "client_disconnected"


class CrawlutronRequestTooLargeError(CrawlutronClientError):
    status_code = 413
    code = "request_too_large"


class CrawlutronTooManyRequestsError(CrawlutronClientError):
    status_code = 429
    code = "too_many_requests"


class CrawlutronUnprocessableEntityError(CrawlutronClientError):
    status_code = 422
    code = "validation_error"


class CrawlutronAcceptedError(CrawlutronClientError):
    status_code = 202
    code = "accepted"


class CrawlutronServerError(CrawlutronError):
    """5xx service error."""


class CrawlutronBadGatewayError(CrawlutronServerError):
    status_code = 502
    code = "bad_gateway"


class CrawlutronServiceUnavailableError(CrawlutronServerError):
    status_code = 503
    code = "service_unavailable"


class CrawlutronInternalServerError(CrawlutronServerError):
    status_code = 500
    code = "internal_server_error"


class CrawlutronTransportError(CrawlutronError):
    """Network/transport failure; no service HTTP status applies."""

    status_code = None
    code = "transport_error"


_STATUS_TO_ERROR: dict[int, type[CrawlutronError]] = {
    202: CrawlutronAcceptedError,
    400: CrawlutronBadRequestError,
    401: CrawlutronUnauthorizedError,
    403: CrawlutronForbiddenError,
    404: CrawlutronNotFoundError,
    409: CrawlutronConflictError,
    413: CrawlutronRequestTooLargeError,
    422: CrawlutronUnprocessableEntityError,
    429: CrawlutronTooManyRequestsError,
    499: CrawlutronClientClosedRequestError,
}

_SERVER_ERRORS: dict[int, type[CrawlutronServerError]] = {
    500: CrawlutronInternalServerError,
    502: CrawlutronBadGatewayError,
    503: CrawlutronServiceUnavailableError,
}


def error_from_response(status_code: int, envelope: dict[str, object]) -> CrawlutronError:
    """Build the matching typed error from a service error response."""
    code = str(envelope.get("code", "crawlutron_error"))
    message = str(envelope.get("message", "Request failed"))
    details = envelope.get("details")
    if not isinstance(details, dict):
        details = {}
    if 500 <= status_code < 600:
        klass: type[CrawlutronError] = _SERVER_ERRORS.get(status_code, CrawlutronInternalServerError)
    else:
        klass = _STATUS_TO_ERROR.get(status_code, CrawlutronError)
    return klass(message=message, code=code, details=details, status_code=status_code)


__all__ = [
    "CrawlutronAcceptedError",
    "CrawlutronBadGatewayError",
    "CrawlutronBadRequestError",
    "CrawlutronClientClosedRequestError",
    "CrawlutronClientError",
    "CrawlutronConflictError",
    "CrawlutronError",
    "CrawlutronForbiddenError",
    "CrawlutronInternalServerError",
    "CrawlutronNotFoundError",
    "CrawlutronRequestTooLargeError",
    "CrawlutronServerError",
    "CrawlutronServiceUnavailableError",
    "CrawlutronTooManyRequestsError",
    "CrawlutronTransportError",
    "CrawlutronUnauthorizedError",
    "CrawlutronUnprocessableEntityError",
    "error_from_response",
]
