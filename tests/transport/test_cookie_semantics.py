"""RFC 6265 cookie semantics for the jar: attributes, deletion, and redirect hops.

Every test states the RFC rule it pins. The jar delegates parse and send
matching to stdlib http.cookiejar; these tests pin the observable behavior.
"""

from __future__ import annotations

import json

import pytest

from crawlutron.storage.coordination_cookies import SharedCookieStore
from crawlutron.transport import cookie_jar
from crawlutron.transport.cookie_jar import MAX_COOKIE_LIFETIME_SECONDS, CookieJar
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


@pytest.mark.parametrize("bad", ["--5", "", "abc", "1.5", "+5"])
def test_malformed_max_age_is_ignored_not_raised(bad):
    """A remote server must not be able to fail our fetch with a bad header
    (#395): a non-numeric Max-Age is ignored -- the cookie is stored as a session
    cookie and the response's OTHER cookies are still stored."""
    jar = CookieJar()
    jar.add_cookie_header(canonical("https://a.example/"), f"bad=x; Max-Age={bad}; Path=/")
    jar.add_cookie_header(canonical("https://a.example/"), "good=y; Path=/")
    header = _header_for(jar, "https://a.example/")
    assert "good=y" in header
    assert "bad=x" in header


# RFC 6265bis 4.1.2.2/5.5 (#399): a cookie's lifetime is capped at 400 days.

_RFC_MAX_LIFETIME_SECONDS = 400 * 24 * 60 * 60


def test_huge_numeric_max_age_is_clamped_to_the_rfc_lifetime(monkeypatch):
    """A 40-digit Max-Age parses fine (Python integers are unbounded), so without
    the cap time.time() + 10**40 pins the cookie in the jar for ever (#399)."""
    jar = CookieJar()
    now = 1_000_000.0
    monkeypatch.setattr(cookie_jar.time, "time", lambda: now)
    jar.add_cookie_header(canonical("https://a.example/"), "bad=x; Max-Age=" + "9" * 40)
    # The module names the RFC number as the one constant both read and enforced.
    assert cookie_jar.MAX_COOKIE_LIFETIME_SECONDS == _RFC_MAX_LIFETIME_SECONDS
    # Clamped, not dropped: inside the cap the cookie is still stored and sent.
    monkeypatch.setattr(cookie_jar.time, "time", lambda: now + _RFC_MAX_LIFETIME_SECONDS - 60)
    assert _header_for(jar, "https://a.example/") == "bad=x"
    # Past the cap it is dead: a site cannot pin a cookie in the jar for ever.
    monkeypatch.setattr(cookie_jar.time, "time", lambda: now + _RFC_MAX_LIFETIME_SECONDS + 60)
    assert _header_for(jar, "https://a.example/") == ""


def test_huge_expires_date_is_clamped_to_the_rfc_lifetime_too(monkeypatch):
    """The cap bounds the cookie, not one attribute: an Expires far in the future
    lands on the same 400-day ceiling."""
    jar = CookieJar()
    now = 1_000_000.0
    monkeypatch.setattr(cookie_jar.time, "time", lambda: now)
    # parse_ns_headers folds an Expires date into an epoch; a year-3000 date is
    # a far-future epoch without ever being a malformed one.
    jar.add_cookie_header(canonical("https://a.example/"), "bad=x; Expires=Fri, 01 Jan 3000 00:00:00 GMT")
    monkeypatch.setattr(cookie_jar.time, "time", lambda: now + _RFC_MAX_LIFETIME_SECONDS + 60)
    assert _header_for(jar, "https://a.example/") == ""


def test_malformed_max_age_never_escapes_the_store_path():
    """The ValueError from int('--5') must be absorbed in _expiry, not escape
    through add_cookie_header into the strategy's error path."""
    jar = CookieJar()
    jar.add_cookie_header(canonical("https://a.example/"), "sid=abc; Max-Age=--5; Path=/")
    assert _header_for(jar, "https://a.example/") == "sid=abc"


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


# RFC 6265bis 5.3 step 6.3 (#438): a public-suffix ``Domain=`` injects into
# every host under the suffix; only the request host itself stays.


@pytest.mark.parametrize(
    ("origin", "header", "victim"),
    [
        ("https://evil.com/", "sid=secret; Domain=com", "https://victim.com/"),
        ("https://evil.co.uk/", "sid=secret; Domain=co.uk", "https://victim.co.uk/"),
    ],
)
def test_public_suffix_domain_attribute_is_refused_outright(origin, header, victim):
    """A ``Domain=`` naming a public suffix is refused, never stored (#438).

    A bare suffix match accepts it, and the send side then matches every
    host under the suffix in the namespace.
    """
    jar = CookieJar()
    jar.add_cookie_header(canonical(origin), header)
    assert _header_for(jar, victim) == ""
    # Refused outright, not degraded: not even the setting host replays it.
    assert _header_for(jar, origin) == ""


def test_public_suffix_domain_equal_to_request_host_degrades_to_host_only():
    """6265bis: Domain= naming the request host itself stays, host-only.

    A bare refusal would drop a legitimate same-site declaration; the RFC
    pins it to exactly the host that set it, never the sibling sites.
    """
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://co.uk/"), "sid=abc; Domain=co.uk")
    assert _header_for(jar, "http://co.uk/") == "sid=abc"
    assert _header_for(jar, "http://x.co.uk/") == ""


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


async def test_hop_cookie_is_not_planted_when_the_next_pin_refuses(monkeypatch):
    """RFC 6265 5.3 makes persistence the fetcher's choice, so the walk defers
    a hop's Set-Cookie until the NEXT pin admits it: a walk dying at a refused
    pin plants nothing (#525 SEC-06) -- no state leaks to an unreached target."""

    def refusing_pin(url, headers):
        if url.host == "refused.test":
            raise ValueError("SSRF protection: cannot resolve hostname refused.test")
        return str(url), dict(headers)

    responses = [
        MockResponse(302, {"location": "http://refused.test/x", "set-cookie": "sess=defer1; Path=/"}, b""),
    ]
    patch_http_fetcher_transport(monkeypatch, responses, allow_ssrf_bypass=True)
    with pytest.raises(ValueError, match="refused.test"):
        await run_default_http_fetcher("http://deferred.test/login", pin=refusing_pin)
    jar = cookie_jar.global_cookie_jar()
    assert _header_for(jar, "http://deferred.test/") == ""


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


# Tombstone reaping (#393): a deletion record must not accumulate forever, and
# a reaped slot must not resurrect the deleted cookie it carried.


def test_reap_drops_the_deleted_cookie_its_record_and_keeps_the_deletion(tmp_path, monkeypatch):
    """#393: a host that sets and deletes N distinct cookie names must not leave
    N records behind once the union window has passed, the live cookie must
    survive its neighbour's reap, and the deletion must still hold after it."""
    monkeypatch.setattr(cookie_jar, "TOMBSTONE_WINDOW_SECONDS", 0.0)
    store = SharedCookieStore(tmp_path / "coordination.db")
    jar = CookieJar(store=store)
    for i in range(10):
        name = f"tmp{i}"
        jar.add_cookie_header(canonical("http://a.example/"), f"{name}=v; Path=/")
        jar.add_cookie_header(canonical("http://a.example/"), f"{name}=; Max-Age=0; Path=/")
    jar.add_cookie_header(canonical("http://a.example/"), "sid=abc; Path=/")
    # A zero window means every read past the deletion reaps the tombstones.
    assert _header_for(jar, "http://a.example/") == "sid=abc"  # deletion still holds
    assert _header_for(jar, "http://a.example/") == "sid=abc"  # no resurrection on re-read
    slot = store.load("", "a.example")
    assert set(slot) == {"sid"}, slot


def test_tombstone_within_the_window_is_retained_and_propagates(tmp_path, monkeypatch):
    """#393: inside the union window the tombstone stays in the slot, so a
    sibling reading the slot still sees the deletion."""
    monkeypatch.setattr(cookie_jar, "TOMBSTONE_WINDOW_SECONDS", 3600.0)
    store = SharedCookieStore(tmp_path / "coordination.db")
    jar = CookieJar(store=store)
    jar.add_cookie_header(canonical("http://a.example/"), "sid=abc; Path=/")
    jar.add_cookie_header(canonical("http://a.example/"), "sid=; Max-Age=0; Path=/")
    slot = store.load("", "a.example")
    record = json.loads(slot["sid"])
    assert record["value"] == "" and record["expires"] == 0.0
    # And a fresh process reading through a new jar sees the deletion too.
    sibling = CookieJar(store=store)
    assert _header_for(sibling, "http://a.example/") == ""


def _zero_window_jar(tmp_path, monkeypatch) -> tuple[object, object]:
    """A store + jar with the tombstone window closed, so reads reap immediately."""
    monkeypatch.setattr(cookie_jar, "TOMBSTONE_WINDOW_SECONDS", 0.0)
    store = SharedCookieStore(tmp_path / "coordination.db")
    return store, CookieJar(store=store)


def test_the_expiry_cap_boundary_instant_is_expired_not_live(monkeypatch):
    """#406: a cookie AT exactly the cap instant is dead, one a second short is
    alive. Pins the comparison the expiry check uses -- a '<' instead of '<='
    would make the exact-cap cookie live and no other test would notice."""
    monkeypatch.setattr(cookie_jar.time, "time", lambda: 1_000_000.0)
    jar = CookieJar()
    # Max-Age exactly the cap: the expiry is now + cap, the boundary instant.
    jar.add_cookie_header(
        canonical("http://a.example/"), f"sid=abc; Path=/; Max-Age={MAX_COOKIE_LIFETIME_SECONDS}"
    )
    # A read AT the boundary instant (cap seconds later) finds it expired.
    monkeypatch.setattr(cookie_jar.time, "time", lambda: 1_000_000.0 + MAX_COOKIE_LIFETIME_SECONDS)
    assert jar.cookie_header_for(canonical("http://a.example/")) == ""
    # A read a second short of the boundary finds it alive.
    monkeypatch.setattr(cookie_jar.time, "time", lambda: 1_000_000.0 + MAX_COOKIE_LIFETIME_SECONDS - 1)
    assert jar.cookie_header_for(canonical("http://a.example/")) == "sid=abc"


def test_prune_never_crosses_the_namespace_boundary(tmp_path, monkeypatch):
    """#393 walk-past: reaping one namespace's tombstones must not touch another
    namespace's slot, and a deletion in one namespace never deletes another's."""
    store, jar = _zero_window_jar(tmp_path, monkeypatch)
    jar.add_cookie_header(canonical("http://a.example/"), "k=v; Path=/", namespace="t1")
    jar.add_cookie_header(canonical("http://a.example/"), "k=v; Path=/", namespace="t2")
    jar.add_cookie_header(canonical("http://a.example/"), "k=; Max-Age=0; Path=/", namespace="t1")
    jar.add_cookie_header(canonical("http://a.example/"), "other=w; Path=/", namespace="t2")
    assert jar.cookie_header_for(canonical("http://a.example/"), namespace="t1") == ""
    assert jar.cookie_header_for(canonical("http://a.example/"), namespace="t2") == "k=v; other=w"
    assert set(store.load("t2", "a.example")) == {"k", "other"}


def test_the_reap_scopes_its_slot_writes_to_the_tombstones_own_namespace(tmp_path, monkeypatch):
    """#401: the reap of namespace t1's tombstones runs while t2 holds live cookies
    for the same host; t2's slot must be untouched and its cookies still sent.
    A prune whose slot write ignores the namespace would erase t2 here."""
    store, jar = _zero_window_jar(tmp_path, monkeypatch)
    jar.add_cookie_header(canonical("http://a.example/"), "dead=x; Path=/", namespace="t1")
    jar.add_cookie_header(canonical("http://a.example/"), "dead=; Max-Age=0; Path=/", namespace="t1")
    jar.add_cookie_header(canonical("http://a.example/"), "live=y; Path=/", namespace="t2")
    # The read that reaps t1's expired tombstone.
    assert jar.cookie_header_for(canonical("http://a.example/"), namespace="t1") == ""
    # t2's slot survived the reap and its cookie is still sent.
    assert set(store.load("t2", "a.example")) == {"live"}
    assert jar.cookie_header_for(canonical("http://a.example/"), namespace="t2") == "live=y"
    # The reap emptied t1's slot; a re-read does not resurrect the deletion.
    assert set(store.load("t1", "a.example")) == set()
