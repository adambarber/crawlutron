from http.server import BaseHTTPRequestHandler

from app.transport import cookie_jar
from app.transport.cookie_jar import CookieJar
from app.transport.request_profile import store_set_cookies
from tests._fetcher_factories import run_default_http_fetcher
from tests._policy_factories import canonical
from tests._transport_doubles import MockResponse, patch_http_fetcher_transport

SENSITIVE_HEADERS = {"Cookie": "session=secret123", "Authorization": "Bearer token456"}


class _TransportHeadersDouble:
    """The transport's own header multidict, with only the repeated names.

    ``get`` and ``items`` raise rather than answer, so the cookie path is pinned
    to ``get_list``: reading Set-Cookie any other way is the corruption this
    test exists to catch, and a double that quietly answered would hide the
    regression instead of failing on it.
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
    """Serve a response carrying two separate ``Set-Cookie`` headers.

    A real socket, not a ``MockResponse``: a plain dict can hold only one value
    per key, so it structurally cannot express the duplicate header that this
    behaviour turns on.
    """

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Set-Cookie", "session=abc; Path=/")
        self.send_header("Set-Cookie", "csrf=xyz; Path=/")
        self.end_headers()
        self.wfile.write(b"<html>ok</html>")

    def log_message(self, *args):
        """Silence the default per-request stderr logging."""


async def test_http_fetcher_persists_every_set_cookie_header():
    """Every Set-Cookie on a response must reach the jar, not just the last.

    ``store_set_cookies`` is documented to capture each Set-Cookie separately
    and does so via ``get_list("set-cookie")``, which returns one entry per
    header. The httpx fetcher used to bypass that: it built a dict comprehension
    of the response headers and handed *that* to the helper, and because
    ``httpx.Headers.items()`` comma-joins duplicate keys the helper fell through
    to its plain-dict branch with a single ``"a=1; Path=/, b=2; Path=/"`` entry.

    Both cookies did survive that round trip -- the jar's SimpleCookie re-splits a
    comma-joined value -- but only for shapes where the first cookie carries its
    own attributes, and ``test_cookiejar_add_cookie_header_parses_each_cookie_
    separately`` in this file documents comma-joined values as a corruption risk
    outright. The fetcher now stores cookies from the response headers themselves,
    so the helper takes its documented ``get_list`` path and the guarantee rests
    on the code that was written to provide it.

    This is the only test covering the fetcher's call into the cookie helper; the
    helper's own multi-header guarantee is pinned separately below and passed
    happily while the caller defeated it.
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


def test_store_set_cookies_handles_multiple_set_cookie_headers():
    """store_set_cookies must capture all Set-Cookie headers, not just one.

    In httpx 0.28+, Headers.items() returns comma-joined values for duplicate
    keys. Using get_list("set-cookie") gives each header separately.

    Reads the process-wide jar, which the conftest teardown empties between
    tests, rather than swapping the module global for a private one: the swap
    was 8 lines of try/finally guarding state the fixture already guarantees
    is clean on entry.
    """
    jar = cookie_jar.global_cookie_jar()

    headers = _TransportHeadersDouble()
    store_set_cookies(canonical("http://example.com/"), headers)

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
