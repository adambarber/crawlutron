"""Browser SSRF protection under S4 loopback egress proxy.

Browser hands hostnames to the proxy; handlers remain fail-closed admission complements.
"""

import socket

import pytest

import crawlutron.transport.document as doc_mod
from crawlutron.transport.browser import make_ssrf_route_handler, resolve_browser_target
from tests._assertions import block_private_targets
from tests._browser_doubles import (
    BrowserFakePage,
    FakePageContext,
    FakeRequest,
    FakeRoute,
    _ContextSwappingPage,
    build_pinned_renderer,
    pinned_page_renderer,
    render_bare,
    render_pinned,
)

# host_of is no longer needed for IP-pinning tests - browser now navigates to logical URL
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical

# -------------------------------------------------------------------------- document.py: SSRF gate on urlopen
# --------------------------------------------------------------------------


async def test_document_extractor_blocks_internal_url(monkeypatch):
    # The document fetch must refuse a loopback target, and refuse it loudly -- a refused target is not the
    # same outcome as an empty PDF.
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    with pytest.raises(ValueError):
        doc_mod.default_document_text_extractor(canonical("http://127.0.0.1:9/secret.pdf"))


async def test_document_extractor_carries_the_logical_url_without_app_dns(monkeypatch):
    """The document fetch hands the logical URL to the proxy; no resolver runs."""
    from unittest.mock import patch

    import httpx

    from tests._fetcher_factories import MockResponse, scripted_transport_client

    def refuse_resolution(*args, **kwargs):
        raise AssertionError("document fetch must not resolve its target")

    observed: list[httpx.Request] = []
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    monkeypatch.setattr(socket, "getaddrinfo", refuse_resolution)
    with patch(
        "crawlutron.transport.http_client_pool.get_async_http_client",
        return_value=scripted_transport_client(
            [MockResponse(200, {"content-type": "application/pdf"}, b"")],
            observed_requests=observed,
        ),
    ):
        doc_mod.default_document_text_extractor(canonical("http://example.com/x.pdf"))
    assert [str(request.url) for request in observed] == ["http://example.com/x.pdf"]


# Proxy-based navigation helpers.


async def test_resolve_browser_target_returns_logical_url():
    """resolve_browser_target returns logical URL; proxy owns dialing (#991)."""
    pinned_url, headers = await resolve_browser_target(canonical("http://example.com/path"))
    # Returns the logical URL - proxy handles dialing
    assert pinned_url == "http://example.com/path"
    assert headers == {}


async def test_render_navigates_to_logical_url(monkeypatch):
    """Render navigates to the logical URL; proxy owns dialing."""
    page, result = await render_pinned(monkeypatch)
    # Navigate to the logical URL - the proxy handles dialing
    assert page.goto_calls[0][0] == "http://example.com/"
    assert result.status == "success"


async def test_render_rejects_internal_redirect_target(monkeypatch):
    page, renderer = pinned_page_renderer(monkeypatch)
    page.url = "http://127.0.0.1:9/"  # site redirected the browser to loopback
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    with pytest.raises(ValueError):
        await render_bare(renderer)


# -------------------------------------------------------------------------- route-level guard aborts
# SSRF-unsafe requests (subresources + redirects)


async def test_ssrf_route_handler_aborts_unsafe_and_continues_safe(monkeypatch):
    """The route handler aborts SSRF-unsafe hosts and continues safe ones.

    With loopback egress proxy, the handler validates via NormalizedUrl admission
    and continues with the logical URL for safe hosts.
    """
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    handler = make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)

    bad = FakeRoute()
    await handler(bad, FakeRequest("http://127.0.0.1:9/", "document"))
    assert bad.decision == "abort"

    good = FakeRoute()
    await handler(good, FakeRequest("http://1.2.3.4/", "document"))
    assert good.decision == "continue"


async def test_render_registers_ssrf_route_guard(monkeypatch):
    page, renderer = pinned_page_renderer(monkeypatch)
    await render_bare(renderer)
    # The SSRF route guard must be registered at context scope: every page the
    # context spawns is covered, not just the one the install saw.
    assert any(pattern == "**/*" for pattern, _ in page.context.route_calls)


class _GuardedContext(FakePageContext):
    """A context double recording route and websocket installs at context scope."""

    def __init__(self):
        super().__init__()
        self.ws_calls: list[tuple[str, object]] = []

    def route_web_socket(self, pattern, handler):
        self.ws_calls.append((pattern, handler))


class _ContextOnlyPage(_ContextSwappingPage):
    """A page whose own route surface is absent: the guard install must land on
    the context, the surface every page/worker the context spawns shares."""

    context_cls = _GuardedContext


async def test_ssrf_guards_install_at_context_level(monkeypatch):
    """CRLN-SEC-02 gap 2: a guard installed per-page never covers pages the
    context spawns later; the install must land on the context."""
    block_private_targets(monkeypatch)
    page = _ContextOnlyPage()
    _page2, renderer = pinned_page_renderer(monkeypatch, page=page)
    await render_bare(renderer)

    ctx = page.context
    assert isinstance(ctx, _GuardedContext), "the context swapped in must be the recording one"
    # The base page has no route surface, so any install recorded here came
    # from the context -- the surface a later page on it inherits.
    assert any(pattern == "**/*" for pattern, _ in ctx.route_calls)
    assert any(pattern == "**/*" for pattern, _ in ctx.ws_calls)


# S4: proxy owns DNS resolution; browser validates via NormalizedUrl admission.


async def test_ssrf_route_handler_validates_via_normalized_url(monkeypatch):
    """The route handler validates URLs via NormalizedUrl admission.

    When routing through the loopback egress proxy, DNS resolution happens on
    the proxy side. The handler validates the URL via NormalizedUrl admission.
    """
    # Need to disable private targets for this test to check SSRF blocking
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    handler = make_ssrf_route_handler(policy=UNRESTRICTED_HOST_POLICY)

    # SSRF-unsafe host is aborted by NormalizedUrl admission.
    bad = FakeRoute()
    await handler(bad, FakeRequest("http://127.0.0.1:9/", "document"))
    assert bad.decision == "abort", "loopback URL should be aborted by NormalizedUrl admission"

    good = FakeRoute()
    await handler(good, FakeRequest("http://example.com/", "document"))
    assert good.decision == "continue"


def _stub_dns_to_example_com(monkeypatch):
    """Resolve every host to a fixed address, standing in for real DNS."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))
        ],
    )


class _CertMismatchPage(BrowserFakePage):
    """A page whose navigation hits a certificate whose identity does not match
    the hostname -- Playwright's real refusal shape, surfaced verbatim."""

    async def goto(self, url, wait_until="load", **kwargs):
        raise Exception("net::ERR_CERT_COMMON_NAME_INVALID at https://example.com/")


async def test_a_cert_identity_mismatch_is_a_classified_refusal_not_silence(monkeypatch):
    """Cert identity mismatch is classified as tls_error, not silence."""
    block_private_targets(monkeypatch)
    _stub_dns_to_example_com(monkeypatch)

    page = _CertMismatchPage()
    renderer = build_pinned_renderer(page)
    from crawlutron.models.render_options import RenderOptions

    result = await renderer.render(
        canonical("https://example.com/"),
        RenderOptions(timeout_seconds=2, max_retries=0),
        policy=UNRESTRICTED_HOST_POLICY,
    )
    assert result.classification == "tls_error", result.classification
    assert result.status != "success", result.status
