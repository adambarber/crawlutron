"""Typed error hierarchy: status-to-class mapping and envelope preservation."""

from __future__ import annotations

from crawlutron_sdk.errors import (
    CrawlutronAcceptedError,
    CrawlutronBadGatewayError,
    CrawlutronClientError,
    CrawlutronError,
    CrawlutronForbiddenError,
    CrawlutronInternalServerError,
    CrawlutronNotFoundError,
    CrawlutronRequestTooLargeError,
    CrawlutronServerError,
    CrawlutronServiceUnavailableError,
    CrawlutronTooManyRequestsError,
    CrawlutronTransportError,
    CrawlutronUnauthorizedError,
    CrawlutronUnprocessableEntityError,
    error_from_response,
)


def test_error_attributes():
    error = CrawlutronNotFoundError("gone", details={"request_id": "x"}, status_code=404)
    assert error.message == "gone"
    assert error.code == "not_found"
    assert error.details == {"request_id": "x"}
    assert error.status_code == 404
    assert str(error) == "gone"


def test_subclass_status_codes():
    assert CrawlutronUnauthorizedError.status_code == 401
    assert CrawlutronForbiddenError.status_code == 403
    assert CrawlutronRequestTooLargeError.status_code == 413
    assert CrawlutronTooManyRequestsError.status_code == 429
    assert CrawlutronNotFoundError.status_code == 404
    assert CrawlutronUnprocessableEntityError.status_code == 422
    assert CrawlutronAcceptedError.status_code == 202
    assert CrawlutronBadGatewayError.status_code == 502
    assert CrawlutronServiceUnavailableError.status_code == 503
    assert CrawlutronInternalServerError.status_code == 500
    assert CrawlutronTransportError.status_code is None


def test_error_family_catches():
    assert issubclass(CrawlutronNotFoundError, CrawlutronClientError)
    assert issubclass(CrawlutronClientError, CrawlutronError)
    assert issubclass(CrawlutronServiceUnavailableError, CrawlutronServerError)
    assert issubclass(CrawlutronServerError, CrawlutronError)


def test_error_from_response_maps_4xx():
    cases = {
        401: CrawlutronUnauthorizedError,
        403: CrawlutronForbiddenError,
        413: CrawlutronRequestTooLargeError,
        429: CrawlutronTooManyRequestsError,
        404: CrawlutronNotFoundError,
        422: CrawlutronUnprocessableEntityError,
        202: CrawlutronAcceptedError,
    }
    for status, klass in cases.items():
        error = error_from_response(status, {"code": "c", "message": "m", "details": {}})
        assert isinstance(error, klass), (status, type(error))
        assert error.status_code == status
        assert error.code == "c"
        assert error.message == "m"


def test_error_from_response_maps_5xx():
    cases = {
        500: CrawlutronInternalServerError,
        502: CrawlutronBadGatewayError,
        503: CrawlutronServiceUnavailableError,
    }
    for status, klass in cases.items():
        error = error_from_response(status, {"code": "c", "message": "m"})
        assert isinstance(error, klass)
        assert error.status_code == status


def test_error_from_response_unknown_status_is_base_error():
    error = error_from_response(418, {"code": "teapot", "message": "m"})
    assert isinstance(error, CrawlutronError)
    assert not isinstance(error, CrawlutronClientError)
    assert error.status_code == 418


def test_error_from_response_normalizes_details():
    error = error_from_response(400, {"code": "c", "message": "m", "details": "not-a-dict"})
    assert error.details == {}


def test_accepted_error_is_client_error():
    assert issubclass(CrawlutronAcceptedError, CrawlutronClientError)
    error = CrawlutronAcceptedError("accepted", status_code=202)
    assert error.status_code == 202


def test_transport_error_has_no_status():
    error = CrawlutronTransportError("network down")
    assert error.status_code is None
    assert error.code == "transport_error"
