"""Unit tests for the shared manual-redirect decision helper (both fetchers)."""

from functools import partial

from app.crawl.strategies.pinned_transport import _MAX_REDIRECT_HOPS
from app.crawl.strategies.pinned_transport import _redirect_target as _redirect_target_with_policy
from app.validation import HostPolicy

_redirect_target = partial(
    _redirect_target_with_policy,
    policy=HostPolicy.unrestricted(),
)


def test_redirect_target_follows_in_budget_redirect():
    result = _redirect_target("http://a.com/x", {"Host": "a.com"}, 302, "/y", 0)
    assert result == ("http://a.com/y", {"Host": "a.com"})


def test_redirect_target_none_on_terminal_status():
    assert _redirect_target("http://a.com/", {}, 200, None, 0) is None
    assert _redirect_target("http://a.com/", {}, 404, "/elsewhere", 0) is None


def test_redirect_target_none_when_hop_budget_exhausted():
    assert _redirect_target("http://a.com/", {}, 301, "/y", _MAX_REDIRECT_HOPS) is None


def test_redirect_target_strips_sensitive_headers_cross_host():
    headers = {"Host": "a.com", "Cookie": "s=1", "Authorization": "Bearer t", "Accept": "*/*"}
    result = _redirect_target("http://a.com/", headers, 301, "http://b.com/z", 0)
    assert result is not None, "a same-hop-count cross-host redirect is still a redirect"
    new_url, new_headers = result
    assert new_url == "http://b.com/z"
    assert "Cookie" not in new_headers and "Authorization" not in new_headers
    assert new_headers["Accept"] == "*/*"
    # original dict must not be mutated (caller keeps per-hop copy semantics)
    assert headers["Cookie"] == "s=1"


def test_redirect_target_keeps_sensitive_headers_within_one_origin():
    """A path change within one origin. Renamed from "same_host".

    The boundary is the origin, not the host, so a test named for the host was
    asserting something weaker than the code guarantees -- and, read the other
    way, implying that same host was sufficient, which is exactly the bug #69
    fixed.
    """
    headers = {"Cookie": "s=1"}
    result = _redirect_target("http://a.com/a", headers, 307, "/b", 2)
    assert result is not None
    _, new_headers = result
    assert new_headers["Cookie"] == "s=1"


def test_redirect_target_strips_sensitive_headers_on_a_port_change():
    """A different port on the same host is a different origin, and a different service.

    RFC 6454 puts the port in the origin, and the service already believes that
    elsewhere: ``credentials.origin_of`` is ``scheme://host[:port]`` with default
    ports dropped, and ``apply_credentials`` sends a credential only to that exact
    origin. So a credential scoped to ``https://a.com:443`` is not sent to
    ``https://a.com:8443``, while one attached to ``https://a.com`` is. Two
    chokepoints answering "is this the same origin?" with different answers, and
    this one was the looser of the pair.
    """
    headers = {"Cookie": "s=1", "Authorization": "Bearer t", "Accept": "*/*"}
    result = _redirect_target("https://a.com/x", headers, 301, "https://a.com:8443/z", 0)
    assert result is not None, "a same-hop-count redirect is still a redirect"
    new_url, new_headers = result
    assert new_url == "https://a.com:8443/z"
    assert "Cookie" not in new_headers and "Authorization" not in new_headers
    assert new_headers["Accept"] == "*/*"


def test_redirect_target_strips_sensitive_headers_on_a_scheme_downgrade():
    """https to http on the same host is the same class of hop.

    The scrub compared hostnames, so a downgrade kept the credential; and a
    downgrade is the direction where sending it matters most.
    """
    headers = {"Cookie": "s=1", "Authorization": "Bearer t"}
    result = _redirect_target("https://a.com/x", headers, 301, "http://a.com/z", 0)
    assert result is not None
    _, new_headers = result
    assert "Cookie" not in new_headers and "Authorization" not in new_headers


def test_redirect_target_keeps_sensitive_headers_across_a_default_port_spelling():
    """A hop that only respells the default port is not a boundary crossing.

    The other half of the origin rule, and the one a naive ``host:port`` string
    comparison gets wrong: ``https://a.com`` and ``https://a.com:443`` are the
    same origin, so scrubbing on that hop would drop the session on every
    legitimate redirect that spells the port out.
    """
    headers = {"Cookie": "s=1", "Authorization": "Bearer t"}
    result = _redirect_target("https://a.com/x", headers, 301, "https://a.com:443/z", 0)
    assert result is not None
    _, new_headers = result
    assert new_headers["Cookie"] == "s=1"
    assert new_headers["Authorization"] == "Bearer t"
