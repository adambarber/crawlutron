"""S4: browser handlers do not DNS-resolve; proxy owns rebinding (#540/#1364)."""

from __future__ import annotations

import pytest

from tests._assertions import block_private_targets, resolves_in_sequence
from tests._browser_doubles import FakeRequest, FakeRoute, ssrf_route_handler
from tests._browser_doubles import FakeWS as _FakeWS
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical


async def test_https_subresource_continues_logical_url_without_browser_dns(monkeypatch) -> None:
    """S4: browser route handler does not DNS-resolve subresources; it admits
    via NormalizedUrl and continues with the logical URL. Proxy owns rebinding."""
    asked: list[str] = []

    def _no_resolve(*args, **kwargs):
        asked.append("resolved")
        raise AssertionError("browser handler must not resolve DNS in S4")

    monkeypatch.setattr("crawlutron.transport.pinned_dns.check_ssrf_protection_stable", _no_resolve)
    route = FakeRoute()
    await ssrf_route_handler()(route, FakeRequest("https://example.com/x", "document"))
    assert route.decision == "continue", route.decision
    assert route.continued_url == "https://example.com/x"
    assert asked == []


async def test_https_subresource_aborts_on_policy_denial(monkeypatch) -> None:
    """S4: NormalizedUrl admission remains fail-closed complement."""
    from crawlutron.transport.browser_targets import make_ssrf_route_handler
    from safe_url.host_policy import HostPolicy

    route = FakeRoute()
    handler = make_ssrf_route_handler(policy=HostPolicy.from_strings("allowed.test", ""))
    await handler(route, FakeRequest("https://evil.com/x", "document"))
    assert route.decision == "abort"


async def test_wss_target_forwards_without_browser_dns(monkeypatch) -> None:
    """S4: WS handler admits via NormalizedUrl and forwards; proxy owns DNS."""
    from crawlutron.transport.browser_targets import make_ssrf_websocket_handler

    def _no_resolve(*args, **kwargs):
        raise AssertionError("browser WS handler must not resolve DNS in S4")

    monkeypatch.setattr("crawlutron.transport.pinned_dns.check_ssrf_protection_stable", _no_resolve)
    ws = _FakeWS("wss://example.com/chat")
    handler = make_ssrf_websocket_handler(policy=UNRESTRICTED_HOST_POLICY)
    await handler(ws)
    assert ws.closed_count == 0
    assert ws.connect_calls == 1


def test_check_ssrf_protection_stable_refuses_changed_answer(monkeypatch) -> None:
    """The named verdict itself: two fresh resolves must agree exactly; a
    changed answer set raises (the private-answer law stays in force too)."""
    from crawlutron.transport.pinned_dns import check_ssrf_protection_stable

    resolves_in_sequence(monkeypatch, "93.184.216.34", "93.184.216.35")
    with pytest.raises(ValueError, match="changed"):
        check_ssrf_protection_stable(canonical("https://example.com/"))


def test_check_ssrf_protection_stable_allows_identical_public_answers(monkeypatch) -> None:
    """Two identical fresh public answers pass the stability verdict."""
    from crawlutron.transport.pinned_dns import check_ssrf_protection_stable

    resolves_in_sequence(monkeypatch, "93.184.216.34", "93.184.216.34")
    check_ssrf_protection_stable(canonical("https://example.com/"))


def test_check_ssrf_protection_stable_ignores_answer_order(monkeypatch) -> None:
    """The same answer set in a rotated order is stable: legit round-robin DNS
    must not read as rebinding (set comparison, not list comparison)."""
    import socket

    from crawlutron.transport.pinned_dns import check_ssrf_protection_stable

    block_private_targets(monkeypatch)
    answers = [
        [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 0)),
        ],
        [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
        ],
    ]
    calls = []

    def rotating(host, *args, **kwargs):
        calls.append(host)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(socket, "getaddrinfo", rotating)
    check_ssrf_protection_stable(canonical("https://example.com/"))
