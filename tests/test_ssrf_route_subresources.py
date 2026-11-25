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


def _counting_resolver(lookups: list[str], ip: str = "1.2.3.4"):
    """A stubbed getaddrinfo recording every host it was asked for."""

    def counting(host, *args, **kwargs):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return counting


def _pinned_handler(monkeypatch, lookups: list[str]):
    """An SSRF route handler over the stubbed, recording resolver, opted out."""
    block_private_targets(monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo", _counting_resolver(lookups))
    return ssrf_route_handler()


async def test_https_subresource_does_not_dns_resolve_in_browser(monkeypatch) -> None:
    """S4: browser route handler does not resolve DNS; proxy owns resolution and
    rebinding defense. Handler admits via NormalizedUrl and continues logical URL."""
    lookups: list[str] = []
    block_private_targets(monkeypatch)
    monkeypatch.setattr(socket, "getaddrinfo", _counting_resolver(lookups))
    handler = ssrf_route_handler()
    await handler(FakeRoute(), FakeRequest("https://1.2.3.4/a", "document"))
    await handler(FakeRoute(), FakeRequest("https://1.2.3.4/b", "document"))
    assert lookups == [], f"browser handler must not DNS-resolve in S4: {lookups}"


async def test_https_subresource_continues_logical_url_no_host_override(monkeypatch) -> None:
    """S4: https subresources continue with logical URL; proxy owns dialing/SNI."""
    block_private_targets(monkeypatch)
    route = FakeRoute()
    await ssrf_route_handler()(route, FakeRequest("https://example.com/x", "document"))
    assert route.decision == "continue"
    assert route.continued_url == "https://example.com/x"
    assert route.continued_headers is ROUTE_ARG_UNSET


async def test_http_subresource_continues_logical_url_no_host_override(monkeypatch) -> None:
    """S4: plain HTTP no longer rewritten to pinned IP; proxy owns dialing."""
    lookups: list[str] = []
    handler = _pinned_handler(monkeypatch, lookups)
    route = FakeRoute()
    await handler(route, FakeRequest("http://1.2.3.4/x", "document"))
    assert route.decision == "continue"
    assert route.continued_url == "http://1.2.3.4/x"
    assert route.continued_headers is ROUTE_ARG_UNSET
    assert lookups == [], f"browser handler must not DNS-resolve in S4: {lookups}"
