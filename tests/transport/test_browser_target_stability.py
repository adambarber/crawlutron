"""#540: the browser's HTTPS/wss check-then-dial residual window.

The browser performs its own DNS and connect after the route/ws handler's
validation (no dial ownership -- #387), so the last verifiable moment is the
handler itself: fail-closed on instability (two fresh resolves must return the
identical public answer set). A changed answer is the rebinding signature; the
documented residual (a stable-through-check flip before the browser's own
resolve) needs dial ownership to close and is refused by architecture -- no
MITM proxy, no CDP. Full analysis lives on the ticket.
"""

from __future__ import annotations

import pytest

from tests._assertions import block_private_targets, resolves_in_sequence
from tests._browser_doubles import FakeRequest, FakeRoute, ssrf_route_handler
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical
from tests.transport.test_websocket_ssrf import _FakeWS


async def test_https_subresource_refuses_a_changed_fresh_answer(monkeypatch) -> None:
    """The browser dials after the check with no dial ownership (#540): two fresh
    resolves are the last verifiable moment, so a second answer differing from the
    first is the rebinding signature and must abort, not continue."""
    asked = resolves_in_sequence(monkeypatch, "93.184.216.34", "93.184.216.35")
    route = FakeRoute()
    await ssrf_route_handler()(route, FakeRequest("https://example.com/x", "document"))
    assert route.decision == "abort", route.decision
    assert len(asked) == 2, f"the verdict must rest on two fresh resolves, got {asked}"


async def test_https_subresource_allows_a_stable_fresh_answer(monkeypatch) -> None:
    """Two identical fresh public answers pass: stability refusal must not
    over-refuse a legitimately stable host (the guard is fail-closed on
    CHANGE, not on resolution count)."""
    resolves_in_sequence(monkeypatch, "93.184.216.34", "93.184.216.34")
    route = FakeRoute()
    await ssrf_route_handler()(route, FakeRequest("https://example.com/x", "document"))
    assert route.decision == "continue", route.decision


async def test_wss_target_refuses_a_changed_fresh_answer(monkeypatch) -> None:
    """The wss path has the same no-dial-ownership residual (#540): a fresh
    answer that differs from its immediate repeat closes the socket."""
    from app.transport.browser_targets import make_ssrf_websocket_handler

    resolves_in_sequence(monkeypatch, "93.184.216.34", "93.184.216.35")
    ws = _FakeWS("wss://example.com/chat")
    handler = make_ssrf_websocket_handler(policy=UNRESTRICTED_HOST_POLICY)
    await handler(ws)
    assert ws.closed_count == 1, "an unstable wss target was not closed"
    assert ws.connect_calls == 0


def test_check_ssrf_protection_stable_refuses_changed_answer(monkeypatch) -> None:
    """The named verdict itself: two fresh resolves must agree exactly; a
    changed answer set raises (the private-answer law stays in force too)."""
    from app.transport.pinned_transport import check_ssrf_protection_stable

    resolves_in_sequence(monkeypatch, "93.184.216.34", "93.184.216.35")
    with pytest.raises(ValueError, match="changed"):
        check_ssrf_protection_stable(canonical("https://example.com/"))


def test_check_ssrf_protection_stable_allows_identical_public_answers(monkeypatch) -> None:
    """Two identical fresh public answers pass the stability verdict."""
    from app.transport.pinned_transport import check_ssrf_protection_stable

    resolves_in_sequence(monkeypatch, "93.184.216.34", "93.184.216.34")
    check_ssrf_protection_stable(canonical("https://example.com/"))


def test_check_ssrf_protection_stable_ignores_answer_order(monkeypatch) -> None:
    """The same answer set in a rotated order is stable: legit round-robin DNS
    must not read as rebinding (set comparison, not list comparison)."""
    import socket

    from app.transport.pinned_transport import check_ssrf_protection_stable

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
