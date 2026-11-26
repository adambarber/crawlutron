"""The SSRF WebSocket guard fails closed when the browser cannot intercept sockets.

A context without ``route_web_socket`` refuses the render with a named refusal;
a context that has the capability still renders and installs the guard.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from crawlutron.models.crawl_result import CrawlResult
from crawlutron.models.intercepted_xhr_record import InterceptedXhrRecord
from crawlutron.models.render_options import RenderOptions
from crawlutron.transport.browser import BrowserRenderer
from crawlutron.transport.browser_driver import BrowserDriver
from crawlutron.transport.browser_render_phases import (
    WebSocketGuardUnavailable,
    WireGuard,
    wire_ssrf_guards,
)
from crawlutron.transport.render_page import GuardContext
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl
from tests._browser_page_doubles import BrowserFakePage, FakePageContext
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, admitted

_TARGET = "http://1.2.3.4/"


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


class _IdleDriver:
    """A driver no render reaches: the render refuses before the page is used."""

    async def close(self) -> None:
        return None


class _UncallableSocketContext(_SocketlessContext):
    """A context whose ``route_web_socket`` exists but cannot be called."""

    route_web_socket = "unsupported"


class _RefusingRenderer(BrowserRenderer):
    """A renderer whose every attempt hits the missing-capability refusal."""

    attempts: int = 0

    async def render_once(
        self, url: NormalizedUrl, options: RenderOptions, *, policy: HostPolicy
    ) -> CrawlResult:
        self.attempts += 1
        raise WebSocketGuardUnavailable()


class TestWebSocketGuardNotRetried:
    """A permanent capability refusal is raised once, not retried into a generic failure."""

    async def test_render_raises_the_named_refusal_without_retrying(self):
        """render() propagates WebSocketGuardUnavailable after a single attempt."""
        renderer = _RefusingRenderer(driver=cast(BrowserDriver, _IdleDriver()))
        with pytest.raises(WebSocketGuardUnavailable):
            await renderer.render(
                admitted(_TARGET, policy=UNRESTRICTED_HOST_POLICY),
                RenderOptions(),
                policy=UNRESTRICTED_HOST_POLICY,
            )
        assert renderer.attempts == 1

    async def test_uncallable_route_web_socket_is_refused_the_same_way(self):
        """A present-but-unusable capability is refused, not left to a TypeError."""
        page = BrowserFakePage(context=cast(GuardContext, _UncallableSocketContext()))
        message = await _refuses_render(page)
        assert "route_web_socket" in message
