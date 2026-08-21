import pytest
from urllib.parse import urljoin

import app.crawl.strategies.http as http_mod
from app.crawl.strategies.http import _default_http_fetcher, _build_pinned_request, _MAX_REDIRECT_HOPS


class MockResponse:
    def __init__(self, status_code, headers, content=b""):
        self.status_code = status_code
        self.headers = headers
        self._content = content
        self.url = "http://example.com/"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_raw(self):
        yield self._content


class MockClient:
    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0
        self.captured_headers = []

    def stream(self, method, url, headers, timeout, follow_redirects):
        self.call_count += 1
        self.captured_headers.append(dict(headers))
        resp = self.responses[self.call_count - 1]
        resp.url = url
        return resp


async def test_http_fetcher_strips_cookie_on_cross_domain_redirect(monkeypatch):
    """HTTP fetcher should strip Cookie and Authorization headers when redirecting
    to a different hostname (Fix 3)."""
    # First response: 302 redirect to different domain
    # Second response: final page
    responses = [
        MockResponse(302, {"location": "http://attacker.com/steal"}, b""),
        MockResponse(200, {"content-type": "text/html"}, b"<html>ok</html>"),
    ]
    mock_client = MockClient(responses)
    monkeypatch.setattr(http_mod, "get_async_http_client", lambda: mock_client)
    monkeypatch.setattr(http_mod, "_check_ssrf_protection", lambda url: None)
    monkeypatch.setattr(http_mod, "_resolve_and_pin_host", lambda url: (urlparse(url).hostname, "1.2.3.4"))

    headers = {"Cookie": "session=secret123", "Authorization": "Bearer token456"}
    result = await _default_http_fetcher("http://example.com/", headers, 30)

    # First request should have the sensitive headers
    assert mock_client.captured_headers[0].get("Cookie") == "session=secret123"
    assert mock_client.captured_headers[0].get("Authorization") == "Bearer token456"

    # Second request (to attacker.com) should NOT have sensitive headers
    assert mock_client.captured_headers[1].get("Cookie") is None
    assert mock_client.captured_headers[1].get("Authorization") is None


async def test_http_fetcher_keeps_cookie_on_same_domain_redirect(monkeypatch):
    """HTTP fetcher should keep Cookie/Authorization headers when redirecting
    within the same hostname (Fix 3)."""
    responses = [
        MockResponse(302, {"location": "/new-path"}, b""),
        MockResponse(200, {"content-type": "text/html"}, b"<html>ok</html>"),
    ]
    mock_client = MockClient(responses)
    monkeypatch.setattr(http_mod, "get_async_http_client", lambda: mock_client)
    monkeypatch.setattr(http_mod, "_check_ssrf_protection", lambda url: None)
    monkeypatch.setattr(http_mod, "_resolve_and_pin_host", lambda url: (urlparse(url).hostname, "1.2.3.4"))

    headers = {"Cookie": "session=secret123", "Authorization": "Bearer token456"}
    result = await _default_http_fetcher("http://example.com/", headers, 30)

    # Both requests should have the sensitive headers (same host)
    assert mock_client.captured_headers[0].get("Cookie") == "session=secret123"
    assert mock_client.captured_headers[1].get("Cookie") == "session=secret123"
    assert mock_client.captured_headers[0].get("Authorization") == "Bearer token456"
    assert mock_client.captured_headers[1].get("Authorization") == "Bearer token456"


# Need urlparse for the mock
from urllib.parse import urlparse
