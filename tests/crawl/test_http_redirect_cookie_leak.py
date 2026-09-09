from urllib.parse import urlparse

import app.crawl.strategies.http as http_mod
from app.crawl.strategies.http import _default_http_fetcher


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


def _patch_fetch_transport(monkeypatch, responses):
    """Route _default_http_fetcher through a scripted mock client."""
    mock_client = MockClient(responses)
    monkeypatch.setattr(http_mod, "get_async_http_client", lambda: mock_client)
    monkeypatch.setattr(http_mod, "_check_ssrf_protection", lambda url: None)
    monkeypatch.setattr(
        http_mod,
        "_resolve_and_pin_host",
        lambda url: (urlparse(url).hostname, "1.2.3.4"),
    )
    return mock_client


SENSITIVE_HEADERS = {"Cookie": "session=secret123", "Authorization": "Bearer token456"}


async def _fetch_with_redirect(monkeypatch, redirect_location):
    """302-redirect the fetcher via ``redirect_location`` and capture per-hop headers."""
    responses = [
        MockResponse(302, {"location": redirect_location}, b""),
        MockResponse(200, {"content-type": "text/html"}, b"<html>ok</html>"),
    ]
    mock_client = _patch_fetch_transport(monkeypatch, responses)
    result = await _default_http_fetcher("http://example.com/", dict(SENSITIVE_HEADERS), 30)
    return mock_client, result


async def test_http_fetcher_strips_cookie_on_cross_domain_redirect(monkeypatch):
    """HTTP fetcher should strip Cookie and Authorization headers when redirecting
    to a different hostname (Fix 3)."""
    mock_client, _ = await _fetch_with_redirect(monkeypatch, "http://attacker.com/steal")

    # First request should have the sensitive headers
    assert mock_client.captured_headers[0].get("Cookie") == SENSITIVE_HEADERS["Cookie"]
    assert mock_client.captured_headers[0].get("Authorization") == SENSITIVE_HEADERS["Authorization"]

    # Second request (to attacker.com) should NOT have sensitive headers
    assert mock_client.captured_headers[1].get("Cookie") is None
    assert mock_client.captured_headers[1].get("Authorization") is None


async def test_http_fetcher_keeps_cookie_on_same_domain_redirect(monkeypatch):
    """HTTP fetcher should keep Cookie/Authorization headers when redirecting
    within the same hostname (Fix 3)."""
    mock_client, _ = await _fetch_with_redirect(monkeypatch, "/new-path")

    # Both requests should have the sensitive headers (same host)
    for captured in mock_client.captured_headers:
        assert captured.get("Cookie") == SENSITIVE_HEADERS["Cookie"]
        assert captured.get("Authorization") == SENSITIVE_HEADERS["Authorization"]


def test_store_set_cookies_handles_multiple_set_cookie_headers():
    """_store_set_cookies must capture all Set-Cookie headers, not just one.

    In httpx 0.28+, Headers.items() returns comma-joined values for duplicate
    keys. Using get_list("set-cookie") gives each header separately.
    """
    from app.crawl.strategies.http import CookieJar, _store_set_cookies

    # Use a fresh jar to avoid interference from other tests
    jar = CookieJar()
    import app.crawl.strategies.http as http_mod

    old_jar = http_mod._GLOBAL_COOKIE_JAR
    http_mod._GLOBAL_COOKIE_JAR = jar
    try:

        class MockHeaders:
            def get_list(self, key):
                if key.lower() == "set-cookie":
                    return ["session=abc; Path=/", "csrf=xyz; Path=/"]
                return []

            def items(self):
                return []

        _store_set_cookies("http://example.com/", MockHeaders())

        cookie_header = jar.cookie_header_for("http://example.com/")
        assert "session=abc" in cookie_header
        assert "csrf=xyz" in cookie_header
    finally:
        http_mod._GLOBAL_COOKIE_JAR = old_jar


def test_cookiejar_add_cookie_header_parses_each_cookie_separately():
    """CookieJar.add_cookie_header must handle each Set-Cookie individually.

    Passing a comma-joined string (what httpx.items() yields) to SimpleCookie
    corrupts cookie values (e.g., 'abc,' instead of 'abc').
    """
    from app.crawl.strategies.http import CookieJar

    jar = CookieJar()

    # BAD: comma-joined (what old code would pass)
    jar.add_cookie_header("http://example.com/", "session=abc, csrf=xyz; Path=/")
    bad_result = jar.cookie_header_for("http://example.com/")
    assert "abc," in bad_result  # Trailing comma - CORRUPTED

    # GOOD: call separately for each cookie
    jar2 = CookieJar()
    jar2.add_cookie_header("http://example.com/", "session=abc; Path=/")
    jar2.add_cookie_header("http://example.com/", "csrf=xyz; Path=/")
    good_result = jar2.cookie_header_for("http://example.com/")
    assert "session=abc" in good_result and "abc," not in good_result
    assert "csrf=xyz" in good_result
