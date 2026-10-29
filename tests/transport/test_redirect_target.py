"""Unit tests for the shared manual-redirect decision helper (both fetchers)."""

import httpx
import pytest

from crawlutron.transport.pinned_transport import MAX_REDIRECT_HOPS, redirect_target
from tests._policy_factories import canonical


async def test_redirect_target_follows_in_budget_redirect():
    result = await redirect_target(canonical("http://a.com/x"), {"Host": "a.com"}, 302, "/y", 0)
    assert result == (canonical("http://a.com/y"), {"Host": "a.com"})


async def test_redirect_target_none_on_terminal_status():
    assert await redirect_target(canonical("http://a.com/"), {}, 200, None, 0) is None
    assert await redirect_target(canonical("http://a.com/"), {}, 404, "/elsewhere", 0) is None


async def test_redirect_target_raises_when_hop_budget_exhausted():
    # Hop exhaustion is distinct from a terminal answer: a 301 at the hop cap is
    # still a redirect, so its body must not be read as page content (#217).
    with pytest.raises(httpx.TooManyRedirects):
        await redirect_target(canonical("http://a.com/"), {}, 301, "/y", MAX_REDIRECT_HOPS)


async def test_redirect_target_strips_sensitive_headers_cross_host():
    headers = {"Host": "a.com", "Cookie": "s=1", "Authorization": "Bearer t", "Accept": "*/*"}
    result = await redirect_target(canonical("http://a.com/"), headers, 301, "http://b.com/z", 0)
    assert result is not None, "a same-hop-count cross-host redirect is still a redirect"
    new_url, new_headers = result
    assert new_url == canonical("http://b.com/z")
    assert "Cookie" not in new_headers and "Authorization" not in new_headers
    assert new_headers["Accept"] == "*/*"
    # original dict must not be mutated (caller keeps per-hop copy semantics)
    assert headers["Cookie"] == "s=1"


async def test_redirect_target_keeps_sensitive_headers_within_one_origin():
    """A path change within one origin. Renamed from "same_host".

    The boundary is the origin, not the host; a test named for the host
    asserted something weaker -- implying same host was sufficient, bug #69.
    """
    headers = {"Cookie": "s=1"}
    result = await redirect_target(canonical("http://a.com/a"), headers, 307, "/b", 2)
    assert result is not None
    _, new_headers = result
    assert new_headers["Cookie"] == "s=1"


async def test_redirect_target_strips_sensitive_headers_on_a_port_change():
    """A different port on the same host is a different origin, and a service.

    RFC 6454 puts the port in the origin, and ``credentials.origin_of`` already
    believes that; this chokepoint was the looser of the two answers.
    """
    headers = {"Cookie": "s=1", "Authorization": "Bearer t", "Accept": "*/*"}
    result = await redirect_target(canonical("https://a.com/x"), headers, 301, "https://a.com:8443/z", 0)
    assert result is not None, "a same-hop-count redirect is still a redirect"
    new_url, new_headers = result
    assert new_url == canonical("https://a.com:8443/z")
    assert "Cookie" not in new_headers and "Authorization" not in new_headers
    assert new_headers["Accept"] == "*/*"


async def test_redirect_target_strips_sensitive_headers_on_a_scheme_downgrade():
    """https to http on the same host is the same class of hop.

    The scrub compared hostnames, so a downgrade kept the credential; and a
    downgrade is the direction where sending it matters most.
    """
    headers = {"Cookie": "s=1", "Authorization": "Bearer t"}
    result = await redirect_target(canonical("https://a.com/x"), headers, 301, "http://a.com/z", 0)
    assert result is not None
    _, new_headers = result
    assert "Cookie" not in new_headers and "Authorization" not in new_headers


async def test_redirect_target_keeps_sensitive_headers_across_a_default_port_spelling():
    """A hop that only respells the default port is not a boundary crossing.

    ``https://a.com`` and ``https://a.com:443`` are the same origin, so a naive
    ``host:port`` compare would scrub the session on a legitimate redirect.
    """
    headers = {"Cookie": "s=1", "Authorization": "Bearer t"}
    result = await redirect_target(canonical("https://a.com/x"), headers, 301, "https://a.com:443/z", 0)
    assert result is not None
    _, new_headers = result
    assert new_headers["Cookie"] == "s=1"
    assert new_headers["Authorization"] == "Bearer t"
