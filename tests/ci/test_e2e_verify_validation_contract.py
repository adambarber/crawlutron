"""E2E smoke checks must pin the validation envelope contract."""

from __future__ import annotations

import httpx


def _response(status: int, payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://test/v1/crawl"))


def _client(responses: list[httpx.Response]) -> httpx.Client:
    remaining = iter(responses)

    def handle(_request: httpx.Request) -> httpx.Response:
        return next(remaining)

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_ssrf_rejection_requires_422_validation_error() -> None:
    from scripts import e2e_verify

    e2e_verify.results.clear()
    responses = [
        _response(400, {"code": "validation_error", "message": "bad"}),
        _response(422, {"code": "validation_error", "message": "bad"}),
        _response(422, {"code": "validation_error", "message": "bad"}),
        _response(200, {"links": []}),
    ]
    with _client(responses) as client:
        e2e_verify._validation(client, "http://test", "https://example.com")

    outcomes = dict(e2e_verify.results)
    assert outcomes["SSRF internal rejected"] is False
    assert outcomes["validation shape standardized"] is True


def test_ssrf_rejection_passes_for_422_validation_error() -> None:
    from scripts import e2e_verify

    e2e_verify.results.clear()
    responses = [
        _response(422, {"code": "validation_error", "message": "bad"}),
        _response(422, {"code": "validation_error", "message": "bad"}),
        _response(422, {"code": "validation_error", "message": "bad"}),
        _response(200, {"links": []}),
    ]
    with _client(responses) as client:
        e2e_verify._validation(client, "http://test", "https://example.com")

    outcomes = dict(e2e_verify.results)
    assert outcomes["SSRF internal rejected"] is True
    assert outcomes["validation shape standardized"] is True
