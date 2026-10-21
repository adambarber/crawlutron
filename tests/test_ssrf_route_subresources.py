"""SSRF route handler: HTTPS subresources stay blocklist-checked without the DNS
pin an https connection never uses (no IP SAN), so it now runs the shared cached
validation-only check; plain HTTP keeps the fresh pin that governs the connection.
"""

from __future__ import annotations

import socket

from tests._assertions import block_private_targets
from tests._browser_doubles import (
    ROUTE_ARG_UNSET,
    FakeRequest,
    FakeRoute,
    assert_route_decision,
    ssrf_route_handler,
)


async def test_https_subresource_to_a_private_target_is_aborted(monkeypatch) -> None:
    """Every private/loopback/reserved HTTPS target is refused before connect."""
    block_private_targets(monkeypatch)
    await assert_route_decision(
        ssrf_route_handler(),
        (
            "https://127.0.0.1:9/",
            "https://169.254.169.254/latest/meta-data/",
            "https://[::1]:9/",
            "https://localhost:9/admin",
        ),
        "abort",
    )


async def test_https_subresource_is_not_rewritten_to_a_pinned_ip(monkeypatch) -> None:
    """The browser keeps the original hostname for HTTPS (SNI/cert verification)."""
    block_private_targets(monkeypatch)
    handler = ssrf_route_handler()
    route = FakeRoute()
    await handler(route, FakeRequest("https://1.2.3.4/x", "document"))
    assert route.decision == "continue"
    assert route.continued_url == "https://1.2.3.4/x"
    assert route.continued_headers is ROUTE_ARG_UNSET


async def test_https_subresource_revalidates_fresh_per_request(monkeypatch) -> None:
    """HTTPS validation must not trust a cached answer: the browser re-resolves
    at connect, so a cached validation up to the TTL stale would let a rebind in
    that window past -- each subresource validates against a fresh resolve (#387)."""
    block_private_targets(monkeypatch)
    lookups: list[str] = []
    real_getaddrinfo = socket.getaddrinfo

    def counting(addr, *args, **kwargs):
        lookups.append(addr)
        return real_getaddrinfo(addr, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", counting)
    handler = ssrf_route_handler()
    await handler(FakeRoute(), FakeRequest("https://1.2.3.4/a", "document"))
    await handler(FakeRoute(), FakeRequest("https://1.2.3.4/b", "document"))
    # Two subresources -> two fresh validations, never one cached answer reused.
    assert len(lookups) == 2, f"expected one fresh lookup per subresource, got {lookups}"


async def test_https_subresource_refuses_a_mixed_public_private_answer(monkeypatch) -> None:
    """A mixed public/private DNS answer is refused on the browser https path: the
    browser dials its own connect and may pick the private member, so it cannot
    select the public one the way the fetch pin does (which owns its dial) (#387)."""
    block_private_targets(monkeypatch)

    def mixed(host, *args, **kwargs):
        del host, args, kwargs
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", mixed)
    route = FakeRoute()
    await ssrf_route_handler()(route, FakeRequest("https://example.com/x", "document"))
    assert route.decision == "abort"


async def test_http_subresource_still_connects_to_the_fresh_pinned_ip(monkeypatch) -> None:
    """Plain HTTP keeps the pin: the connection is forced to the resolved IP
    with the original hostname preserved, and the resolve stays uncached so a
    DNS rebind inside the cache TTL cannot slip past the check."""
    block_private_targets(monkeypatch)
    lookups: list[str] = []
    real_getaddrinfo = socket.getaddrinfo

    def counting(host, *args, **kwargs):
        lookups.append(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", counting)
    handler = ssrf_route_handler()
    route = FakeRoute()
    await handler(route, FakeRequest("http://1.2.3.4/x", "document"))
    assert route.decision == "continue"
    assert route.continued_url == "http://1.2.3.4/x"
    assert route.continued_headers == {"Host": "1.2.3.4"}
    # The pin governs the connection, so each request resolves again rather than reusing a cached IP: a rebind
    # inside the cache TTL cannot slip past a connection that never looked.
    lookups_after_first = len(lookups)
    await handler(FakeRoute(), FakeRequest("http://1.2.3.4/y", "document"))
    assert len(lookups) > lookups_after_first, (
        f"the second pin reused the first resolve instead of resolving again: {lookups}"
    )
