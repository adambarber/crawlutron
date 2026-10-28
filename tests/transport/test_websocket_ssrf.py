"""WebSocket SSRF guard: installed on the render path and forwards allowed sockets.
The wrong attribute name (route_web_socket) once skipped the guard; the dead handler
never connected allowed targets. These tests cover both, on the installed-Playwright API.
"""

from __future__ import annotations

from tests._assertions import block_private_targets, host_resolves_to
from tests._browser_doubles import pinned_page_renderer, render_bare
from tests._browser_page_doubles import FakePageContext, _ContextSwappingPage
from tests._pool_ws_doubles import FakeWS as _FakeWS

_PUBLIC = "1.2.3.4"


class _WSGuardContext(FakePageContext):
    """A context whose websocket-route surface matches installed Playwright 1.62.0:
    ``route_web_socket(url, handler)`` records the handler the renderer installs."""

    def route_web_socket(self, url, handler):
        self.ws_handler = handler
        super().route_web_socket(url, handler)


class _WSBearingPage(_ContextSwappingPage):
    """A page whose CONTEXT records the websocket guard the renderer installs."""

    context_cls = _WSGuardContext

    @property
    def ws_handler(self):
        return self.context.ws_handler


async def test_ws_guard_is_installed_and_closes_unsafe_connects_safe(monkeypatch):
    block_private_targets(monkeypatch)
    page = _WSBearingPage()
    _page2, renderer = pinned_page_renderer(monkeypatch, page=page)
    await render_bare(renderer)

    # The guard must actually be installed at context scope (the old per-page
    # install never covered pages the context spawned later).
    assert page.ws_handler is not None, "websocket SSRF guard was never installed"

    # (a) an unsafe (private/loopback) websocket target is closed, not connected.
    unsafe = _FakeWS("wss://127.0.0.1:9/secret")
    await page.ws_handler(unsafe)
    assert unsafe.closed_count == 1, "unsafe websocket was not closed"
    assert unsafe.connect_calls == 0

    # (b) a safe target's connect_to_server is actually invoked so the socket works.
    with host_resolves_to(monkeypatch, _PUBLIC):
        good = _FakeWS("wss://example.com/chat")
        await page.ws_handler(good)
    assert good.connect_calls == 1, "safe websocket never connected to the server"
    assert good.closed_count == 0
