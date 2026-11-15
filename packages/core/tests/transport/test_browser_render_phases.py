"""The SSRF WebSocket guard fails closed when the browser cannot intercept sockets.

A context without ``route_web_socket`` refuses the render with a named refusal;
a context that has the capability still renders and installs the guard.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from crawlutron.models.intercepted_xhr_record import InterceptedXhrRecord
from crawlutron.transport.browser_render_phases import (
    WebSocketGuardUnavailable,
    WireGuard,
    wire_ssrf_guards,
)
from crawlutron.transport.pinned_transport import PinnedRequest
from crawlutron.transport.render_page import GuardContext
from tests._browser_page_doubles import BrowserFakePage, FakePageContext
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, admitted

_TARGET = "http://1.2.3.4/"


def _dummy_pin(*args: object, **kwargs: object) -> PinnedRequest:
    """A pin that is never reached: the refusal fires before any route is wired."""
    return PinnedRequest(
        connect_url=_TARGET,
        headers={},
        url=admitted(_TARGET, policy=UNRESTRICTED_HOST_POLICY),
    )


class _SocketlessContext:
    """A page context with no ``route_web_socket`` so the guard refuses the
    render instead of installing an unguarded socket channel.
    """

    async def route(self, pattern: str, handler: Callable[..., object]) -> object:
        return None


def _socketless_page() -> BrowserFakePage:
    """A page double whose context cannot intercept websockets."""
    return BrowserFakePage(context=cast(GuardContext, _SocketlessContext()))


def _wire() -> WireGuard:
    """The wire bundle every render guard test builds; the pin is never reached."""
    return WireGuard(
        pin_sync=_dummy_pin,
        policy=UNRESTRICTED_HOST_POLICY,
        timeout_seconds=10.0,
    )


async def _refuses_render(page) -> str:
    """Run the guard path on a socketless page; return the refusal message."""
    wire = _wire()
    with pytest.raises(WebSocketGuardUnavailable) as excinfo:
        await wire_ssrf_guards(page, wire=wire, profile=None, on_response=[])
    return str(excinfo.value)


class TestWebSocketGuardFailsClosed:
    """The render is refused when the browser cannot intercept websockets."""

    async def test_refuses_render_when_context_has_no_route_web_socket(self):
        """A page whose context lacks route_web_socket raises the named refusal."""
        page = _socketless_page()
        message = await _refuses_render(page)
        assert "route_web_socket" in message
        assert "Playwright >= 1.62" in message
        assert "unguarded socket channel" in message

    async def test_refusal_names_missing_capability_and_supported_version(self):
        """The refusal message says what is missing and which Playwright supports it."""
        page = _socketless_page()
        message = await _refuses_render(page)
        assert "route_web_socket" in message
        assert "1.62" in message


class TestWebSocketGuardInstalledWhenCapable:
    """A context with route_web_socket still renders and installs the guard."""

    async def test_renders_and_installs_websocket_guard(self):
        """A capable page proceeds and the websocket guard is installed."""
        context = FakePageContext()
        page = BrowserFakePage(context=context)
        wire = _wire()
        on_response: list[InterceptedXhrRecord] = []
        await wire_ssrf_guards(page, wire=wire, profile=None, on_response=on_response)
        assert len(context.ws_calls) == 1
        assert context.ws_calls[0][0] == "**/*"
        assert len(context.route_calls) == 1
        assert context.route_calls[0][0] == "**/*"
