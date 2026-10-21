"""RFC 6265 cookie semantics for the jar: attributes, deletion, and redirect hops.

Every test states the RFC rule it pins. The jar delegates parse and send
matching to stdlib http.cookiejar; these tests pin the observable behavior.
"""

from __future__ import annotations

from app.transport import cookie_jar
from app.transport.cookie_jar import CookieJar
from tests._fetcher_factories import run_default_http_fetcher
from tests._policy_factories import canonical
from tests._transport_doubles import MockResponse, patch_http_fetcher_transport


def _header_for(jar: CookieJar, url: str, *, namespace: str = "") -> str:
    return jar.cookie_header_for(canonical(url), namespace=namespace)


# RFC 6265 4.1.2.2 / 5.3 step 15: Max-Age=0 is a deletion, not an empty value.


def test_max_age_zero_deletes_a_cookie():
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://a.example/"), "sid=abc; Path=/")
    jar.add_cookie_header(canonical("http://a.example/"), "sid=; Max-Age=0; Path=/")
    assert _header_for(jar, "http://a.example/") == ""


def test_past_expires_deletes_a_cookie():
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://a.example/"), "sid=abc; Path=/")
    jar.add_cookie_header(
        canonical("http://a.example/"),
        "sid=abc; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/",
    )
    assert _header_for(jar, "http://a.example/") == ""


# RFC 6265bis 5.5: a Secure cookie offered over a non-secure origin is refused.


def test_secure_cookie_is_refused_over_plain_http():
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://a.example/"), "sid=abc; Secure; Path=/")
    assert _header_for(jar, "http://a.example/") == ""  # never sent over http (5.4)
    assert _header_for(jar, "https://a.example/") == ""  # and never stored at all


# RFC 6265 5.4 / 8.5: a Secure cookie never travels over plain HTTP.


def test_secure_cookie_is_stored_over_https_and_refused_over_http():
    jar = CookieJar()
    jar.add_cookie_header(canonical("https://a.example/"), "sid=abc; Secure; Path=/")
    assert _header_for(jar, "https://a.example/") == "sid=abc"
    assert _header_for(jar, "http://a.example/") == ""


# RFC 6265 5.1.4 default-path + 5.4 path-match: /admin cookies stay on /admin.


def test_cookie_without_path_is_scoped_to_the_request_directory():
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://a.example/admin/deep"), "sid=abc")
    assert _header_for(jar, "http://a.example/admin/panel") == "sid=abc"
    assert _header_for(jar, "http://a.example/public") == ""


def test_explicit_path_attribute_scopes_the_cookie():
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://a.example/"), "sid=abc; Path=/admin")
    assert _header_for(jar, "http://a.example/admin/panel") == "sid=abc"
    assert _header_for(jar, "http://a.example/") == ""


# RFC 6265 5.1.3 domain-match + 5.3 step 6 host-only-flag.


def test_domain_attribute_broadens_the_cookie_to_the_domain():
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://sub.a.example/"), "sid=abc; Domain=a.example")
    assert _header_for(jar, "http://a.example/") == "sid=abc"
    assert _header_for(jar, "http://other.a.example/") == "sid=abc"
    assert _header_for(jar, "http://other.example/") == ""


def test_host_only_cookie_stays_on_its_host():
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://sub.a.example/"), "sid=abc")
    assert _header_for(jar, "http://sub.a.example/") == "sid=abc"
    assert _header_for(jar, "http://a.example/") == ""


def test_domain_attribute_outside_the_request_host_is_refused():
    # A response for other.example cannot mint a cookie for a.example.
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://other.example/"), "sid=abc; Domain=a.example")
    assert _header_for(jar, "http://a.example/") == ""
    assert _header_for(jar, "http://other.example/") == ""


# RFC 6265 5.4: cookies with longer paths are listed first.


def test_longer_path_cookies_are_sent_first():
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://a.example/"), "root=v; Path=/")
    jar.add_cookie_header(canonical("http://a.example/"), "deep=v; Path=/a/b")
    header = _header_for(jar, "http://a.example/a/b/c")
    assert header == "deep=v; root=v"


def test_namespaced_attribute_cookies_stay_isolated():
    jar = CookieJar()
    jar.add_cookie_header(canonical("https://a.example/"), "sid=abc; Secure; Path=/admin", namespace="t1")
    assert _header_for(jar, "https://a.example/admin/x", namespace="t1") == "sid=abc"
    assert _header_for(jar, "https://a.example/admin/x") == ""


# Redirect hops: a 302's Set-Cookie must reach the jar (RFC 6265 5.3), and a
# cross-origin hop must re-attach the new origin's own jar cookies.


async def test_redirect_hop_set_cookie_is_collected(monkeypatch):
    responses = [
        MockResponse(302, {"location": "/dashboard", "set-cookie": "sess=hop1; Path=/"}, b""),
        MockResponse(200, {"content-type": "text/html"}, b"<html>ok</html>"),
    ]
    patch_http_fetcher_transport(monkeypatch, responses, allow_ssrf_bypass=True)
    await run_default_http_fetcher("http://hop1.test/login")
    jar = cookie_jar.global_cookie_jar()
    assert "sess=hop1" in _header_for(jar, "http://hop1.test/dashboard")


async def test_cross_origin_redirect_reattaches_the_new_origins_cookies(monkeypatch):
    jar = cookie_jar.global_cookie_jar()
    jar.add_cookie_header(canonical("http://attacker.test/"), "sid=atk; Path=/")
    responses = [
        MockResponse(302, {"location": "http://attacker.test/steal"}, b""),
        MockResponse(200, {"content-type": "text/html"}, b"<html>pwned</html>"),
    ]
    client = patch_http_fetcher_transport(monkeypatch, responses, allow_ssrf_bypass=True)
    await run_default_http_fetcher("http://example.test/", {"Cookie": "user=example"})
    # Hop 2 carries only what attacker.test itself set before: the jar cookie
    # for the new origin is attached, the stripped origin cookie is not.
    hop2_cookie = client.captured_headers[1].get("Cookie")
    assert hop2_cookie == "sid=atk", hop2_cookie
    assert "user=example" not in hop2_cookie
