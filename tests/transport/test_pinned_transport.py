"""The logical-request contract: no app-side pin, no connect URL, one redirect walk."""

from __future__ import annotations

import pytest

from crawlutron.transport import pinned_transport as pinned_mod
from crawlutron.transport.pinned_dns import DNS_WAIT_SECONDS, dns_wait_for
from crawlutron.transport.redirect_walker import RedirectWalker
from crawlutron.transport.transport import FetchRequest
from tests._fetch_doubles import make_fetch
from tests._fetcher_factories import ScriptedTransport, _void_store_cookies
from tests._policy_factories import canonical


def test_the_derivation_never_exceeds_the_posture():
    """The request budget tightens the proxy's DNS wait; the posture is the ceiling."""
    assert dns_wait_for(0.3) == 0.3
    assert dns_wait_for(2) == 2
    assert dns_wait_for(30) == DNS_WAIT_SECONDS
    assert dns_wait_for(None) == DNS_WAIT_SECONDS


class TestHeaderValueWireGuard:
    """The redirect walk is the one edge both transports send through, so a header
    VALUE that could end one header and start another (CR/LF/NUL) is refused there.
    """

    async def test_caller_crlf_value_is_refused_before_the_first_hop(self):
        with pytest.raises(ValueError):
            await RedirectWalker(ScriptedTransport(), _void_store_cookies).walk(
                canonical("http://example.com/"),
                {"X-Tenant": "a\r\nCookie: admin=1"},
                10,
            )

    async def test_clean_multibyte_headers_reach_the_walk_untouched(self):
        transport = ScriptedTransport(
            [make_fetch(status=200, body=b"ok", url="http://example.com/", final_url="http://example.com/")]
        )
        await RedirectWalker(transport, _void_store_cookies).walk(
            canonical("http://example.com/"), {"X-Tenant": "Café"}, 10
        )
        request = transport.calls[0][0]
        assert isinstance(request, FetchRequest)
        assert request.headers["X-Tenant"] == "Café"


def test_one_redirect_walk_on_the_surface():
    """One redirect walk on the surface: RedirectWalker. The app-side pin surface
    is retired -- PinnedRequest, PinRule and both builders are gone."""
    assert not hasattr(pinned_mod, "RedirectChain")
    assert not hasattr(pinned_mod, "apinned_hops")
    assert not hasattr(pinned_mod, "apinned_stream")
    assert not hasattr(pinned_mod, "PinnedRequest")
    assert not hasattr(pinned_mod, "PinRule")
    assert not hasattr(pinned_mod, "build_pinned_request")
    assert not hasattr(pinned_mod, "build_pinned_request_async")
    assert RedirectWalker.__name__ == "RedirectWalker"


def test_logical_request_has_no_connect_url_and_keeps_the_logical_identity():
    """TLS identity is derived from the logical host; a connect URL is inexpressible."""
    url = canonical("https://example.com/secure.pdf")
    request = FetchRequest(headers={"Host": "example.com"}, url=url, timeout_seconds=10.0)
    assert request.tls_hostname == "example.com"
    assert not hasattr(request, "connect_url")
    assert request.url is url
