"""Adversarial fix (HIGH): error responses must not leak raw exception
text (DNS names, internal paths, stack detail) in the API payload.

Raw detail is NEVER returned to the caller. Only a stable error code is exposed
in ``error``/``diagnostics``, and the raw detail is logged server-side for
debugging (see app.crawl.strategies.http._http_strategy_result).
"""
import json
import socket

import httpx

from app.crawl.decompression import DecompressionTooLargeError
from app.crawl.strategies.http import _classify_error_code, _http_strategy_result


class _RaisingFetcher:
    def __init__(self, exc):
        self.exc = exc

    def __call__(self, url, headers, timeout, max_content_length):
        async def _raise():
            raise self.exc

        return _raise()


async def _run(exc):
    fetcher = _RaisingFetcher(exc)
    policy = {"limits": {}, "extraction": "html", "headers": {}}
    return await _http_strategy_result(
        original_url="http://example.com/",
        normalized_url="http://example.com/",
        strategy="HTTP_DECLARED_CRAWLER",
        policy=policy,
        http_fetcher=fetcher,
    )


def test_classify_error_codes_map_to_stable_values():
    assert _classify_error_code(socket.gaierror("boom")) == "connection_error"
    assert _classify_error_code(httpx.ConnectError("x")) == "connection_error"
    assert _classify_error_code(ValueError("bad")) == "value_error"
    assert _classify_error_code(DecompressionTooLargeError("big")) == "payload_too_large"
    # Unknown exception -> generic network_error, never the raw message.
    assert _classify_error_code(RuntimeError("secret /app/x")) == "network_error"


async def test_http_strategy_result_hides_raw_dns_error():
    res = await _run(socket.gaierror("[Errno -2] Name or service not known"))
    assert res.status == "failed"
    # Stable code, never the raw message.
    assert res.error == "connection_error"
    assert res.error != "[Errno -2] Name or service not known"
    # Raw exception text must NOT be returned anywhere in the API diagnostics.
    assert "error_detail" not in res.diagnostics
    serialized = json.dumps(res.diagnostics, default=str)
    assert "Name or service not known" not in serialized


async def test_http_strategy_result_hides_arbitrary_exception():
    res = await _run(RuntimeError("internal secret /app/secret/path"))
    assert res.status == "failed"
    assert res.error == "network_error"
    assert res.error != "/app/secret/path"
    assert "error_detail" not in res.diagnostics
    serialized = json.dumps(res.diagnostics, default=str)
    assert "/app/secret/path" not in serialized
