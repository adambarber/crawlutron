"""SSRF route handler: HTTPS subresources stay blocklist-checked without
paying for a DNS-rebinding pin the connection never uses.

``SafeUrl.connect`` keeps the original hostname for ``https`` (real certs carry
no IP SAN, so a pinned IP would only break SNI/cert verification). The route
handler therefore validated HTTPS subresources with an *uncached* pin resolve
whose result was discarded — one ``socket.getaddrinfo`` per subresource through
the default executor. HTTPS now goes through the cached validation-only check
the websocket guard and the HTTP fetcher already use; plain HTTP keeps the
fresh pin because there the pinned IP governs the actual connection.
"""

from __future__ import annotations

import socket

from _assertions import block_private_targets
from _browser_doubles import ROUTE_ARG_UNSET, FakeRequest, FakeRoute
from _policy_factories import UNRESTRICTED_HOST_POLICY

import app.crawl.strategies.pinned_transport as http_mod
from app.crawl.strategies.browser import _make_ssrf_route_handler


async def test_https_subresource_to_a_private_target_is_aborted(monkeypatch) -> None:
    """Every private/loopback/reserved HTTPS target is refused before connect."""
    block_private_targets(monkeypatch)
    handler = _make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)
    for url in (
        "https://127.0.0.1:9/",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]:9/",
        "https://localhost:9/admin",
    ):
        route = FakeRoute()
        await handler(route, FakeRequest(url, "document"))
        assert route.decision == "abort", f"private target was not blocked: {url}"


async def test_https_subresource_is_not_rewritten_to_a_pinned_ip(monkeypatch) -> None:
    """The browser keeps the original hostname for HTTPS (SNI/cert verification)."""
    block_private_targets(monkeypatch)
    handler = _make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)
    route = FakeRoute()
    await handler(route, FakeRequest("https://1.2.3.4/x", "document"))
    assert route.decision == "continue"
    assert route.continued_url == "https://1.2.3.4/x"
    assert route.continued_headers is ROUTE_ARG_UNSET


async def test_https_subresource_reuses_the_dns_lookup(monkeypatch) -> None:
    """HTTPS validation is served by the shared TTL'd resolver: one lookup per
    host, not one uncached resolve per subresource."""
    block_private_targets(monkeypatch)
    http_mod._DNS_CACHE.clear()
    lookups: list[str] = []
    real_getaddrinfo = socket.getaddrinfo

    def counting(addr, *args, **kwargs):
        lookups.append(addr)
        return real_getaddrinfo(addr, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", counting)
    handler = _make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)
    await handler(FakeRoute(), FakeRequest("https://1.2.3.4/a", "document"))
    await handler(FakeRoute(), FakeRequest("https://1.2.3.4/b", "document"))
    assert len(lookups) == 1, f"expected one shared lookup, got {lookups}"


async def test_http_subresource_still_connects_to_the_fresh_pinned_ip(monkeypatch) -> None:
    """Plain HTTP keeps the pin: the connection is forced to the resolved IP
    with the original hostname preserved, and the resolve stays uncached so a
    DNS rebind inside the cache TTL cannot slip past the check."""
    block_private_targets(monkeypatch)
    resolves: list[bool] = []
    real_resolve = http_mod._resolve_ips

    def recording(host, *, use_cache=True):
        resolves.append(use_cache)
        return real_resolve(host, use_cache=use_cache)

    monkeypatch.setattr(http_mod, "_resolve_ips", recording)
    handler = _make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)
    route = FakeRoute()
    await handler(route, FakeRequest("http://1.2.3.4/x", "document"))
    assert route.decision == "continue"
    assert route.continued_url == "http://1.2.3.4/x"
    assert route.continued_headers == {"Host": "1.2.3.4"}
    # The pin governs the connection, so its resolve is still uncached.
    assert resolves and not all(resolves), f"pin resolve went through the cache: {resolves}"
