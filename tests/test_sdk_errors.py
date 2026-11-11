"""Typed error hierarchy: status-to-class mapping and envelope preservation."""

from __future__ import annotations

import crawlutron_sdk
from crawlutron_sdk.errors import error_from_response


def test_package_root_exports_every_refusal_class():
    """Every service refusal class a caller can catch imports from the root."""
    module_errors = {name for name in crawlutron_sdk.errors.__all__ if name.endswith("Error")}
    root_errors = {name for name in crawlutron_sdk.__all__ if name.endswith("Error")}
    assert module_errors == root_errors, f"root is missing: {sorted(module_errors - root_errors)}"


def test_every_root_export_is_importable_from_the_root():
    """The root's ``__all__`` is derived from its submodules; every name resolves."""
    for name in crawlutron_sdk.__all__:
        assert getattr(crawlutron_sdk, name) is not None, name


def test_new_refusal_class_code_constants_match_their_wire_codes():
    """error_from_response overrides code from the envelope, so a drifting class
    constant is invisible to the mapping tests; pin the three newest."""
    assert crawlutron_sdk.CrawlutronBadRequestError.code == "bad_request"
    assert crawlutron_sdk.CrawlutronConflictError.code == "invalid_state"
    assert crawlutron_sdk.CrawlutronClientClosedRequestError.code == "client_disconnected"


def test_error_attributes():
    error = crawlutron_sdk.CrawlutronNotFoundError("gone", details={"request_id": "x"}, status_code=404)
    assert error.message == "gone"
    assert error.code == "not_found"
    assert error.details == {"request_id": "x"}
    assert error.status_code == 404
    assert str(error) == "gone"


def test_subclass_status_codes():
    assert crawlutron_sdk.CrawlutronBadRequestError.status_code == 400
    assert crawlutron_sdk.CrawlutronConflictError.status_code == 409
    assert crawlutron_sdk.CrawlutronClientClosedRequestError.status_code == 499
    assert crawlutron_sdk.CrawlutronUnauthorizedError.status_code == 401
    assert crawlutron_sdk.CrawlutronForbiddenError.status_code == 403
    assert crawlutron_sdk.CrawlutronRequestTooLargeError.status_code == 413
    assert crawlutron_sdk.CrawlutronTooManyRequestsError.status_code == 429
    assert crawlutron_sdk.CrawlutronNotFoundError.status_code == 404
    assert crawlutron_sdk.CrawlutronUnprocessableEntityError.status_code == 422
    assert crawlutron_sdk.CrawlutronAcceptedError.status_code == 202
    assert crawlutron_sdk.CrawlutronBadGatewayError.status_code == 502
    assert crawlutron_sdk.CrawlutronServiceUnavailableError.status_code == 503
    assert crawlutron_sdk.CrawlutronInternalServerError.status_code == 500
    assert crawlutron_sdk.CrawlutronTransportError.status_code is None


def test_error_family_catches():
    assert issubclass(crawlutron_sdk.CrawlutronNotFoundError, crawlutron_sdk.CrawlutronClientError)
    assert issubclass(crawlutron_sdk.CrawlutronClientError, crawlutron_sdk.CrawlutronError)
    assert issubclass(crawlutron_sdk.CrawlutronServiceUnavailableError, crawlutron_sdk.CrawlutronServerError)
    assert issubclass(crawlutron_sdk.CrawlutronServerError, crawlutron_sdk.CrawlutronError)


def test_error_from_response_maps_4xx():
    cases = {
        400: crawlutron_sdk.CrawlutronBadRequestError,
        409: crawlutron_sdk.CrawlutronConflictError,
        499: crawlutron_sdk.CrawlutronClientClosedRequestError,
        401: crawlutron_sdk.CrawlutronUnauthorizedError,
        403: crawlutron_sdk.CrawlutronForbiddenError,
        413: crawlutron_sdk.CrawlutronRequestTooLargeError,
        429: crawlutron_sdk.CrawlutronTooManyRequestsError,
        404: crawlutron_sdk.CrawlutronNotFoundError,
        422: crawlutron_sdk.CrawlutronUnprocessableEntityError,
        202: crawlutron_sdk.CrawlutronAcceptedError,
    }
    for status, klass in cases.items():
        error = error_from_response(status, {"code": "c", "message": "m", "details": {}})
        assert isinstance(error, klass), (status, type(error))
        assert error.status_code == status
        assert error.code == "c"
        assert error.message == "m"


def test_error_from_response_maps_5xx():
    cases = {
        500: crawlutron_sdk.CrawlutronInternalServerError,
        502: crawlutron_sdk.CrawlutronBadGatewayError,
        503: crawlutron_sdk.CrawlutronServiceUnavailableError,
    }
    for status, klass in cases.items():
        error = error_from_response(status, {"code": "c", "message": "m"})
        assert isinstance(error, klass)
        assert error.status_code == status


def test_error_from_response_unknown_status_is_base_error():
    error = error_from_response(418, {"code": "teapot", "message": "m"})
    assert isinstance(error, crawlutron_sdk.CrawlutronError)
    assert not isinstance(error, crawlutron_sdk.CrawlutronClientError)
    assert error.status_code == 418


def test_error_from_response_normalizes_details():
    error = error_from_response(400, {"code": "c", "message": "m", "details": "not-a-dict"})
    assert error.details == {}


def test_accepted_error_is_client_error():
    assert issubclass(crawlutron_sdk.CrawlutronAcceptedError, crawlutron_sdk.CrawlutronClientError)
    error = crawlutron_sdk.CrawlutronAcceptedError("accepted", status_code=202)
    assert error.status_code == 202


def test_transport_error_has_no_status():
    error = crawlutron_sdk.CrawlutronTransportError("network down")
    assert error.status_code is None
    assert error.code == "transport_error"
