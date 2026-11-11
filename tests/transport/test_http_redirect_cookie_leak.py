from http.server import BaseHTTPRequestHandler

from crawlutron.transport import cookie_jar
from crawlutron.transport.cookie_jar import CookieJar
from crawlutron.transport.request_profile import store_set_cookies
from tests._fetcher_factories import MockResponse, patch_http_fetcher_transport, run_default_http_fetcher
from tests._policy_factories import canonical

SENSITIVE_HEADERS = {"Cookie": "session=secret123", "Authorization": "Bearer token456"}


class _TransportHeadersDouble:
    """The transport's own header multidict, with only the repeated names.

    ``get``/``items`` raise, pinning the cookie path to ``get_list`` -- reading
    Set-Cookie any other way is the corruption this test exists to catch.
    """

    def __init__(self) -> None:
        self.get_list_calls: list[str] = []

    def get_list(self, key: str) -> list[str]:
        self.get_list_calls.append(key)
        if key.lower() == "set-cookie":
            return ["session=abc; Path=/", "csrf=xyz; Path=/"]
        return []

    def get(self, key: str, default: str = "") -> str:
        raise AssertionError("Set-Cookie extraction must use get_list")

    def items(self):
        raise AssertionError("Set-Cookie extraction must use get_list")


async def _fetch_with_redirect(monkeypatch, redirect_location):
    """302-redirect the fetcher via ``redirect_location`` and capture per-hop headers."""
    responses = [
        MockResponse(302, {"location": redirect_location}, b""),
        MockResponse(200, {"content-type": "text/html"}, b"<html>ok</html>"),
    ]
    mock_client = patch_http_fetcher_transport(
        monkeypatch,
        responses,
        allow_ssrf_bypass=True,
    )
    result = await run_default_http_fetcher("http://example.com/", SENSITIVE_HEADERS)
    return mock_client, result


async def test_http_fetcher_strips_cookie_on_cross_domain_redirect(monkeypatch):
    """HTTP fetcher should strip Cookie and Authorization headers when redirecting
    to a different hostname."""
    mock_client, _ = await _fetch_with_redirect(monkeypatch, "http://attacker.com/steal")

    # First request should have the sensitive headers
    assert mock_client.captured_headers[0].get("Cookie") == SENSITIVE_HEADERS["Cookie"]
    assert mock_client.captured_headers[0].get("Authorization") == SENSITIVE_HEADERS["Authorization"]

    # Second request (to attacker.com) should NOT have sensitive headers
    assert mock_client.captured_headers[1].get("Cookie") is None
    assert mock_client.captured_headers[1].get("Authorization") is None


async def test_http_fetcher_keeps_cookie_on_same_domain_redirect(monkeypatch):
    """HTTP fetcher should keep Cookie/Authorization headers when redirecting
    within the same hostname."""
    mock_client, _ = await _fetch_with_redirect(monkeypatch, "/new-path")

    # Both requests should have the sensitive headers (same host)
    for captured in mock_client.captured_headers:
        assert captured.get("Cookie") == SENSITIVE_HEADERS["Cookie"]
        assert captured.get("Authorization") == SENSITIVE_HEADERS["Authorization"]


class _TwoCookieHandler(BaseHTTPRequestHandler):
    """Serve two separate ``Set-Cookie`` headers, framing the body explicitly."""

    BODY = b"<html>ok</html>"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Set-Cookie", "session=abc; Path=/")
        self.send_header("Set-Cookie", "csrf=xyz; Path=/")
        self.send_header("Content-Length", str(len(_TwoCookieHandler.BODY)))
        self.end_headers()
        self.wfile.write(_TwoCookieHandler.BODY)

    def log_message(self, *args):
        """Silence the default per-request stderr logging."""


async def test_http_fetcher_persists_every_set_cookie_header():
    """Every Set-Cookie on a response must reach the jar, not just the last.

    The httpx fetcher once comma-joined duplicates in a dict comprehension,
    defeating the helper's ``get_list`` path; it now stores from the headers.
    """
    from tests.helpers.server import spawn_threaded_server

    server = spawn_threaded_server(_TwoCookieHandler)
    url = f"http://127.0.0.1:{server.server_port}/"
    await run_default_http_fetcher(url)
    server.shutdown()
    server.server_close()

    jar = cookie_jar.global_cookie_jar()
    cookie_header = jar.cookie_header_for(canonical(url))
    assert "session=abc" in cookie_header, cookie_header
    assert "csrf=xyz" in cookie_header, cookie_header


async def test_store_set_cookies_handles_multiple_set_cookie_headers():
    """store_set_cookies must capture all Set-Cookie headers, not just one.

    httpx 0.28+ comma-joins duplicate keys in ``items()``; ``get_list`` gives
    each header separately. Awaited: the function is the tails' async door.
    """
    jar = cookie_jar.global_cookie_jar()

    headers = _TransportHeadersDouble()
    await store_set_cookies(canonical("http://example.com/"), headers)

    assert headers.get_list_calls == ["set-cookie"]
    cookie_header = jar.cookie_header_for(canonical("http://example.com/"))
    assert "session=abc" in cookie_header
    assert "csrf=xyz" in cookie_header


def test_cookiejar_add_cookie_header_parses_each_cookie_separately():
    """CookieJar.add_cookie_header must handle each Set-Cookie individually.

    Passing a comma-joined string (what httpx.items() yields) to SimpleCookie
    corrupts cookie values (e.g., 'abc,' instead of 'abc').
    """
    jar = CookieJar()

    # BAD: comma-joined (what old code would pass)
    jar.add_cookie_header(canonical("http://example.com/"), "session=abc, csrf=xyz; Path=/")
    bad_result = jar.cookie_header_for(canonical("http://example.com/"))
    assert "abc," in bad_result  # Trailing comma - CORRUPTED

    # GOOD: call separately for each cookie
    jar2 = CookieJar()
    jar2.add_cookie_header(canonical("http://example.com/"), "session=abc; Path=/")
    jar2.add_cookie_header(canonical("http://example.com/"), "csrf=xyz; Path=/")
    good_result = jar2.cookie_header_for(canonical("http://example.com/"))
    assert "session=abc" in good_result and "abc," not in good_result
    assert "csrf=xyz" in good_result
